from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.background_jobs import enqueue, get_job_status
from frappe.utils.data import cint
from rq.job import JobStatus

from ask_alyf.ask_alyf import field_agent
from ask_alyf.ask_alyf.agent import run_message
from ask_alyf.ask_alyf.tools import (
	OPERATION_KIND_BACKEND,
	OPERATION_KIND_FRONTEND,
	execute_pending_operation,
	get_settings,
	validate_frappe_charts_payload,
)
from ask_alyf.ask_alyf.utils import chunk_text, dumps, loads

MODE_ASK = "Ask"
MODE_AGENT = "Agent"
ASK_ALYF_USER_ROLE = "Ask ALYF User"
BACKGROUND_JOB_ID_KEY = "background_job_id"


def _truncate_doc_for_size(
	doc: dict,
	table_fieldnames: set | None = None,
	threshold_bytes: int = 16 * 1024,
	max_rows: int = 20,
) -> dict:
	"""Truncate child-table lists in *doc* when the payload exceeds *threshold_bytes*.

	Args:
		doc: The document dict to (potentially) truncate in-place.
		table_fieldnames: Set of fieldnames whose fieldtype is "Table". When
			`None` or empty, any list-of-dicts value is treated as a child table.
		threshold_bytes: Payload size limit in bytes (default 16 KB).
		max_rows: Maximum rows to retain per child table (default 20).

	Returns:
		The same *doc* dict, mutated in-place and returned for convenience.
	"""
	if len(json.dumps(doc, default=str)) <= threshold_bytes:
		return doc

	known_tables: set = table_fieldnames or set()

	for key, value in doc.items():
		if not isinstance(value, list):
			continue
		is_child_table = key in known_tables or (value and isinstance(value[0], dict))
		if is_child_table and len(value) > max_rows:
			total = len(value)
			doc[key] = [
				*value[:max_rows],
				{"_truncated": f"Child table truncated to first {max_rows} of {total} rows for size"},
			]

	return doc


def get_support_phone_uri(phone_number: str | None) -> str:
	phone_number = (phone_number or "").strip()
	if not phone_number:
		return ""

	digits_only = "".join(character for character in phone_number if character.isdigit())
	if not digits_only:
		return ""

	prefix = "+" if phone_number.startswith("+") else ""
	return f"tel:{prefix}{digits_only}"


def get_ask_alyf_boot_payload() -> dict:
	settings_available = frappe.db.exists("DocType", "Ask ALYF Settings")
	configured = False
	agent_mode_enabled = False
	field_agent_enabled = False
	file_upload_enabled = False
	support_phone_number = ""
	support_phone_uri = ""

	try:
		settings = get_settings()
		agent_mode_enabled = bool(settings.allow_agent_mode)
		field_agent_enabled = bool(settings.allow_field_agent)
		file_upload_enabled = bool(settings.allow_file_upload)
		api_key = (settings.get_password("api_key", raise_exception=False) or "").strip()
		configured = bool(api_key and (settings.model or "").strip())
		support_phone_number = (settings.support_phone_number or "").strip()
		support_phone_uri = get_support_phone_uri(support_phone_number)
	except Exception:
		pass

	return {
		"allowed": can_access_ask_alyf(),
		"configured": configured,
		"agent_mode_enabled": agent_mode_enabled,
		"field_agent_enabled": field_agent_enabled,
		"file_upload_enabled": file_upload_enabled,
		"support_phone_number": support_phone_number,
		"support_phone_uri": support_phone_uri,
		"default_mode": MODE_ASK,
		"site_name": frappe.local.site,
		"user": frappe.session.user,
		"settings_available": bool(settings_available),
	}


def can_access_ask_alyf() -> bool:
	if frappe.session.user == "Guest":
		return False

	return ASK_ALYF_USER_ROLE in frappe.get_roles()


def can_use_agent_mode() -> bool:
	if not can_access_ask_alyf():
		return False

	try:
		settings = get_settings()
	except Exception:
		return False

	return bool(settings.allow_agent_mode)


def normalize_mode(mode: str | None) -> str:
	if mode == MODE_AGENT:
		if not can_use_agent_mode():
			frappe.throw(_("Agent mode is disabled or not available for your user."))
		return MODE_AGENT

	return MODE_ASK


def make_message(role: str, content: str, **metadata) -> dict:
	return {
		"id": uuid4().hex,
		"role": role,
		"content": content,
		"created_at": now_datetime().isoformat(),
		"metadata": metadata,
	}


def load_pending_operations(json_str: str) -> list[dict[str, Any]]:
	return loads(json_str, []) or []


def build_assistant_message_metadata(
	mode: str,
	*,
	pending_operations: list[dict[str, Any]] | None = None,
	document_extractions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
	"""Build persisted metadata for an assistant message."""
	metadata = {
		"mode": mode,
		"pending_operation": bool(pending_operations),
	}
	if isinstance(document_extractions, list) and document_extractions:
		metadata["document_extractions"] = document_extractions
	return metadata


def apply_agent_result_to_conversation(
	doc,
	messages: list[dict[str, Any]],
	result: dict[str, Any],
	mode: str,
	**extra_metadata: Any,
) -> list[dict[str, Any]]:
	"""Apply a run_message result to the conversation and save. Returns all pending operations."""
	response = result.get("response") or ""
	new_operations = result.get("pending_operations") or []
	if not isinstance(new_operations, list):
		new_operations = []
	document_extractions = result.get("document_extractions")
	attached_files = result.get("attached_files")

	if new_operations and not response:
		response = _("I've prepared the operation. Please review and confirm.")

	if isinstance(attached_files, list) and attached_files:
		file_names = ", ".join(f.get("file_name") or f.get("name") or "" for f in attached_files)
		messages.append(make_message("system", file_names, files=attached_files))

	assistant_message = make_message(
		"assistant",
		response,
		**build_assistant_message_metadata(
			mode,
			pending_operations=new_operations,
			document_extractions=document_extractions,
		),
		**extra_metadata,
	)
	messages.append(assistant_message)

	stamped = [{**op, "assistant_message_id": assistant_message["id"]} for op in new_operations]
	existing = load_pending_operations(doc.pending_operation_json)
	all_operations = existing + stamped
	doc.pending_operation_json = dumps(all_operations) if all_operations else ""
	save_messages(doc, messages)
	return all_operations


def _find_operation_by_call_id(
	operations: list[dict[str, Any]],
	call_id: str,
) -> dict[str, Any]:
	"""Find a pending operation by call_id, falling back to the first operation."""
	if call_id:
		for op in operations:
			if op.get("call_id") == call_id:
				return op
		frappe.throw(_("No pending operation matches call_id '{0}'.").format(call_id))
	return operations[0]


def find_assistant_message_for_pending_operation(
	messages: list[dict[str, Any]],
	pending_operation: dict[str, Any],
) -> dict[str, Any] | None:
	target_id = pending_operation.get("assistant_message_id")
	if not isinstance(target_id, str) or not target_id.strip():
		return None

	target_id = target_id.strip()
	for msg in messages:
		if msg.get("id") == target_id:
			return msg

	return None


def attach_frappe_charts_to_message(
	message: dict[str, Any],
	charts: list[dict[str, Any]],
) -> None:
	meta = message.setdefault("metadata", {})
	meta["frappe_charts"] = [*charts]
	meta["pending_operation"] = False


def get_or_create_conversation(conversation_name: str | None = None):
	if conversation_name:
		doc = frappe.get_doc("Ask ALYF Conversation", conversation_name)
		doc.check_permission("read")
		return doc

	existing = frappe.get_list(
		"Ask ALYF Conversation",
		filters={"owner": frappe.session.user, "status": "Active"},
		fields=["name"],
		order_by="modified desc",
		limit=1,
	)
	if existing:
		return frappe.get_doc("Ask ALYF Conversation", existing[0].name)

	doc = frappe.get_doc(
		doctype="Ask ALYF Conversation",
		title=_("New Conversation"),
		status="Active",
		messages_json="[]",
	)
	doc.insert()
	return doc


def get_messages(conversation) -> list[dict]:
	return loads(conversation.messages_json, [])


def save_messages(conversation, messages: list[dict]):
	conversation.messages_json = dumps(messages)
	conversation.last_message_at = now_datetime()
	conversation.save()


def _build_confirm_ack_content(
	pending_operation: dict[str, Any],
	action_result: dict[str, Any],
	execution_error: str | None,
) -> str:
	if execution_error:
		return _("Could not confirm operation: {0}").format(execution_error)

	content = _("Confirmed operation: {0}").format(
		pending_operation.get("summary") or pending_operation.get("tool")
	)
	if action_result.get("doctype") and action_result.get("name"):
		content += "\n\n" + _("Document: {0} {1}").format(
			_(action_result["doctype"]),
			action_result["name"],
		)
	elif action_result.get("message"):
		content += "\n\n" + str(action_result["message"])
	return content


def _collect_completed_action_summaries(messages: list[dict[str, Any]]) -> list[str]:
	"""Collect one-line summaries of successfully executed or rejected actions.

	Failed actions are intentionally excluded so the agent may retry them.
	"""
	summaries = []
	for msg in messages:
		if msg.get("role") != "assistant":
			continue
		meta = msg.get("metadata") or {}
		content = (msg.get("content") or "").split("\n", 1)[0].strip()
		status = meta.get("action_status") or meta.get("frontend_action_status")
		if meta.get("rejected_action") or status == "rejected":
			summaries.append(f"- Rejected by user: {content}")
		elif status == "success":
			summaries.append(f"- Executed successfully: {content}")
	return summaries


def continue_after_action(
	conversation,
	mode: str,
	messages: list[dict[str, Any]],
	pending_operation: dict[str, Any],
	*,
	status: str,
	result_payload: dict[str, Any] | None = None,
	error: str | None = None,
) -> dict[str, Any] | None:
	"""Run the agent after an action result so it can confirm, handle errors, or propose follow-ups."""
	request_context = loads(conversation.last_context_json, {})
	if not isinstance(request_context, dict):
		request_context = {}

	operation_payload = pending_operation.get("payload")
	operation_payload = operation_payload if isinstance(operation_payload, dict) else {}
	system_payload: dict[str, Any] = {
		"status": status,
		"operation": {
			"kind": pending_operation.get("kind"),
			"tool": pending_operation.get("tool"),
			"summary": pending_operation.get("summary"),
			"payload": operation_payload,
		},
	}
	if result_payload:
		system_payload["result"] = result_payload
	if error:
		system_payload["error"] = error

	if status == "rejected":
		system_message = (
			"The user rejected this proposed action. "
			"Acknowledge briefly. If you can suggest an alternative, do so.\n"
			f"{dumps(system_payload)}"
		)
	else:
		system_message = (
			"This action has been executed. "
			"Use the result context below to continue.\n"
			f"{dumps(system_payload)}"
		)

	prior_actions = _collect_completed_action_summaries(messages)
	if prior_actions:
		system_message += (
			"\n\nActions already completed earlier in this conversation:\n"
			+ "\n".join(prior_actions)
			+ "\n\nDo NOT re-propose any action that was already executed or rejected."
		)

	history_with_result = list(messages)
	history_with_result.append({"role": "system", "content": system_message})

	try:
		return run_message(
			conversation_name=conversation.name,
			message=(
				"Confirm the action result briefly. "
				"If the user's original request is not fully completed, "
				"proceed with the next write action now. "
				"NEVER re-propose an action that was already executed or confirmed. "
				"If all requested changes are done, summarize the results and stop."
			),
			mode=mode,
			request_context=request_context,
			conversation_history=history_with_result,
		)
	except Exception:
		frappe.log_error("Ask ALYF Action Follow-Up Error")
		frappe.clear_messages()
		return None


def conversation_payload(conversation) -> dict:
	return {
		"name": conversation.name,
		"title": conversation.title,
		"status": conversation.status,
		"route": conversation.route,
		"messages": get_messages(conversation),
		"pending_operations": load_pending_operations(conversation.pending_operation_json),
		"last_context": loads(conversation.last_context_json, {}),
	}


def publish_status_update(conversation_name: str, user: str, text: str):
	frappe.publish_realtime(
		"ask_alyf_status",
		{"conversation": conversation_name, "text": text},
		user=user,
	)


@frappe.whitelist()
def bootstrap(conversation: str | None = None) -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	doc = get_or_create_conversation(conversation_name=conversation)
	return {
		"ask_alyf": get_ask_alyf_boot_payload(),
		"conversation": conversation_payload(doc),
	}


@frappe.whitelist()
def list_conversations(limit: int = 20) -> list[dict]:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	limit = max(1, cint(limit))
	conversations = frappe.get_list(
		"Ask ALYF Conversation",
		filters={"owner": frappe.session.user},
		fields=["name", "title", "status", "modified", "last_message_at"],
		order_by="modified desc",
		limit=limit,
	)

	return conversations


@frappe.whitelist(methods=["POST"])
def start_new_conversation() -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	doc = frappe.get_doc(
		doctype="Ask ALYF Conversation",
		title=_("New Conversation"),
		status="Active",
		messages_json="[]",
	)
	doc.insert()
	return conversation_payload(doc)


@frappe.whitelist(methods=["POST"])
def send_message(
	message: str,
	mode: str = MODE_ASK,
	conversation: str | None = None,
	context: str | dict | None = None,
) -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	normalized_mode = normalize_mode(mode)
	context_data = frappe.parse_json(context) if isinstance(context, str) else (context or {})
	doc = get_or_create_conversation(conversation_name=conversation)

	messages = get_messages(doc)
	job_id = uuid4().hex
	user_message = make_message(
		"user",
		message,
		mode=normalized_mode,
		**{BACKGROUND_JOB_ID_KEY: job_id},
	)
	messages.append(user_message)

	if doc.title == _("New Conversation"):
		doc.title = message[:72]

	doc.route = context_data.get("route")
	doc.last_context_json = dumps(context_data)
	doc.pending_operation_json = ""
	save_messages(doc, messages)

	enqueue(
		"ask_alyf.ask_alyf.api.process_message_job",
		queue="short",
		enqueue_after_commit=True,
		job_id=job_id,
		conversation_name=doc.name,
		message=message,
		mode=normalized_mode,
		context_data=context_data,
		user_message_id=user_message["id"],
	)

	return {
		"conversation": doc.name,
		"user_message_id": user_message["id"],
		"job_id": job_id,
	}


@frappe.whitelist()
def get_message_job_status(conversation: str, user_message_id: str, job_id: str) -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	doc = frappe.get_doc("Ask ALYF Conversation", conversation)
	doc.check_permission("read")
	messages = get_messages(doc)
	user_message_index = next(
		(
			index
			for index, item in enumerate(messages)
			if item.get("role") == "user" and item.get("id") == user_message_id
		),
		None,
	)
	if user_message_index is None:
		frappe.throw(_("The message could not be found in this conversation."))

	user_message = messages[user_message_index]
	if user_message.get("metadata", {}).get(BACKGROUND_JOB_ID_KEY) != job_id:
		frappe.throw(_("The background job does not match this message."))

	if any(item.get("role") == "assistant" for item in messages[user_message_index + 1 :]):
		return {"status": "completed", "conversation": conversation_payload(doc)}

	status = get_job_status(job_id)
	if status in {
		JobStatus.QUEUED,
		JobStatus.STARTED,
		JobStatus.DEFERRED,
		JobStatus.SCHEDULED,
	}:
		return {"status": "pending"}
	if status in {JobStatus.FINISHED, JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED}:
		doc.reload()
		messages = get_messages(doc)
		user_message_index = next(
			(
				index
				for index, item in enumerate(messages)
				if item.get("role") == "user" and item.get("id") == user_message_id
			),
			None,
		)
		if user_message_index is None:
			frappe.throw(_("The message could not be found in this conversation."))
		if any(item.get("role") == "assistant" for item in messages[user_message_index + 1 :]):
			return {"status": "completed", "conversation": conversation_payload(doc)}
	if status == JobStatus.FINISHED:
		return {"status": "completed", "conversation": conversation_payload(doc)}
	if status in {JobStatus.FAILED, JobStatus.STOPPED, JobStatus.CANCELED}:
		return {"status": "failed"}
	return {"status": "missing"}


def process_message_job(
	conversation_name: str,
	message: str,
	mode: str,
	context_data: dict,
	user_message_id: str | None = None,
):
	doc = frappe.get_doc("Ask ALYF Conversation", conversation_name)
	messages = get_messages(doc)
	history = messages[:-1] if messages and messages[-1].get("id") == user_message_id else messages

	frappe.publish_realtime(
		"ask_alyf_response_start",
		{"conversation": conversation_name},
		user=doc.owner,
	)

	try:
		result = run_message(
			conversation_name=conversation_name,
			message=message,
			mode=mode,
			request_context=context_data,
			conversation_history=history,
		)
		response = result.get("response") or ""
		pending_operations = result.get("pending_operations") or []
		if not isinstance(pending_operations, list):
			pending_operations = []
		document_extractions = result.get("document_extractions")
		attached_files = result.get("attached_files")
		if pending_operations and not response:
			response = _("I've prepared the operation. Please review and confirm.")
	except Exception as error:
		frappe.log_error("Ask ALYF Agent Error")
		frappe.clear_messages()
		response = str(error).strip() or _("I hit an error while processing that request. Please try again.")
		pending_operations = []
		document_extractions = None
		attached_files = None

	file_message = None
	if isinstance(attached_files, list) and attached_files:
		file_names = ", ".join(f.get("file_name") or f.get("name") or "" for f in attached_files)
		file_message = make_message("system", file_names, files=attached_files)
		messages.append(file_message)

	assistant_message = make_message(
		"assistant",
		response,
		**build_assistant_message_metadata(
			mode,
			pending_operations=pending_operations,
			document_extractions=document_extractions,
		),
	)
	messages.append(assistant_message)

	stamped = [{**op, "assistant_message_id": assistant_message["id"]} for op in pending_operations]
	doc.pending_operation_json = dumps(stamped) if stamped else ""
	save_messages(doc, messages)

	if file_message:
		frappe.publish_realtime(
			"ask_alyf_file_attachment",
			{
				"conversation": conversation_name,
				"message": file_message,
			},
			user=doc.owner,
		)

	for chunk in chunk_text(response or " "):
		frappe.publish_realtime(
			"ask_alyf_response_chunk",
			{
				"conversation": conversation_name,
				"message_id": assistant_message["id"],
				"chunk": chunk + " ",
			},
			user=doc.owner,
		)

	frappe.publish_realtime(
		"ask_alyf_response_complete",
		{
			"conversation": conversation_name,
			"message_id": assistant_message["id"],
			"pending_operations": stamped,
		},
		user=doc.owner,
	)


@frappe.whitelist(methods=["POST"])
def confirm_pending_operation(conversation: str, call_id: str = "", mode: str = MODE_ASK) -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	doc = frappe.get_doc("Ask ALYF Conversation", conversation)
	doc.check_permission("write")
	all_operations = load_pending_operations(doc.pending_operation_json)
	if not all_operations:
		frappe.throw(_("There is no pending operation to confirm."))

	call_id = (call_id or "").strip()
	pending_operation = _find_operation_by_call_id(all_operations, call_id)

	messages = get_messages(doc)
	normalized_mode = normalize_mode(mode)
	if pending_operation.get("kind") != OPERATION_KIND_BACKEND:
		frappe.throw(_("Only backend actions can be confirmed by this endpoint."))

	remaining = [op for op in all_operations if op.get("call_id") != pending_operation.get("call_id")]
	doc.pending_operation_json = dumps(remaining) if remaining else ""

	publish_status_update(doc.name, doc.owner, _("Confirming action..."))
	try:
		execution_error = None
		try:
			result = execute_pending_operation(pending_operation)
		except Exception as error:
			frappe.log_error("Ask ALYF Confirm Action Error")
			frappe.clear_messages()
			execution_error = str(error)
			result = None

		operation_payload = pending_operation.get("payload") if isinstance(pending_operation, dict) else {}
		operation_payload = operation_payload if isinstance(operation_payload, dict) else {}
		action_result = {
			"kind": pending_operation.get("kind"),
			"tool": pending_operation.get("tool"),
			"summary": pending_operation.get("summary"),
			"doctype": operation_payload.get("doctype"),
			"name": operation_payload.get("name"),
		}
		if isinstance(result, dict):
			action_result["name"] = result.get("name") or result.get("new_name") or action_result["name"]
			action_result["message"] = result.get("message")
		action_result = {key: value for key, value in action_result.items() if value not in (None, "")}

		status = "failed" if execution_error else "success"

		ack_meta = {"confirmed_action": True, "action_status": status}

		if remaining:
			content = _build_confirm_ack_content(pending_operation, action_result, execution_error)
			messages.append(make_message("assistant", content, mode=normalized_mode, **ack_meta))
			save_messages(doc, messages)
		else:
			publish_status_update(doc.name, doc.owner, _("Generating response..."))
			agent_result = continue_after_action(
				doc,
				normalized_mode,
				messages,
				pending_operation,
				status=status,
				result_payload=action_result,
				error=execution_error,
			)
			if agent_result:
				apply_agent_result_to_conversation(
					doc,
					messages,
					agent_result,
					normalized_mode,
					**ack_meta,
				)
			else:
				content = _build_confirm_ack_content(pending_operation, action_result, execution_error)
				messages.append(make_message("assistant", content, mode=normalized_mode, **ack_meta))
				save_messages(doc, messages)

		response_payload: dict[str, Any] = {"conversation": conversation_payload(doc)}
		if execution_error:
			response_payload["error"] = execution_error
		else:
			response_payload["result"] = action_result
		return response_payload
	finally:
		publish_status_update(doc.name, doc.owner, "")


@frappe.whitelist(methods=["POST"])
def reject_pending_operation(conversation: str, call_id: str = "", mode: str = MODE_ASK) -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	doc = frappe.get_doc("Ask ALYF Conversation", conversation)
	doc.check_permission("write")
	all_operations = load_pending_operations(doc.pending_operation_json)
	if not all_operations:
		return {"conversation": conversation_payload(doc)}

	call_id = (call_id or "").strip()
	pending_operation = _find_operation_by_call_id(all_operations, call_id)

	remaining = [op for op in all_operations if op.get("call_id") != pending_operation.get("call_id")]
	doc.pending_operation_json = dumps(remaining) if remaining else ""

	normalized_mode = normalize_mode(mode)
	messages = get_messages(doc)

	try:
		summary = pending_operation.get("summary") or pending_operation.get("tool")

		if remaining:
			messages.append(
				make_message(
					"assistant",
					_("Cancelled the pending operation: {0}.").format(summary),
					rejected_action=True,
					mode=normalized_mode,
				)
			)
			save_messages(doc, messages)
		else:
			publish_status_update(doc.name, doc.owner, _("Generating response..."))
			agent_result = continue_after_action(
				doc,
				normalized_mode,
				messages,
				pending_operation,
				status="rejected",
			)
			if agent_result:
				apply_agent_result_to_conversation(
					doc,
					messages,
					agent_result,
					normalized_mode,
					rejected_action=True,
				)
			else:
				messages.append(
					make_message(
						"assistant",
						_("Cancelled the pending operation: {0}.").format(summary),
						rejected_action=True,
						mode=normalized_mode,
					)
				)
				save_messages(doc, messages)

		return {"conversation": conversation_payload(doc)}
	finally:
		publish_status_update(doc.name, doc.owner, "")


def _resolve_show_chart_frontend_action(
	doc,
	messages: list[dict[str, Any]],
	pending_operation: dict[str, Any],
	remaining_operations: list[dict[str, Any]],
	status_value: str,
	error: str | None,
	normalized_mode: str,
) -> dict[str, Any]:
	doc.pending_operation_json = dumps(remaining_operations) if remaining_operations else ""

	if status_value == "success":
		payload = pending_operation.get("payload")
		payload = payload if isinstance(payload, dict) else {}
		charts, validation_error = validate_frappe_charts_payload(payload.get("frappe_charts"))
		if validation_error:
			frappe.throw(validation_error)
		target = find_assistant_message_for_pending_operation(messages, pending_operation)
		if not target:
			frappe.throw(_("Could not attach charts to the assistant message."))
		attach_frappe_charts_to_message(target, charts)
		save_messages(doc, messages)
		return {"conversation": conversation_payload(doc)}

	if status_value == "rejected":
		content = _("Cancelled showing chart: {0}.").format(pending_operation.get("summary") or _("chart"))
	else:
		content = _("Could not display chart.")
		if error:
			content += " " + _("Reason: {0}").format(error)

	messages.append(
		make_message(
			"assistant",
			content,
			mode=normalized_mode,
			frontend_action_result=True,
			frontend_action_status=status_value,
		)
	)
	save_messages(doc, messages)
	return {"conversation": conversation_payload(doc)}


@frappe.whitelist(methods=["POST"])
def frontend_action_result(
	conversation: str,
	call_id: str,
	status: str,
	mode: str = MODE_ASK,
	result: str | dict | None = None,
	error: str | None = None,
) -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	doc = frappe.get_doc("Ask ALYF Conversation", conversation)
	doc.check_permission("write")
	all_operations = load_pending_operations(doc.pending_operation_json)
	if not all_operations:
		frappe.throw(_("There is no pending frontend action to resolve."))

	call_id = (call_id or "").strip()
	pending_operation = _find_operation_by_call_id(all_operations, call_id)

	if pending_operation.get("kind") != OPERATION_KIND_FRONTEND:
		frappe.throw(_("The pending operation is not a frontend action."))

	remaining = [op for op in all_operations if op.get("call_id") != pending_operation.get("call_id")]

	status_value = (status or "").strip().lower()
	if status_value not in {"success", "failed", "rejected"}:
		frappe.throw(_("Status must be one of success, failed, or rejected."))

	normalized_mode = normalize_mode(mode)
	result_payload: dict[str, Any] = {}
	if isinstance(result, str):
		parsed_result = frappe.parse_json(result)
		if isinstance(parsed_result, dict):
			result_payload = parsed_result
	elif isinstance(result, dict):
		result_payload = result

	publish_status_update(doc.name, doc.owner, _("Generating response..."))
	try:
		messages = get_messages(doc)
		if (pending_operation.get("tool") or "").strip() == "show_chart":
			return _resolve_show_chart_frontend_action(
				doc,
				messages,
				pending_operation,
				remaining,
				status_value,
				error,
				normalized_mode,
			)

		doc.pending_operation_json = dumps(remaining) if remaining else ""
		extra_metadata: dict[str, Any] = {
			"frontend_action_result": True,
			"frontend_action_status": status_value,
		}
		if result_payload:
			extra_metadata["frontend_action_payload"] = result_payload

		summary = pending_operation.get("summary") or pending_operation.get("tool") or _("frontend action")

		if remaining:
			if status_value == "success":
				content = _("Executed frontend action: {0}").format(summary)
			elif status_value == "rejected":
				content = _("Cancelled frontend action: {0}").format(summary)
			else:
				content = _("Frontend action failed: {0}").format(summary)
				if error:
					content += "\n\n" + _("Reason: {0}").format(error)
			messages.append(make_message("assistant", content, mode=normalized_mode, **extra_metadata))
			save_messages(doc, messages)
		else:
			agent_result = continue_after_action(
				doc,
				normalized_mode,
				messages,
				pending_operation,
				status=status_value,
				result_payload=result_payload,
				error=error,
			)
			if agent_result:
				apply_agent_result_to_conversation(
					doc,
					messages,
					agent_result,
					normalized_mode,
					**extra_metadata,
				)
			else:
				if status_value == "success":
					content = _("Executed frontend action: {0}").format(summary)
				elif status_value == "rejected":
					content = _("Cancelled frontend action: {0}").format(summary)
				else:
					content = _("Frontend action failed: {0}").format(summary)
					if error:
						content += "\n\n" + _("Reason: {0}").format(error)
				messages.append(make_message("assistant", content, mode=normalized_mode, **extra_metadata))
				save_messages(doc, messages)

		return {"conversation": conversation_payload(doc)}
	finally:
		publish_status_update(doc.name, doc.owner, "")


@frappe.whitelist(methods=["POST"])
def field_agent_run(
	doctype: str,
	fieldname: str,
	fieldtype: str,
	current_value: str,
	doc: str | dict,
	prompt: str,
) -> dict:
	"""Run the field agent trigger for a single field and return the generated value.

	Permission checks (in order):
	1. User must have the Ask ALYF User role.
	2. Settings must have allow_field_agent enabled.
	3. User must have read permission on the target DocType.

	Args:
		doctype: The DocType of the document being edited.
		fieldname: The field name being generated.
		fieldtype: The Frappe fieldtype string.
		current_value: The current field value.
		doc: The document dict (JSON string or dict) from the frontend form.
		prompt: The user's natural-language instruction.

	Returns:
		A dict with key "response" containing the generated field value.
	"""
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	settings = get_settings()
	if not settings.allow_field_agent:
		frappe.throw(_("Field Agent is not enabled."))

	if not frappe.has_permission(doctype, "read"):
		frappe.throw(_("You do not have read permission on {0}.").format(doctype))

	# Sanitize doc: parse JSON string if needed
	if isinstance(doc, str):
		doc_data: dict = frappe.parse_json(doc) or {}
	else:
		doc_data = dict(doc) if doc else {}

	# Single meta lookup; fail-closed so password stripping cannot be silently skipped
	meta = frappe.get_meta(doctype)
	password_fieldnames = {df.fieldname for df in meta.fields if df.fieldtype == "Password"}
	for pw_field in password_fieldnames:
		doc_data.pop(pw_field, None)

	table_fieldnames = {df.fieldname for df in meta.fields if df.fieldtype == "Table"}
	_truncate_doc_for_size(doc_data, table_fieldnames=table_fieldnames)

	result = field_agent.run_field_agent(
		doctype=doctype,
		fieldname=fieldname,
		fieldtype=fieldtype,
		current_value=current_value,
		doc=doc_data,
		prompt=prompt,
	)
	return {"response": result}


@frappe.whitelist(methods=["POST"])
def attach_file(conversation: str, file: str | dict) -> dict:
	if not can_access_ask_alyf():
		frappe.throw(_("You do not have access to Ask ALYF."))

	settings = get_settings()
	if not settings.allow_file_upload:
		frappe.throw(_("File upload is not enabled."))

	doc = frappe.get_doc("Ask ALYF Conversation", conversation)
	doc.check_permission("write")

	file_data = frappe.parse_json(file) if isinstance(file, str) else file
	file_id = ""
	if isinstance(file_data, dict):
		file_id = (file_data.get("name") or file_data.get("file_id") or "").strip()
	elif isinstance(file_data, str):
		file_id = file_data.strip()

	if not file_id:
		frappe.throw(_("No valid file provided."))
	if not frappe.db.exists("File", file_id):
		frappe.throw(_("File '{0}' was not found.").format(file_id))

	file_doc = frappe.get_doc("File", file_id)
	file_doc.check_permission("read")

	file_entry = {
		"name": file_doc.name,
		"file_name": file_doc.file_name,
		"file_url": file_doc.file_url,
	}

	content = f"User attached a file: {file_entry['file_name']} (ID: {file_entry['name']})"
	messages = get_messages(doc)
	messages.append(make_message("system", content, files=[file_entry]))
	save_messages(doc, messages)

	return {"conversation": conversation_payload(doc)}
