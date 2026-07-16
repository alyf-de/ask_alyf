import contextlib
import functools
import inspect
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import frappe
from frappe import _

from ask_alyf.ask_alyf import tools
from ask_alyf.ask_alyf.history import (
	_build_document_extraction_history_entry,
	_build_file_markdown_link,
)
from ask_alyf.ask_alyf.skill_utils import get_accessible_skill_doc
from ask_alyf.ask_alyf.utils import parse_newline_list

# Frappe list filters are a list of filter rows, where each row is a list of
# scalars, e.g. [["Customer", "name", "=", "CUST-001"], ["disabled", "=", 0]].
# Typed (rather than `list[Any]`) so the generated JSON Schema gives the array
# `items` a concrete `type`, which OpenAI strict function-calling requires.
Scalar = str | int | float | bool | None
FrappeFilterList = list[list[Scalar | list[Scalar]]]
_TOOL_DB_LOCK = threading.RLock()


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


class ask_alyfToolset:
	def __init__(self, runtime: ask_alyfRuntime, settings=None):
		self.runtime = runtime
		self.settings = settings

	def _get_settings(self):
		if self.settings is None:
			self.settings = tools.get_settings()
		return self.settings

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
		filters: dict[str, Any] | FrappeFilterList | None = None,
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
		filters: dict[str, Any] | FrappeFilterList | None = None,
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
		filters: dict[str, Any] | FrappeFilterList | str | None = None,
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

	def extract_document_data(self, file_id: str, extraction_prompt: str = "") -> dict[str, Any]:
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
		import asyncio

		result = asyncio.run(
			tools.extract_document_data(file_id=file_id, extraction_prompt=extraction_prompt)
		)
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


@contextlib.contextmanager
def _isolated_db():
	"""Run a block against a private Frappe DB connection.

	LangGraph runs sync tools via `asyncio.to_thread`, which copies the
	calling context (contextvars) into the worker thread. Frappe's
	`frappe.local` is contextvar-backed, so the worker thread inherits the
	same `frappe.local.db` pymysql connection as the agent's main thread.
	pymysql connections are not safe for concurrent use, so parallel tool
	calls (and subagent tool calls) corrupt the shared connection's packet
	stream — surfacing as `InterfaceError(0, '')` / `Packet sequence number
	wrong`.

	This opens a fresh connection bound to the current thread/context, runs
	the block, then closes it and restores the inherited binding. The agent
	thread's own connection is never touched or closed.

	No-op when Frappe isn't initialized (e.g. direct unit-test calls).
	"""
	conf = getattr(frappe.local, "conf", None)
	if not conf or not getattr(conf, "db_name", None):
		yield
		return

	inherited_db = getattr(frappe.local, "db", None)

	frappe.connect(set_admin_as_user=False)
	try:
		yield
		frappe.db.commit()
	except Exception:
		with contextlib.suppress(Exception):
			frappe.db.rollback()
		raise
	finally:
		with contextlib.suppress(Exception):
			frappe.local.db.close()
		frappe.local.db = inherited_db


def clear_messages_on_tool_error(func):
	"""Wrap a tool so each call runs on a private DB connection and queued
	Frappe messages are discarded on exception.

	The private connection (see `_isolated_db`) is what makes tool calls
	safe under LangGraph's threaded tool executor; the error handling keeps
	user-facing popups from firing when a tool fails inside the agent loop.
	"""

	if inspect.iscoroutinefunction(func):

		@functools.wraps(func)
		async def async_wrapper(*args, **kwargs):
			try:
				with _TOOL_DB_LOCK:
					with _isolated_db():
						return await func(*args, **kwargs)
			except Exception:
				frappe.clear_messages()
				raise

		return async_wrapper

	@functools.wraps(func)
	def wrapper(*args, **kwargs):
		try:
			return _run_with_isolated_db(func, args, kwargs)
		except Exception:
			frappe.clear_messages()
			raise

	return wrapper


def _run_with_isolated_db(func, args, kwargs):
	with _TOOL_DB_LOCK:
		with _isolated_db():
			return func(*args, **kwargs)
