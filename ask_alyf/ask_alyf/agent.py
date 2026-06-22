import functools
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import frappe
from any_llm import AnyLLM
from frappe import _
from tinyagent import AgentConfig, TinyAgent

from ask_alyf.ask_alyf import tools
from ask_alyf.ask_alyf.skill_utils import (
	build_available_skills_instruction,
	get_accessible_skill_doc,
)
from ask_alyf.ask_alyf.utils import parse_newline_list

SPECIALIST_JSON_OUTPUT_INSTRUCTION = (
	"Return only a valid JSON object. "
	"Do not wrap the JSON in markdown fences. "
	"Do not add explanatory prose before or after the JSON."
)
ALLOWED_DOCUMENT_PLANNER_TOOLS = frozenset({"insert", "save", "set_value"})


@dataclass
class ask_alyfRuntime:
	conversation_name: str
	mode: str
	request_context: dict[str, Any]
	conversation_history: list[dict[str, Any]] = field(default_factory=list)
	pending_operations: list[dict[str, Any]] = field(default_factory=list)
	document_extractions: list[dict[str, Any]] = field(default_factory=list)
	attached_files: list[dict[str, Any]] = field(default_factory=list)

	def emit_status(self, text: str):
		"""Send a short status update to the current user."""
		frappe.publish_realtime(
			"ask_alyf_status",
			{"conversation": self.conversation_name, "text": text},
			user=frappe.session.user,
		)

	def remember_document_extraction(self, extraction: dict[str, Any], *, extraction_prompt: str = ""):
		"""Store a normalized extraction result for reuse in later turns."""
		self.document_extractions.append(
			_build_document_extraction_history_entry(extraction, extraction_prompt=extraction_prompt)
		)

	def remember_attached_file(self, file_entry: dict[str, Any]):
		"""Store a file attachment to be shown in the conversation history."""
		self.attached_files.append(file_entry)


def _get_api_key_from_settings(settings) -> str:
	api_key = (settings.get_password("api_key", raise_exception=False) or "").strip()
	if not api_key:
		frappe.throw(_("Configure an API key in Ask ALYF Settings before sending messages."))
	return api_key


def _get_model_id_from_settings(settings) -> str:
	model_id = (settings.model or "").strip()
	if not model_id:
		frappe.throw(_("Configure a model in Ask ALYF Settings before sending messages."))

	try:
		AnyLLM.split_model_provider(model_id)
		return model_id
	except ValueError:
		pass

	if settings.llm_provider in {"OpenAI", "OpenAI Compatible"}:
		return f"openai:{model_id}"

	return model_id


def _build_internal_agent_config(
	*,
	settings,
	name: str,
	instructions: str,
	tool_defs: list[Callable[..., Any]],
):
	return AgentConfig(
		name=name,
		model_id=_get_model_id_from_settings(settings),
		api_base=(settings.base_url or "").strip() or None,
		api_key=_get_api_key_from_settings(settings),
		instructions=instructions,
		tools=tool_defs,
		model_args={"temperature": 0.1},
	)


async def _create_internal_agent_async(
	*,
	settings,
	name: str,
	instructions: str,
	tool_defs: list[Callable[..., Any]],
):
	return await TinyAgent.create_async(
		_build_internal_agent_config(
			settings=settings,
			name=name,
			instructions=instructions,
			tool_defs=tool_defs,
		),
	)


def _build_specialist_history_context(runtime: ask_alyfRuntime, limit: int = 6) -> str:
	history = getattr(runtime, "conversation_history", None) or []
	if not history:
		return ""

	lines = [_("Recent conversation context:")]
	for item in history[-limit:]:
		lines.extend(_build_history_item_lines(item))
	return "\n".join(lines)


def _parse_json_object_output(raw_output: Any) -> dict[str, Any] | None:
	if isinstance(raw_output, dict):
		return raw_output
	if not isinstance(raw_output, str):
		return None

	text = raw_output.strip()
	if not text:
		return None

	candidates = [text]
	fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
	if fenced_match:
		candidates.insert(0, fenced_match.group(1).strip())

	start = text.find("{")
	end = text.rfind("}")
	if start != -1 and end != -1 and start < end:
		candidates.append(text[start : end + 1])

	for candidate in candidates:
		try:
			parsed = json.loads(candidate)
		except Exception:
			continue
		if isinstance(parsed, dict):
			return parsed

	return None


def _coerce_string_list(value: Any, *, limit: int = 12) -> list[str]:
	if not isinstance(value, list):
		return []

	items: list[str] = []
	for entry in value:
		if not isinstance(entry, str):
			continue
		text = entry.strip()
		if not text:
			continue
		items.append(text[:500])
		if len(items) >= limit:
			break

	return items


def _coerce_evidence_entries(value: Any) -> list[dict[str, Any]]:
	if not isinstance(value, list):
		return []

	evidence: list[dict[str, Any]] = []
	for entry in value[:12]:
		if not isinstance(entry, dict):
			continue

		path = entry.get("path")
		if not isinstance(path, str) or not path.strip():
			continue

		start_line = entry.get("start_line")
		end_line = entry.get("end_line")
		note = entry.get("note") or entry.get("reason") or ""
		item: dict[str, Any] = {"path": path.strip()}
		if isinstance(start_line, int) and start_line > 0:
			item["start_line"] = start_line
		if isinstance(end_line, int) and end_line > 0:
			item["end_line"] = end_line
		if isinstance(note, str) and note.strip():
			item["note"] = note.strip()[:500]
		evidence.append(item)

	return evidence


def _normalize_source_code_analysis(result: dict[str, Any], *, raw_output: str = "") -> dict[str, Any]:
	answer = result.get("answer")
	summary = result.get("summary")
	uncertainty = result.get("uncertainty")

	answer_text = answer.strip() if isinstance(answer, str) else ""
	summary_text = summary.strip() if isinstance(summary, str) else ""
	uncertainty_text = uncertainty.strip() if isinstance(uncertainty, str) else ""
	evidence = _coerce_evidence_entries(result.get("evidence"))
	searched_paths = _coerce_string_list(result.get("searched_paths"))

	if not answer_text:
		answer_text = summary_text or raw_output.strip()
	if not summary_text:
		summary_text = answer_text

	return {
		"answer": answer_text,
		"summary": summary_text,
		"evidence": evidence,
		"uncertainty": uncertainty_text,
		"searched_paths": searched_paths,
	}


def _build_document_planner_failure(
	message: str,
	*,
	recommended_tool: str = "",
	payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
	return {
		"ready": False,
		"recommended_tool": recommended_tool if recommended_tool in ALLOWED_DOCUMENT_PLANNER_TOOLS else "",
		"payload": payload or {},
		"reason": "",
		"missing_information": [message],
		"checks": [],
		"warnings": [],
	}


def _normalize_document_plan(
	result: dict[str, Any],
	*,
	default_doctype: str = "",
	default_operation: str = "",
	default_name: str = "",
	raw_output: str = "",
) -> dict[str, Any]:
	requested_operation = default_operation.strip().lower()
	recommended_tool = result.get("recommended_tool")
	if isinstance(recommended_tool, str):
		recommended_tool = recommended_tool.strip().lower()
	else:
		recommended_tool = ""

	if recommended_tool not in ALLOWED_DOCUMENT_PLANNER_TOOLS:
		recommended_tool = (
			requested_operation if requested_operation in ALLOWED_DOCUMENT_PLANNER_TOOLS else ""
		)

	payload = result.get("payload")
	payload = payload if isinstance(payload, dict) else {}
	if default_doctype and "doctype" not in payload:
		payload["doctype"] = default_doctype
	if default_name and recommended_tool in {"save", "set_value"} and "name" not in payload:
		payload["name"] = default_name

	reason = result.get("reason")
	reason_text = reason.strip()[:1000] if isinstance(reason, str) else ""
	missing_information = _coerce_string_list(result.get("missing_information"))
	checks = _coerce_string_list(result.get("checks"))
	warnings = _coerce_string_list(result.get("warnings"))
	ready = bool(result.get("ready")) and not missing_information and bool(recommended_tool)

	if recommended_tool in {"insert", "save"}:
		values = payload.get("values")
		if not isinstance(values, dict):
			payload["values"] = {}
			ready = False
			warnings.append("DocumentPlanner did not return an object in payload.values.")

	if recommended_tool == "save":
		name = payload.get("name")
		if not isinstance(name, str) or not name.strip():
			ready = False
			missing_information.append("Target document name is missing.")

	if recommended_tool == "set_value":
		fieldname = payload.get("fieldname")
		name = payload.get("name")
		if not isinstance(fieldname, str) or not fieldname.strip():
			ready = False
			missing_information.append("Target fieldname is missing.")
		if "value" not in payload:
			ready = False
			missing_information.append("Target field value is missing.")
		if not isinstance(name, str) or not name.strip():
			ready = False
			missing_information.append("Target document name is missing.")

	doctype = payload.get("doctype")
	if recommended_tool and (not isinstance(doctype, str) or not doctype.strip()):
		ready = False
		missing_information.append("Target DocType is missing.")

	if raw_output and not result:
		warnings.append("DocumentPlanner returned invalid JSON.")

	return {
		"ready": ready,
		"recommended_tool": recommended_tool,
		"payload": payload,
		"reason": reason_text,
		"missing_information": list(dict.fromkeys(missing_information)),
		"checks": checks,
		"warnings": list(dict.fromkeys(warnings)),
	}


class ask_alyfToolset:
	def __init__(self, runtime: ask_alyfRuntime, settings=None):
		self.runtime = runtime
		self.settings = settings
		self._source_code_analyzer = None
		self._document_planner = None

	def _get_settings(self):
		if self.settings is None:
			self.settings = tools.get_settings()
		return self.settings

	def _get_source_code_analyzer(self):
		if self._source_code_analyzer is None:
			self._source_code_analyzer = SourceCodeAnalyzer(
				runtime=self.runtime,
				settings=self._get_settings(),
				toolset=self,
			)
		return self._source_code_analyzer

	def _get_document_planner(self):
		if self._document_planner is None:
			self._document_planner = DocumentPlanner(
				runtime=self.runtime,
				settings=self._get_settings(),
				toolset=self,
			)
		return self._document_planner

	def _proposal(
		self,
		kind: str,
		tool: str,
		summary: str,
		reason: str = "",
		*,
		validation_error_status: str,
		prepared_status: str,
		requires_confirmation: bool = True,
		**payload: Any,
	) -> dict[str, Any]:
		"""Create a pending operation proposal and append it to the list."""
		validation_error = tools.validate_pending_operation_payload(kind, tool, payload)
		if validation_error:
			self.runtime.emit_status(validation_error_status)
			return {
				"success": False,
				"requires_confirmation": False,
				"error": validation_error,
			}

		proposal = {
			"kind": kind,
			"tool": tool,
			"summary": summary,
			"reason": reason,
			"requires_confirmation": bool(requires_confirmation),
			"payload": payload,
			"call_id": uuid4().hex,
		}
		self.runtime.pending_operations.append(proposal)
		self.runtime.emit_status(prepared_status)
		return {
			"success": True,
			"requires_confirmation": bool(requires_confirmation),
			"proposal": proposal,
		}

	def _backend_proposal(
		self,
		tool: str,
		summary: str,
		reason: str = "",
		*,
		validation_error_status: str,
		prepared_status: str,
		**payload: Any,
	) -> dict[str, Any]:
		return self._proposal(
			tools.OPERATION_KIND_BACKEND,
			tool,
			summary,
			reason,
			validation_error_status=validation_error_status,
			prepared_status=prepared_status,
			requires_confirmation=True,
			**payload,
		)

	def _frontend_proposal(
		self,
		tool: str,
		summary: str,
		reason: str = "",
		*,
		validation_error_status: str,
		prepared_status: str,
		requires_confirmation: bool | None = None,
		**payload: Any,
	) -> dict[str, Any]:
		if requires_confirmation is None:
			requires_confirmation = tools.get_frontend_action_requires_confirmation(tool)
		return self._proposal(
			tools.OPERATION_KIND_FRONTEND,
			tool,
			summary,
			reason,
			validation_error_status=validation_error_status,
			prepared_status=prepared_status,
			requires_confirmation=requires_confirmation,
			**payload,
		)

	def get_list(
		self,
		doctype: str,
		fields: str | list[str] | None = None,
		filters: dict[str, Any] | list[Any] | None = None,
		order_by: str | None = None,
		limit: int = 20,
		group_by: str | None = None,
	) -> list[dict[str, Any]]:
		"""List documents with filters, fields, ordering, and optional grouping.

		Args:
			doctype: The DocType to query.
			fields: Optional field name or field list to return.
			filters: Optional Frappe filters.
			order_by: Optional ordering expression.
			limit: Maximum number of rows to return.
			group_by: Optional SQL group by expression.

		Returns:
			A list of matching documents.
		"""
		self.runtime.emit_status(_("Fetching list..."))
		return tools.get_list(
			doctype=doctype,
			fields=fields,
			filters=filters,
			order_by=order_by,
			limit=limit,
			group_by=group_by,
		)

	def get_count(
		self,
		doctype: str,
		filters: dict[str, Any] | list[Any] | None = None,
	) -> int:
		"""Count documents that match the given filters.

		Args:
			doctype: The DocType to query.
			filters: Optional Frappe filters.

		Returns:
			The number of matching documents.
		"""
		self.runtime.emit_status(_("Counting documents..."))
		return tools.get_count(doctype=doctype, filters=filters)

	def get(
		self,
		doctype: str,
		name: str | None = None,
		filters: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		"""Read a single document by name or filters.

		Args:
			doctype: The DocType to query.
			name: Optional document name.
			filters: Optional filters when name is not known.

		Returns:
			The matching document.
		"""
		self.runtime.emit_status(_("Fetching document..."))
		return tools.get_document(doctype=doctype, name=name, filters=filters)

	def get_value(
		self,
		doctype: str,
		fieldname: str | list[str],
		filters: dict[str, Any] | list[Any] | str | None = None,
	) -> Any:
		"""Read one or more field values from a document.

		Args:
			doctype: The DocType to query.
			fieldname: One field name or a list of field names.
			filters: Name or filters to identify the record.

		Returns:
			The requested value or values.
		"""
		self.runtime.emit_status(_("Fetching value..."))
		return tools.get_value(doctype=doctype, fieldname=fieldname, filters=filters)

	def get_single_value(self, doctype: str, field: str) -> Any:
		"""Read a field value from a Single DocType.

		Args:
			doctype: The Single DocType to query.
			field: The field name to read.

		Returns:
			The field value.
		"""
		self.runtime.emit_status(_("Fetching single value..."))
		return tools.get_single_value(doctype=doctype, field=field)

	def get_meta(self, doctype: str) -> dict[str, Any]:
		"""Inspect metadata, fields, and permissions for a DocType.

		Args:
			doctype: The DocType to inspect.

		Returns:
			A metadata dictionary for the DocType.
		"""
		self.runtime.emit_status(_("Loading metadata..."))
		return tools.get_meta(doctype=doctype)

	def has_permission(self, doctype: str, docname: str, perm_type: str = "read") -> dict[str, bool]:
		"""Check whether the current user has a specific permission on a document.

		Args:
			doctype: The DocType to check.
			docname: The document name.
			perm_type: The permission type to evaluate.

		Returns:
			A dictionary containing the boolean permission result.
		"""
		self.runtime.emit_status(_("Checking permissions..."))
		return tools.has_permission(doctype=doctype, docname=docname, perm_type=perm_type)

	def get_doc_permissions(self, doctype: str, docname: str) -> dict[str, Any]:
		"""Get the evaluated permission map for a document.

		Args:
			doctype: The DocType to check.
			docname: The document name.

		Returns:
			The evaluated permission dictionary.
		"""
		self.runtime.emit_status(_("Evaluating permissions..."))
		return tools.get_doc_permissions(doctype=doctype, docname=docname)

	def list_accessible_doctypes(self, permission_type: str = "read") -> list[str]:
		"""List DocTypes that the current user can access.

		Args:
			permission_type: The permission type to test.

		Returns:
			A list of DocType names.
		"""
		self.runtime.emit_status(_("Listing accessible DocTypes..."))
		return tools.list_accessible_doctypes(permission_type=permission_type)

	def list_accessible_reports(self) -> list[dict[str, Any]]:
		"""List reports that the current user can access.

		Returns:
			A list of report metadata dictionaries.
		"""
		self.runtime.emit_status(_("Listing accessible reports..."))
		return tools.list_accessible_reports()

	def translate_ui_labels(
		self,
		labels: list[str],
		language: str | None = None,
	) -> dict[str, Any]:
		"""Translate UI labels so responses match what the user sees on screen.

		Use this whenever request context language is not English before mentioning
		button names, tab names, DocType labels, field labels, menu items, or status labels.

		Args:
			labels: English UI labels to translate.
			language: Optional target language code (defaults to request context language).

		Returns:
			A dictionary with the resolved language and translated labels.
		"""
		self.runtime.emit_status(_("Translating UI labels..."))
		request_language = self.runtime.request_context.get("lang") or self.runtime.request_context.get(
			"locale"
		)
		return tools.translate_ui_labels(labels=labels, language=language or request_language)

	def search_code(self, query: str, relative_path: str = "", limit: int = 20) -> list[dict[str, Any]]:
		"""Search installed app code for matching text.

		Args:
			query: The text to search for.
			relative_path: Optional installed-app-relative path. Providing at least the app name will make it much faster.
			limit: Maximum number of matches to return.

		Returns:
			A list of code search matches.
		"""
		self.runtime.emit_status(_("Searching code..."))
		return tools.search_code(query=query, relative_path=relative_path, limit=limit)

	def read_code_file(self, path: str, start_line: int = 1, end_line: int = 200) -> dict[str, Any]:
		"""Read a file from installed app code.

		Args:
			path: Bench-relative path inside an installed app, such as apps/my_app/my_app/module.py.
			start_line: First line to include.
			end_line: Last line to include.

		Returns:
			The selected file content and line range.
		"""
		self.runtime.emit_status(_("Reading code file..."))
		return tools.read_code_file(path=path, start_line=start_line, end_line=end_line)

	def ls(
		self,
		app_name: str,
		relative_path: str = "",
		recursive: bool = False,
		include_hidden: bool = False,
		limit: int = 200,
	) -> dict[str, Any]:
		"""List files or directories in an installed app, similar to Debian ls.

		Args:
			app_name: The installed app name.
			relative_path: Optional path inside the app.
			recursive: Whether to include nested descendants.
			include_hidden: Whether to include hidden files and folders.
			limit: Maximum number of entries to return.

		Returns:
			A directory listing payload.
		"""
		self.runtime.emit_status(_("Listing code files..."))
		return tools.ls(
			app_name=app_name,
			relative_path=relative_path,
			recursive=recursive,
			include_hidden=include_hidden,
			limit=limit,
		)

	def find(
		self,
		app_name: str,
		name_pattern: str = "*",
		relative_path: str = "",
		entry_type: str = "any",
		include_hidden: bool = False,
		limit: int = 200,
	) -> dict[str, Any]:
		"""Find files or directories in an installed app, similar to Debian find.

		Args:
			app_name: The installed app name.
			name_pattern: Shell-style filename pattern, such as *.py.
			relative_path: Optional path inside the app to search from.
			entry_type: One of any, file, or directory.
			include_hidden: Whether to include hidden files and folders.
			limit: Maximum number of matches to return.

		Returns:
			A find-style search payload.
		"""
		self.runtime.emit_status(_("Finding code paths..."))
		return tools.find(
			app_name=app_name,
			name_pattern=name_pattern,
			relative_path=relative_path,
			entry_type=entry_type,
			include_hidden=include_hidden,
			limit=limit,
		)

	def grep(
		self,
		app_name: str,
		query: str,
		relative_path: str = "",
		file_pattern: str = "*",
		case_sensitive: bool = False,
		include_hidden: bool = False,
		limit: int = 50,
	) -> dict[str, Any]:
		"""Search file contents in an installed app, similar to Debian grep.

		Args:
			app_name: The installed app name.
			query: The text to search for.
			relative_path: Optional path inside the app to search from.
			file_pattern: Optional shell-style filename filter, such as *.py.
			case_sensitive: Whether matching should be case-sensitive.
			include_hidden: Whether to include hidden files and folders.
			limit: Maximum number of matches to return.

		Returns:
			A grep-style search payload.
		"""
		self.runtime.emit_status(_("Searching file contents..."))
		return tools.grep(
			app_name=app_name,
			query=query,
			relative_path=relative_path,
			file_pattern=file_pattern,
			case_sensitive=case_sensitive,
			include_hidden=include_hidden,
			limit=limit,
		)

	async def source_code_analyzer(self, question: str, relative_path: str = "") -> dict[str, Any]:
		"""Analyze installed app source code via a restricted specialist agent.

		Use this for code questions that require searching, reading, and interpreting
		multiple files. The specialist only has access to read-only code tools.

		Args:
			question: The code question to investigate.
			relative_path: Optional installed-app-relative path to narrow the search.

		Returns:
			A structured summary with an answer and supporting evidence.
		"""
		self.runtime.emit_status(_("Delegating code analysis..."))
		return await self._get_source_code_analyzer().analyze(
			question=question,
			relative_path=relative_path,
		)

	def get_file_id(
		self,
		reference_doctype: str,
		reference_name: str,
		reference_field: str = "",
		file_url: str = "",
		file_name: str = "",
	) -> str:
		"""Resolve a unique File ID from attachment reference filters.

		Args:
			reference_doctype: The DocType the file is attached to.
			reference_name: The document name the file is attached to.
			reference_field: Optional attachment field name.
			file_url: Optional file URL to disambiguate matches.
			file_name: Optional file name to disambiguate matches.

		Returns:
			The matching File ID.
		"""
		self.runtime.emit_status(_("Resolving file ID..."))
		return tools.get_file_id(
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			reference_field=reference_field,
			file_url=file_url,
			file_name=file_name,
		)

	def read_file_record(self, file_id: str) -> dict[str, Any]:
		"""Read the content of a File record the user can access.

		Args:
			file_id: The File ID (`name`) to read.

		Returns:
			The file metadata and content.
		"""
		self.runtime.emit_status(_("Reading file..."))
		return tools.read_file_record(file_id=file_id)

	async def extract_document_data(self, file_id: str, extraction_prompt: str = "") -> dict[str, Any]:
		"""Extract structured data from a PDF or image file using vision AI.

		Call this tool when a user uploads or references a document (invoice, receipt,
		contract, etc.) and wants to extract information from it. The file is rendered
		as images and sent to a vision-capable model that reads text, tables, and layouts.

		Supports PDF files (up to 10 pages) and images (PNG, JPG, GIF, WebP).

		Args:
			file_id: The File ID (`name`) to process.
			extraction_prompt: Optional instructions for what data to extract.
				If omitted, a general-purpose extraction prompt is used.

		Returns:
			A dictionary with the file ID, file name, number of pages processed,
			and the extracted data as a JSON object.
		"""
		self.runtime.emit_status(_("Extracting document data..."))
		result = await tools.extract_document_data(file_id=file_id, extraction_prompt=extraction_prompt)
		self.runtime.remember_document_extraction(result, extraction_prompt=extraction_prompt)
		return result

	def get_print(
		self,
		doctype: str,
		name: str,
		print_format: str = "",
		letterhead: str = "",
		language: str = "",
	) -> dict[str, Any]:
		"""Generate a PDF print of a document and attach it to the conversation.

		Uses the default print format and default letter head unless overridden.
		Permissions are enforced automatically by the tool.

		The generated PDF is automatically shown to the user as a clickable file
		attachment in the conversation UI. Do not repeat the file name, URL, or
		link in your text — just confirm briefly what was printed.

		Args:
			doctype: The DocType of the document to print.
			name: The document name.
			print_format: Optional print format name. Defaults to the DocType's default.
			letterhead: Optional letter head name. Defaults to the site's default.
			language: Optional language code for the print. Defaults to the document's
				language field if it exists, otherwise the current session language.

		Returns:
			A dictionary with the generated file metadata (name, file_name, file_url).
		"""
		self.runtime.emit_status(_("Generating print..."))
		file_entry = tools.get_print(
			doctype=doctype,
			name=name,
			conversation_name=self.runtime.conversation_name,
			print_format=print_format,
			letterhead=letterhead,
			language=language,
		)
		self.runtime.remember_attached_file(file_entry)
		return file_entry

	def run_read_only_sql(self, query: str) -> list[dict[str, Any]]:
		"""Run a read-only SQL query when the current user is allowed to do so.

		Args:
			query: A single read-only SQL query.

		Returns:
			The SQL result rows.
		"""
		self.runtime.emit_status(_("Running SQL query..."))
		return tools.run_read_only_sql(query=query)

	def get_app_version(self, app_name: str) -> str:
		"""Read the installed version for an app.

		Args:
			app_name: The installed app name.

		Returns:
			The app version string.
		"""
		self.runtime.emit_status(_("Reading app version..."))
		return tools.get_app_version(app_name=app_name)

	def read_github_releases(self, app_name: str, limit: int = 5) -> list[dict[str, Any]]:
		"""Read recent GitHub releases for an installed app.

		Args:
			app_name: The installed app name.
			limit: Maximum number of releases to return.

		Returns:
			A list of release dictionaries.
		"""
		self.runtime.emit_status(_("Reading GitHub releases..."))
		return tools.read_github_releases(app_name=app_name, limit=limit)

	def read_documentation_page(self, app_name: str, relative_path: str = "") -> dict[str, Any]:
		"""Fetch a documentation page for an installed app.

		Args:
			app_name: The installed app name.
			relative_path: Optional path relative to the configured docs URL.

		Returns:
			A documentation payload containing the page content.
		"""
		self.runtime.emit_status(_("Reading documentation..."))
		return tools.read_documentation_page(app_name=app_name, relative_path=relative_path)

	def read_skill(self, name: str) -> dict[str, str]:
		"""Read the full content of an Ask ALYF skill available to the current user.

		Use this when the instructions list a skill that seems relevant. Pass the
		exact skill `name` shown in that list.

		Args:
			name: The exact Ask ALYF Skill name.

		Returns:
			A dictionary containing the skill name, title, and markdown description.
		"""
		self.runtime.emit_status(_("Reading skill..."))
		skill_doc = get_accessible_skill_doc(name)
		return {
			"name": skill_doc.name,
			"title": skill_doc.title,
			"description": skill_doc.description or "",
		}

	def write_skill(
		self,
		title: str,
		description: str,
		roles: list[str],
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose creating a reusable Ask ALYF skill.

		Use this when the user wants to save durable instructions that can later be
		loaded with `read_skill`.

		Args:
			title: The skill title shown to users.
			description: The markdown skill content.
			roles: Roles that should be allowed to use the skill.
				Accepts a list or a comma/newline-separated string.
			reason: Optional explanation of why the skill should be created.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		clean_title = (title or "").strip()
		clean_description = (description or "").strip()
		clean_roles = parse_newline_list(roles)

		if not clean_title:
			frappe.throw(_("Skill title is required."))
		if not clean_description:
			frappe.throw(_("Skill description is required."))
		if not clean_roles:
			frappe.throw(_("At least one role is required."))

		return self._backend_proposal(
			"insert",
			_("Create skill '{0}'").format(clean_title),
			reason,
			validation_error_status=_("Skill proposal needs correction."),
			prepared_status=_("Prepared skill proposal."),
			doctype="Ask ALYF Skill",
			values={
				"title": clean_title,
				"description": clean_description,
				"roles": [{"role": role} for role in clean_roles],
			},
		)

	async def document_planner(
		self,
		user_request: str,
		doctype: str = "",
		operation: str = "insert",
		name: str = "",
		values_hint: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		"""Plan a document create or update flow via a read-only specialist agent.

		Use this before non-trivial insert, save, or set_value operations that need
		schema inspection or Link resolution. The specialist only has read tools.

		Args:
			user_request: The user's requested outcome in natural language.
			doctype: The target DocType to plan for.
			operation: One of insert, save, or set_value.
			name: Optional existing document name for save or set_value.
			values_hint: Optional partial field values already known.

		Returns:
			A structured plan with readiness, payload, missing information, and checks.
		"""
		self.runtime.emit_status(_("Planning document change..."))
		return await self._get_document_planner().plan(
			user_request=user_request,
			doctype=doctype,
			operation=operation,
			name=name,
			values_hint=values_hint,
		)

	def insert(self, doctype: str, values: dict[str, Any], reason: str = "") -> dict[str, Any]:
		"""Propose creating a new document. Use get_meta first to know the schema.
		For child tables, values must be a list of row objects.

		Args:
			doctype: The DocType to create.
			values: The field values for the new document.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"insert",
			_("Create {0}").format(_(doctype)),
			reason,
			validation_error_status=_("Create proposal needs correction."),
			prepared_status=_("Prepared create proposal."),
			doctype=doctype,
			values=values,
		)

	def batch_insert(self, doctype: str, records: list[dict[str, Any]], reason: str = "") -> dict[str, Any]:
		"""Propose creating multiple documents of the same DocType.
		Use get_meta first to know the schema for each record.
		For child tables, each record must use a list of row objects.

		Args:
			doctype: The DocType to create.
			records: A list of field-value dictionaries, one per new document.
			reason: Optional explanation of why this batch change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		record_count = len(records) if isinstance(records, list) else 0
		return self._backend_proposal(
			"batch_insert",
			_("Create {0} {1} records").format(record_count, _(doctype)),
			reason,
			validation_error_status=_("Batch create proposal needs correction."),
			prepared_status=_("Prepared batch create proposal."),
			doctype=doctype,
			records=records,
		)

	def save(
		self,
		doctype: str,
		name: str,
		values: dict[str, Any],
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose updating an existing document.
		For child tables, values must be a list of row objects.

		Args:
			doctype: The DocType to update.
			name: The document name.
			values: The fields to update.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"save",
			_("Update {0} {1}").format(_(doctype), name),
			reason,
			validation_error_status=_("Update proposal needs correction."),
			prepared_status=_("Prepared update proposal."),
			doctype=doctype,
			name=name,
			values=values,
		)

	def set_value(
		self,
		doctype: str,
		name: str,
		fieldname: str,
		value: Any,
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose setting a single field on a document.

		Args:
			doctype: The DocType to update.
			name: The document name.
			fieldname: The field to set.
			value: The new value.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"set_value",
			_("Set {0} on {1} {2}").format(fieldname, _(doctype), name),
			reason,
			validation_error_status=_("Set value proposal needs correction."),
			prepared_status=_("Prepared set value proposal."),
			doctype=doctype,
			name=name,
			fieldname=fieldname,
			value=value,
		)

	def submit(self, doctype: str, name: str, reason: str = "") -> dict[str, Any]:
		"""Propose submitting a document.

		Args:
			doctype: The DocType to submit.
			name: The document name.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"submit",
			_("Submit {0} {1}").format(_(doctype), name),
			reason,
			validation_error_status=_("Submit proposal needs correction."),
			prepared_status=_("Prepared submit proposal."),
			doctype=doctype,
			name=name,
		)

	def cancel(self, doctype: str, name: str, reason: str = "") -> dict[str, Any]:
		"""Propose cancelling a document.

		Args:
			doctype: The DocType to cancel.
			name: The document name.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"cancel",
			_("Cancel {0} {1}").format(_(doctype), name),
			reason,
			validation_error_status=_("Cancel proposal needs correction."),
			prepared_status=_("Prepared cancel proposal."),
			doctype=doctype,
			name=name,
		)

	def amend(self, doctype: str, name: str, reason: str = "") -> dict[str, Any]:
		"""Propose amending a cancelled document.

		Args:
			doctype: The DocType to amend.
			name: The document name.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"amend",
			_("Amend {0} {1}").format(_(doctype), name),
			reason,
			validation_error_status=_("Amend proposal needs correction."),
			prepared_status=_("Prepared amend proposal."),
			doctype=doctype,
			name=name,
		)

	def delete(self, doctype: str, name: str, reason: str = "") -> dict[str, Any]:
		"""Propose deleting a document.

		Args:
			doctype: The DocType to delete.
			name: The document name.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"delete",
			_("Delete {0} {1}").format(_(doctype), name),
			reason,
			validation_error_status=_("Delete proposal needs correction."),
			prepared_status=_("Prepared delete proposal."),
			doctype=doctype,
			name=name,
		)

	def rename_doc(
		self,
		doctype: str,
		name: str,
		new_name: str,
		merge: bool = False,
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose renaming a document.

		Args:
			doctype: The DocType to rename.
			name: The current document name.
			new_name: The target document name.
			merge: Whether to merge into an existing target.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"rename_doc",
			_("Rename {0} {1} to {2}").format(_(doctype), name, new_name),
			reason,
			validation_error_status=_("Rename proposal needs correction."),
			prepared_status=_("Prepared rename proposal."),
			doctype=doctype,
			name=name,
			new_name=new_name,
			merge=merge,
		)

	def attach_file(
		self,
		doctype: str,
		name: str,
		file_id: str,
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose attaching an existing file to a document.

		Args:
			doctype: The DocType to update.
			name: The document name.
			file_id: The File ID (`name`) to attach.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		file_doc = tools._get_accessible_file_doc(file_id=file_id)
		return self._backend_proposal(
			"attach_file",
			_("Attach file {0} to {1} {2}").format(
				_build_file_markdown_link(file_doc),
				_(doctype),
				name,
			),
			reason,
			validation_error_status=_("Attach file proposal needs correction."),
			prepared_status=_("Prepared attach file proposal."),
			doctype=doctype,
			name=name,
			file_id=file_id,
		)

	def run_whitelisted_method(
		self,
		method: str,
		args: dict[str, Any] | None = None,
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose calling a whitelisted method.

		Args:
			method: The dotted Python path of the whitelisted method.
			args: Optional keyword arguments for the method.
			reason: Optional explanation of why this change is needed.

		Returns:
			A pending action proposal that requires confirmation.
		"""
		return self._backend_proposal(
			"run_method",
			_("Call {0}").format(method),
			reason,
			validation_error_status=_("Method call proposal needs correction."),
			prepared_status=_("Prepared method call proposal."),
			method=method,
			args=args or {},
		)

	def set_route(self, route: list[str], reason: str = "") -> dict[str, Any]:
		"""Propose navigating to a Desk route on the frontend.

		Args:
			route: Route parts used by frappe.set_route.
			reason: Optional explanation of why this navigation helps the user.

		Returns:
			A pending frontend operation proposal.
		"""
		route_label = "/".join(part for part in (route or []) if isinstance(part, str))
		return self._frontend_proposal(
			"set_route",
			_("Navigate to {0}").format(route_label or _("target route")),
			reason,
			validation_error_status=_("Route action needs correction."),
			prepared_status=_("Prepared route action."),
			route=route,
		)

	def new_doc(
		self,
		doctype: str,
		route_options: dict[str, Any] | None = None,
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose opening a new document form in the frontend.

		Args:
			doctype: The target DocType for frappe.new_doc.
			route_options: Optional route options to prefill form values.
			reason: Optional explanation of why this action helps the user.

		Returns:
			A pending frontend operation proposal.
		"""
		return self._frontend_proposal(
			"new_doc",
			_("Open new {0}").format(_(doctype)),
			reason,
			validation_error_status=_("New document action needs correction."),
			prepared_status=_("Prepared new document action."),
			doctype=doctype,
			route_options=route_options or {},
		)

	def scroll_to_field(self, fieldname: str, reason: str = "") -> dict[str, Any]:
		"""Propose scrolling to a field on the active form.

		Args:
			fieldname: The fieldname to scroll to.
			reason: Optional explanation of why this action helps the user.

		Returns:
			A pending frontend operation proposal.
		"""
		return self._frontend_proposal(
			"scroll_to_field",
			_("Scroll to field {0}").format(fieldname),
			reason,
			validation_error_status=_("Scroll action needs correction."),
			prepared_status=_("Prepared scroll action."),
			fieldname=fieldname,
		)

	def frm_set_value(
		self,
		fieldname: str,
		value: Any,
		doctype: str | None = None,
		docname: str | None = None,
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose setting a field value on the active frontend form.

		Args:
			fieldname: The target fieldname.
			value: The value to apply.
			doctype: Optional expected active form DocType.
			docname: Optional expected active form document name.
			reason: Optional explanation of why this action helps the user.

		Returns:
			A pending frontend operation proposal.
		"""
		payload: dict[str, Any] = {"fieldname": fieldname, "value": value}
		if doctype:
			payload["doctype"] = doctype
		if docname:
			payload["docname"] = docname

		return self._frontend_proposal(
			"frm_set_value",
			_("Set field {0} on current form").format(fieldname),
			reason,
			validation_error_status=_("Set field action needs correction."),
			prepared_status=_("Prepared set field action."),
			**payload,
		)

	def frm_add_child(
		self,
		fieldname: str,
		values: dict[str, Any] | None = None,
		doctype: str | None = None,
		docname: str | None = None,
		reason: str = "",
	) -> dict[str, Any]:
		"""Propose adding a child table row on the active frontend form.

		Args:
			fieldname: The child table fieldname.
			values: Optional row values for the new child row.
			doctype: Optional expected active form DocType.
			docname: Optional expected active form document name.
			reason: Optional explanation of why this action helps the user.

		Returns:
			A pending frontend operation proposal.
		"""
		payload: dict[str, Any] = {"fieldname": fieldname, "values": values or {}}
		if doctype:
			payload["doctype"] = doctype
		if docname:
			payload["docname"] = docname

		return self._frontend_proposal(
			"frm_add_child",
			_("Add a row to {0} on current form").format(fieldname),
			reason,
			validation_error_status=_("Add child row action needs correction."),
			prepared_status=_("Prepared add child row action."),
			**payload,
		)

	def show_chart(
		self,
		frappe_charts: list[dict[str, Any]],
		reason: str = "",
	) -> dict[str, Any]:
		"""Show one or more Frappe Charts under this assistant message.

		The desk creates the DOM element; each item in `frappe_charts` is the `options`
		argument to `new frappe.Chart(container, options)` (Frappe Charts on the client).

		Shape (one object per chart):
		- `type`: bar, line, scatter, pie, percentage, donut, or axis-mixed
		- `data`: `{ "labels": [...], "datasets": [ { "values": [...], "name"?: str, "type"?: "bar"|"line" } ] }`
		  — every `values` list must be the same length as `labels`
		- Optional: `title`, `height` (0 for default; if set, at least 240 — Frappe Charts reserves ~130px chrome),
		  `colors` (array; empty for defaults),
		  `barOptions`, `lineOptions`, `axisOptions` (see Frappe Charts docs)

		Args:
			frappe_charts: One or more chart option objects.
			reason: Optional short note for the user.

		Returns:
			A pending frontend operation proposal (auto-executed in the browser).
		"""
		count = len(frappe_charts) if isinstance(frappe_charts, list) else 0
		summary = _("Show chart") if count == 1 else _("Show {0} charts").format(count)
		return self._frontend_proposal(
			"show_chart",
			summary,
			reason,
			validation_error_status=_("Chart action needs correction."),
			prepared_status=_("Prepared chart display."),
			requires_confirmation=False,
			frappe_charts=frappe_charts,
		)


class SourceCodeAnalyzer:
	def __init__(self, runtime: ask_alyfRuntime, settings, toolset: ask_alyfToolset):
		self.runtime = runtime
		self.settings = settings
		self.tool_defs = [
			toolset.search_code,
			toolset.read_code_file,
			toolset.ls,
			toolset.find,
			toolset.grep,
		]
		self.instructions = """
You are SourceCodeAnalyzer, an internal Ask ALYF specialist for installed app code.

You can only use the provided source-code tools.

Rules:
- Search or list first, then read the smallest relevant file ranges.
- Prefer the narrowest path scope available.
- Do not answer from memory when the tools can verify it.
- If evidence is incomplete or ambiguous, say so clearly.
- Include bench-relative paths and line ranges in evidence whenever possible.
- Return a compact JSON object with keys:
  - `answer` (string)
  - `summary` (string)
  - `evidence` (list of objects with `path`, optional `start_line`, optional `end_line`, and optional `note`)
  - `uncertainty` (string)
  - `searched_paths` (list of strings)

Return only a valid JSON object.
Do not wrap the JSON in markdown fences.
Do not add explanatory prose before or after the JSON.
""".strip()
		self.agent = None

	async def _get_agent(self):
		if self.agent is None:
			self.agent = await _create_internal_agent_async(
				settings=self.settings,
				name="SourceCodeAnalyzer",
				instructions=self.instructions,
				tool_defs=self.tool_defs,
			)
		return self.agent

	async def analyze(self, question: str, relative_path: str = "") -> dict[str, Any]:
		clean_question = (question or "").strip()
		if not clean_question:
			return _normalize_source_code_analysis({})

		history_context = _build_specialist_history_context(self.runtime)
		request_context = frappe.as_json(getattr(self.runtime, "request_context", {}), indent=2)
		path_hint = (relative_path or "").strip() or _("all installed app code roots available to you")
		prompt = "\n".join(
			[
				"Investigate this source-code question for the parent Ask ALYF agent.",
				f"Question: {clean_question}",
				f"Preferred scope: {path_hint}",
				"Use the preferred scope when it is specific enough.",
				"",
				"Request context JSON:",
				request_context,
				"",
				history_context or _("No recent conversation context."),
			]
		)
		agent = await self._get_agent()
		trace = await agent.run_async(prompt)
		raw_output = str(trace.final_output or "").strip()
		parsed = _parse_json_object_output(raw_output) or {}
		return _normalize_source_code_analysis(parsed, raw_output=raw_output)


class DocumentPlanner:
	def __init__(self, runtime: ask_alyfRuntime, settings, toolset: ask_alyfToolset):
		self.runtime = runtime
		self.settings = settings
		self.tool_defs = [
			toolset.get_list,
			toolset.get,
			toolset.get_value,
			toolset.get_single_value,
			toolset.get_meta,
			toolset.has_permission,
			toolset.get_doc_permissions,
			toolset.list_accessible_doctypes,
		]
		self.instructions = f"""
You are DocumentPlanner, an internal Ask ALYF specialist for planning Frappe document changes.

You only have read-only access to metadata and documents. You never execute writes.

You may only plan these operations:
- `insert`
- `save`
- `set_value`

Rules:
- Always inspect `get_meta` before planning `insert`, `save`, or `set_value`.
- Use the read tools to resolve Link targets or confirm existing values when possible.
- Never invent document names, Link targets, or required values.
- Treat `values_hint` as tentative until it is confirmed by the user or by a read tool.
- If information is missing, set `ready` to `false` and list each missing item in `missing_information`.
- The `payload` must match the parent tool signature for the recommended operation.
- Return a JSON object with keys:
  - `ready` (boolean)
  - `recommended_tool` (`insert`, `save`, or `set_value`)
  - `payload` (object)
  - `reason` (string)
  - `missing_information` (list of strings)
  - `checks` (list of strings)
  - `warnings` (list of strings)

{SPECIALIST_JSON_OUTPUT_INSTRUCTION}
""".strip()
		self.agent = None

	async def _get_agent(self):
		if self.agent is None:
			self.agent = await _create_internal_agent_async(
				settings=self.settings,
				name="DocumentPlanner",
				instructions=self.instructions,
				tool_defs=self.tool_defs,
			)
		return self.agent

	async def plan(
		self,
		user_request: str,
		doctype: str = "",
		operation: str = "insert",
		name: str = "",
		values_hint: dict[str, Any] | None = None,
	) -> dict[str, Any]:
		clean_doctype = (doctype or "").strip()
		clean_operation = (operation or "").strip().lower() or "insert"
		clean_name = (name or "").strip()
		clean_request = (user_request or "").strip()
		clean_values_hint = values_hint if isinstance(values_hint, dict) else {}

		if not clean_doctype:
			return _build_document_planner_failure("Target DocType is required.")
		if clean_operation not in ALLOWED_DOCUMENT_PLANNER_TOOLS:
			return _build_document_planner_failure(
				"Operation must be one of insert, save, or set_value.",
				recommended_tool="insert",
				payload={"doctype": clean_doctype},
			)
		if not clean_request:
			return _build_document_planner_failure(
				"User request is required.",
				recommended_tool=clean_operation,
				payload={"doctype": clean_doctype},
			)

		history_context = _build_specialist_history_context(self.runtime)
		request_context = frappe.as_json(getattr(self.runtime, "request_context", {}), indent=2)
		prompt = "\n".join(
			[
				"Plan the next document action for the parent Ask ALYF agent.",
				f"User request: {clean_request}",
				f"Requested operation: {clean_operation}",
				f"Target DocType: {clean_doctype}",
				f"Target document name: {clean_name or '(not provided)'}",
				"",
				"values_hint JSON:",
				frappe.as_json(clean_values_hint, indent=2),
				"",
				"Request context JSON:",
				request_context,
				"",
				history_context or _("No recent conversation context."),
			]
		)
		agent = await self._get_agent()
		trace = await agent.run_async(prompt)
		raw_output = str(trace.final_output or "").strip()
		parsed = _parse_json_object_output(raw_output) or {}
		return _normalize_document_plan(
			parsed,
			default_doctype=clean_doctype,
			default_operation=clean_operation,
			default_name=clean_name,
			raw_output=raw_output,
		)


def _clear_messages_on_tool_error(func):
	"""Wrap a tool callable so that queued Frappe messages are discarded on
	exception.  The error still propagates to the agent framework (so the LLM
	sees it), but the user won't receive a popup."""

	if inspect.iscoroutinefunction(func):

		@functools.wraps(func)
		async def async_wrapper(*args, **kwargs):
			try:
				return await func(*args, **kwargs)
			except Exception:
				frappe.clear_messages()
				raise

		return async_wrapper

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		try:
			return func(*args, **kwargs)
		except Exception:
			frappe.clear_messages()
			raise

	return wrapper


class ask_alyfAgentRunner:
	def __init__(self, runtime: ask_alyfRuntime):
		self.runtime = runtime
		self.settings = tools.get_settings()
		self.toolset = ask_alyfToolset(runtime, settings=self.settings)
		self.agent = TinyAgent.create(
			AgentConfig(
				name="Ask ALYF",
				model_id=self._get_model_id(),
				api_base=(self.settings.base_url or "").strip() or None,
				api_key=self._get_api_key(),
				instructions=self._build_instructions(),
				tools=self._build_tools(),
				model_args={"temperature": 0.2},
			),
		)

	def _get_api_key(self) -> str:
		return _get_api_key_from_settings(self.settings)

	def _get_model_id(self) -> str:
		return _get_model_id_from_settings(self.settings)

	def _can_write_skill(self) -> bool:
		return bool(frappe.has_permission("Ask ALYF Skill", ptype="create"))

	def _build_instructions(self) -> str:
		context = frappe.as_json(self.runtime.request_context, indent=2)
		excluded_doctypes = ", ".join(sorted(tools.get_excluded_doctypes())) or "None"
		available_skills_instruction = build_available_skills_instruction()
		system_prompt = (self.settings.system_prompt or "").strip()
		code_search_usage_instruction = ""
		if self.settings.is_code_search_enabled():
			code_search_usage_instruction = (
				"\n- When code search is enabled, use `source_code_analyzer` for code questions "
				"instead of reasoning from memory."
			)

		base_instructions = f"""
You are Ask ALYF, an ERPNext and Frappe assistant embedded inside the user's desk.

Always follow these rules:
- Adapt your language to the user. Prefer short, direct answers for everyday operational questions. Offer more detail only when the question calls for it or the user asks. Avoid ERP jargon and internal field names in your prose — use the labels the user sees on screen. If the user writes informally, respond in kind.
- Use the available read tools whenever the user asks about instance data, permissions, metadata, code, files, or reports.
- Be concise, accurate, and explicit about uncertainty.
- Respect the current user's permissions. If a tool says something is not allowed, explain that plainly.{code_search_usage_instruction}
- If request context `lang` is not English, always call `translate_ui_labels` before using user-facing UI terms (DocType names, field labels, button labels, tabs, menus, and status labels) in your response.
- Render responses as Markdown when that helps.
- If conversation history includes attachment metadata, use the exact file ID shown there. If you only know a document reference, file name, or file URL, call `get_file_id` first. Never guess or invent a file ID.
- When the user asks about the contents of an attached PDF or image, prefer `extract_document_data`. Use `read_file_record` for text-like files.
- If conversation history includes stored document extraction data, reuse it for follow-up questions instead of re-running extraction unless the user asks for a fresh read.
- If a file tool returns a truncation warning, tell the user clearly that only part of the file was processed.
{available_skills_instruction}

- Current request context (includes `user_roles` for non-Administrator users):
{context}

Mode awareness and behavior:
- The current mode is `{self.runtime.mode}` and is authoritative for this turn.
- `Ask` mode is strictly read-only: write tools are unavailable, so if intent is mutation (create, update, submit, cancel, amend, rename, delete, attach, or a write method), immediately recommend switching to `Agent` mode and do not claim anything was done or queued.
- `Agent` mode supports mutation workflows with write tools while still handling read-only questions with read tools. Every write tool call creates a pending proposal that requires user confirmation before execution. Multiple proposals can be created in a single turn — if the request needs several writes, propose them all now. The user will confirm or reject each one individually.
- Frontend action tools can navigate or adjust the current form in the browser, or display Frappe Charts under the assistant message via `show_chart` (pass `frappe_charts` as a list of chart option objects; validated server-side). See the `show_chart` tool docstring for the options shape.
- Frontend actions with `requires_confirmation` must be confirmed before the browser executes them.
- In `Agent` mode, prefer `document_planner` before non-trivial `insert`, `save`, or `set_value` operations. If it returns `ready=false`, ask the user for the missing information instead of guessing. If it returns `ready=true`, use the matching write tool with the returned payload.
- When the user wants to create multiple documents of the same DocType, prefer `batch_insert` instead of preparing many separate `insert` proposals.
- Before insert or save, call get_meta for the target DocType and follow field types exactly.
- Child table fields (fieldtype Table) must be arrays of row objects, never plain strings.
- Act on clear intent immediately with sensible defaults. Only ask when required information is truly missing and cannot be inferred.
- Never repeat the user's data in your response. The UI shows a detailed preview of every pending write. After calling a write tool, confirm readiness in one sentence.
- When you receive an action result (success, failure, or rejection), confirm the outcome briefly. If a natural follow-up action exists (e.g. submitting a newly created document), proceed with it. Do not ask "would you like me to..." — just do it.
- Excluded DocTypes for Agent mode: {excluded_doctypes}
""".strip()

		if system_prompt:
			return f"{system_prompt}\n\n{base_instructions}"

		return base_instructions

	def _build_tools(self) -> list[Callable[..., Any]]:
		tool_defs: list[Callable[..., Any]] = [
			self.toolset.get_list,
			self.toolset.get_count,
			self.toolset.get,
			self.toolset.get_value,
			self.toolset.get_single_value,
			self.toolset.get_meta,
			self.toolset.has_permission,
			self.toolset.get_doc_permissions,
			self.toolset.list_accessible_doctypes,
			self.toolset.list_accessible_reports,
			self.toolset.translate_ui_labels,
			self.toolset.read_skill,
			self.toolset.set_route,
			self.toolset.new_doc,
			self.toolset.scroll_to_field,
			self.toolset.show_chart,
			self.toolset.get_file_id,
			self.toolset.read_file_record,
			self.toolset.extract_document_data,
			self.toolset.get_print,
			self.toolset.run_read_only_sql,
			self.toolset.get_app_version,
			self.toolset.read_github_releases,
			self.toolset.read_documentation_page,
		]

		if self.settings.is_code_search_enabled():
			tool_defs.append(self.toolset.source_code_analyzer)

		if self.runtime.mode == "Agent":
			if self._can_write_skill():
				tool_defs.append(self.toolset.write_skill)
			tool_defs.extend(
				[
					self.toolset.document_planner,
					self.toolset.insert,
					self.toolset.batch_insert,
					self.toolset.save,
					self.toolset.set_value,
					self.toolset.submit,
					self.toolset.cancel,
					self.toolset.amend,
					self.toolset.delete,
					self.toolset.rename_doc,
					self.toolset.attach_file,
					self.toolset.run_whitelisted_method,
					self.toolset.frm_set_value,
					self.toolset.frm_add_child,
				]
			)

		return [_clear_messages_on_tool_error(fn) for fn in tool_defs]

	def run(self, message: str, conversation_history: list[dict[str, Any]]) -> dict[str, Any]:
		trace = self.agent.run(build_prompt(message, conversation_history))
		return {
			"response": str(trace.final_output or "").strip(),
			"pending_operations": self.runtime.pending_operations,
			"document_extractions": self.runtime.document_extractions,
			"attached_files": self.runtime.attached_files,
		}


def build_prompt(message: str, conversation_history: list[dict[str, Any]]) -> str:
	if not conversation_history:
		return message

	lines = [
		"Use the prior conversation as context when answering the final user message.",
		"",
		"Conversation history:",
	]
	for item in conversation_history:
		lines.extend(_build_history_item_lines(item))

	lines.extend(["", f"User: {message}"])
	return "\n".join(lines)


def _build_document_extraction_history_entry(
	extraction: dict[str, Any],
	*,
	extraction_prompt: str = "",
) -> dict[str, Any]:
	"""Normalize extraction metadata stored on assistant messages."""
	return {
		"file_id": extraction.get("file_id") or extraction.get("name"),
		"file_name": extraction.get("file_name"),
		"pages_processed": extraction.get("pages_processed"),
		"total_pages": extraction.get("total_pages"),
		"truncated": bool(extraction.get("truncated")),
		"warning": extraction.get("warning"),
		"extraction_prompt": extraction_prompt,
		"extracted_data": extraction.get("extracted_data"),
	}


def _build_history_item_lines(item: dict[str, Any]) -> list[str]:
	"""Render one stored message and its metadata into prompt lines."""
	role = (item.get("role") or "user").capitalize()
	content = item.get("content") or ""
	lines = [f"{role}: {content}"]

	metadata = item.get("metadata")
	if not isinstance(metadata, dict):
		return lines

	lines.extend(_build_attachment_metadata_lines(metadata.get("files")))
	lines.extend(_build_document_extraction_lines(metadata.get("document_extractions")))
	return lines


def _build_attachment_metadata_lines(files: Any) -> list[str]:
	"""Render attachment metadata into compact prompt lines."""
	if not isinstance(files, list):
		return []

	lines: list[str] = []
	for file_entry in files:
		if not isinstance(file_entry, dict):
			continue

		parts = []
		file_id = (file_entry.get("name") or file_entry.get("file_id") or "").strip()
		file_name = (file_entry.get("file_name") or "").strip()
		if file_id:
			parts.append(f"id={file_id}")
		if file_name:
			parts.append(f"name={file_name}")
		if parts:
			lines.append(f"Attachment metadata: {', '.join(parts)}")

	return lines


def _build_document_extraction_lines(document_extractions: Any) -> list[str]:
	"""Render persisted document extraction metadata and JSON into prompt lines."""
	if not isinstance(document_extractions, list):
		return []

	lines: list[str] = []
	for extraction in document_extractions:
		if not isinstance(extraction, dict):
			continue

		summary = _build_document_extraction_summary(extraction)
		if summary:
			lines.append(summary)

		extraction_prompt = (extraction.get("extraction_prompt") or "").strip()
		if extraction_prompt:
			lines.append(f"Extraction request: {extraction_prompt}")

		warning = (extraction.get("warning") or "").strip()
		if warning:
			lines.append(f"Extraction warning: {warning}")

		extracted_data_text = _stringify_extracted_data(extraction.get("extracted_data"))
		if extracted_data_text:
			lines.append("Extracted document data (JSON):")
			lines.append(extracted_data_text)

	return lines


def _build_document_extraction_summary(extraction: dict[str, Any]) -> str:
	"""Summarize a stored extraction record for prompt reuse."""
	parts = []
	file_id = (extraction.get("file_id") or extraction.get("name") or "").strip()
	file_name = (extraction.get("file_name") or "").strip()
	pages_processed = extraction.get("pages_processed")
	total_pages = extraction.get("total_pages")
	if file_id:
		parts.append(f"id={file_id}")
	if file_name:
		parts.append(f"name={file_name}")
	if isinstance(pages_processed, int):
		parts.append(f"pages_processed={pages_processed}")
	if isinstance(total_pages, int):
		parts.append(f"total_pages={total_pages}")
	return f"Stored document extraction: {', '.join(parts)}" if parts else ""


def _build_file_markdown_link(file_doc) -> str:
	"""Build a Markdown link for a File document label."""
	file_label = _escape_markdown_link_label(
		(file_doc.file_name or file_doc.name or "").strip() or file_doc.name
	)
	file_url = _quote_markdown_link_destination(file_doc.file_url)
	if not file_url:
		return file_label
	return f"[{file_label}]({file_url})"


def _escape_markdown_link_label(label: str) -> str:
	"""Escape characters that would break a Markdown link label."""
	return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _quote_markdown_link_destination(url: str | None) -> str:
	"""Quote a URL so it is safe inside Markdown link syntax."""
	clean_url = (url or "").strip()
	if not clean_url:
		return ""
	return quote(clean_url, safe="/#:?&=%")


def _stringify_extracted_data(extracted_data: Any) -> str:
	"""Render stored extraction data as JSON text for the prompt."""
	if isinstance(extracted_data, str):
		return extracted_data.strip()
	if extracted_data is None:
		return ""
	return frappe.as_json(extracted_data, indent=2)


def run_message(
	conversation_name: str,
	message: str,
	mode: str,
	request_context: dict[str, Any],
	conversation_history: list[dict[str, Any]],
) -> dict[str, Any]:
	runtime = ask_alyfRuntime(
		conversation_name=conversation_name,
		mode=mode,
		request_context=request_context,
		conversation_history=conversation_history,
	)
	runner = ask_alyfAgentRunner(runtime)
	return runner.run(message, conversation_history)
