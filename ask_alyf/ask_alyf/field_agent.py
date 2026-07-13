from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import frappe

from ask_alyf.ask_alyf import field_contexts, tools
from ask_alyf.ask_alyf.agent import (
	_clear_messages_on_tool_error,
	ask_alyfToolset,
	build_chat_model,
	build_stateless_agent,
)

# Read-only tool method names exposed to the field agent.
# Excludes: source_code_analyzer (unbounded latency in request thread),
# get_print / extract_document_data (require conversation_name),
# write_skill / document_planner (write or sub-agent tools),
# all write tools, all frontend tools.
FIELD_AGENT_TOOLS: list[str] = [
	"get_list",
	"get_count",
	"get",
	"get_value",
	"get_single_value",
	"get_meta",
	"has_permission",
	"get_doc_permissions",
	"list_accessible_doctypes",
	"list_accessible_reports",
	"translate_ui_labels",
	"get_file_id",
	"read_file_record",
	"run_read_only_sql",
	"get_app_version",
	"read_github_releases",
	"read_documentation_page",
]


@dataclass
class FieldAgentRuntime:
	"""Minimal runtime for the field agent — no conversation, no realtime events.

	The request_context and conversation_history attributes are required because
	ask_alyfToolset.translate_ui_labels accesses self.runtime.request_context and
	other tool methods may reference self.runtime.conversation_history.
	"""

	request_context: dict[str, Any] = field(default_factory=dict)
	conversation_history: list[dict[str, Any]] = field(default_factory=list)

	def emit_status(self, text: str) -> None:
		"""No-op status emitter — field agent runs synchronously without realtime."""
		pass


def build_field_agent_instructions(
	context: dict,
	doctype: str,
	fieldname: str,
	fieldtype: str,
	current_value: str,
	doc: dict,
) -> str:
	"""Build the system instructions for the field agent run.

	Args:
		context: Field context dict from field_contexts.get_field_context.
		doctype: The DocType name.
		fieldname: The field name.
		fieldtype: The Frappe fieldtype string.
		current_value: The current field value (may be empty).
		doc: The sanitized document dict from the frontend.

	Returns:
		The full system instructions string for the agent.
	"""
	system_prompt = context.get("system_prompt") or ""

	doc_json = json.dumps(doc, ensure_ascii=False, indent=2, default=str)

	instructions_parts = [system_prompt]

	instructions_parts.append(
		f"\n\n---\nDocType: {doctype}\nField name: {fieldname}\nField type: {fieldtype}\n"
	)

	if current_value:
		instructions_parts.append(f"\nCurrent field value:\n{current_value}\n")
	else:
		instructions_parts.append("\nCurrent field value: (empty)\n")

	instructions_parts.append(
		f"\nDocument context (JSON — child tables may be truncated to first 20 rows):\n{doc_json}\n"
	)

	instructions_parts.append("\nReturn only the new field value with no surrounding explanation.")

	return "".join(instructions_parts)


def run_field_agent(
	doctype: str,
	fieldname: str,
	fieldtype: str,
	current_value: str,
	doc: dict,
	prompt: str,
) -> str:
	"""Run the field agent synchronously and return the generated field value.

	Args:
		doctype: The DocType name.
		fieldname: The field name.
		fieldtype: The Frappe fieldtype string.
		current_value: The current field value.
		doc: The sanitized document dict (Password fields stripped, child tables truncated).
		prompt: The user's natural-language instruction.

	Returns:
		The generated field value as a string.

	Raises:
		frappe.ValidationError: If the API key or model is not configured in settings.
	"""
	settings = tools.get_settings()
	context = field_contexts.get_field_context(doctype, fieldname, fieldtype, doc=doc)
	instructions = build_field_agent_instructions(
		context=context,
		doctype=doctype,
		fieldname=fieldname,
		fieldtype=fieldtype,
		current_value=current_value,
		doc=doc,
	)

	runtime = FieldAgentRuntime()
	toolset = ask_alyfToolset(runtime=runtime, settings=settings)

	tool_defs = [
		_clear_messages_on_tool_error(getattr(toolset, method_name)) for method_name in FIELD_AGENT_TOOLS
	]

	model = build_chat_model(settings, temperature=0.1)
	agent = build_stateless_agent(model, tool_defs, system_prompt=instructions)

	try:
		result = agent.invoke({"messages": [{"role": "user", "content": prompt}]})
	except Exception:
		frappe.log_error("Ask ALYF Field Agent Error")
		raise

	result_messages = result.get("messages") if isinstance(result, dict) else None
	if result_messages:
		last = result_messages[-1]
		content = getattr(last, "content", None)
		if isinstance(content, str):
			return content.strip()

	return ""
