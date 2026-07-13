import threading
from collections.abc import Callable
from typing import Any

import frappe
from deepagents import (
	FilesystemPermission,
	HarnessProfile,
	SubAgent,
	create_deep_agent,
	register_harness_profile,
)
from frappe import _
from langchain.agents import create_agent
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_openai import ChatOpenAI

from ask_alyf.ask_alyf import tools
from ask_alyf.ask_alyf.deep_agent_backend import build_ask_alyf_backend
from ask_alyf.ask_alyf.history import history_item_to_native_message
from ask_alyf.ask_alyf.skill_utils import build_available_skills_instruction
from ask_alyf.ask_alyf.subagents import (
	DOCUMENT_PLANNER_INSTRUCTIONS,
	SOURCE_CODE_ANALYZER_INSTRUCTIONS,
	DocumentPlannerResult,
	SourceCodeAnalysisResult,
)
from ask_alyf.ask_alyf.toolset import (
	ask_alyfRuntime,
	ask_alyfToolset,
	clear_messages_on_tool_error,
)

# Deep Agents exposes built-in filesystem write tools (``write_file``,
# ``edit_file``) and a shell ``execute`` tool by default. Ask ALYF must never
# let the model mutate the host filesystem or run shell commands directly, so
# we register a provider-wide OpenAI harness profile that strips those tools
# from every Deep Agents graph built from a ``ChatOpenAI`` model. Read-only
# filesystem tools (``ls``, ``read_file``, ``glob``, ``grep``) remain available
# and operate on the restricted composite VFS (workspace, source, attachments).
ASK_ALYF_EXCLUDED_TOOLS = frozenset({"write_file", "edit_file", "execute"})
_ASK_ALYF_PROFILE_REGISTERED = False
_ASK_ALYF_PROFILE_LOCK = threading.Lock()


def _ensure_ask_alyf_harness_profile() -> None:
	"""Register the Ask ALYF harness profile once per process.

	Registration is additive and idempotent, but the check-then-set is guarded
	by a lock so concurrent ``ask_alyfAgentRunner`` constructions (e.g. two
	gunicorn threads or RQ jobs in one process) cannot both register.
	"""
	global _ASK_ALYF_PROFILE_REGISTERED
	with _ASK_ALYF_PROFILE_LOCK:
		if _ASK_ALYF_PROFILE_REGISTERED:
			return
		profile = HarnessProfile(excluded_tools=ASK_ALYF_EXCLUDED_TOOLS)
		register_harness_profile("openai", profile)
		_ASK_ALYF_PROFILE_REGISTERED = True


def _get_api_key_from_settings(settings) -> str:
	api_key = (settings.get_password("api_key", raise_exception=False) or "").strip()
	if not api_key:
		frappe.throw(_("Configure an API key in Ask ALYF Settings before sending messages."))
	return api_key


def _get_model_name_from_settings(settings) -> str:
	model_name = (settings.model or "").strip()
	if not model_name:
		frappe.throw(_("Configure a model in Ask ALYF Settings before sending messages."))
	return model_name


def build_chat_model(settings, *, temperature: float = 0.2) -> ChatOpenAI:
	"""Build a LangChain `ChatOpenAI` from Ask ALYF Settings values.

	Supports OpenAI and OpenAI-compatible `base_url` configurations by
	preserving the existing settings-driven model/api_key/base_url resolution.
	"""
	return ChatOpenAI(
		model=_get_model_name_from_settings(settings),
		api_key=_get_api_key_from_settings(settings),
		base_url=(settings.base_url or "").strip() or None,
		temperature=temperature,
	)


# --- Deep Agents coordinator --------------------------------------------------


class ask_alyfAgentRunner:
	def __init__(self, runtime: ask_alyfRuntime):
		self.runtime = runtime
		self.settings = tools.get_settings()
		self.toolset = ask_alyfToolset(runtime, settings=self.settings)
		_ensure_ask_alyf_harness_profile()
		self.model = build_chat_model(self.settings, temperature=0.2)
		app_roots = tools.get_installed_app_roots() if self.settings.is_code_search_enabled() else {}
		self.backend = build_ask_alyf_backend(app_roots)
		self.agent = create_deep_agent(
			model=self.model,
			tools=self._build_tools(),
			system_prompt=self._build_instructions(),
			backend=self.backend,
			subagents=self._build_subagents(),
			permissions=self._build_permissions(),
			name="ask_alyf",
		)

	def _can_write_skill(self) -> bool:
		return bool(frappe.has_permission("Ask ALYF Skill", ptype="create"))

	def _build_permissions(self) -> list[FilesystemPermission]:
		# Defense-in-depth on top of the harness profile tool exclusions: even
		# if a write tool somehow remained visible, the VFS denies every write.
		return [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]

	def _build_instructions(self) -> str:
		context = frappe.as_json(self.runtime.request_context, indent=2)
		excluded_doctypes = ", ".join(sorted(tools.get_excluded_doctypes())) or "None"
		available_skills_instruction = build_available_skills_instruction()
		system_prompt = (self.settings.system_prompt or "").strip()
		code_search_usage_instruction = ""
		if self.settings.is_code_search_enabled():
			code_search_usage_instruction = (
				"\n- When code search is enabled, delegate code questions to the "
				"`source-code-analyzer` subagent via the `task` tool instead of "
				"reasoning from memory."
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
- In `Agent` mode, prefer the `document-planner` subagent (via the `task` tool) before non-trivial `insert`, `save`, or `set_value` operations. If it returns `ready=false`, ask the user for the missing information instead of guessing. If it returns `ready=true`, use the matching write tool with the returned payload.
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

		if self.runtime.mode == "Agent":
			if self._can_write_skill():
				tool_defs.append(self.toolset.write_skill)
			tool_defs.extend(
				[
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

		return [clear_messages_on_tool_error(fn) for fn in tool_defs]

	def _build_subagents(self) -> list[SubAgent]:
		# ``general-purpose`` is auto-added by Deep Agents; we only declare the
		# two restricted specialists here. Each supplies an explicit minimal
		# tool list so the parent's proposal/mutation tools are never inherited.
		subagents: list[SubAgent] = []

		if self.settings.is_code_search_enabled():
			subagents.append(
				{
					"name": "source-code-analyzer",
					"description": (
						"Analyze installed app source code. Delegate code questions that require "
						"searching, reading, and interpreting multiple files to this specialist. "
						"It only has read-only filesystem tools scoped to the /source/ virtual mount."
					),
					"system_prompt": SOURCE_CODE_ANALYZER_INSTRUCTIONS,
					"tools": [],
					"response_format": SourceCodeAnalysisResult,
				}
			)

		if self.runtime.mode == "Agent":
			subagents.append(
				{
					"name": "document-planner",
					"description": (
						"Plan document create or update flows. Delegate non-trivial insert, save, "
						"or set_value operations to this read-only specialist so it can inspect "
						"metadata, resolve Link targets, and return a ready-to-execute payload."
					),
					"system_prompt": DOCUMENT_PLANNER_INSTRUCTIONS,
					"tools": [
						self.toolset.get_list,
						self.toolset.get,
						self.toolset.get_value,
						self.toolset.get_single_value,
						self.toolset.get_meta,
						self.toolset.has_permission,
						self.toolset.get_doc_permissions,
						self.toolset.list_accessible_doctypes,
					],
					"response_format": DocumentPlannerResult,
				}
			)

		return subagents

	def _build_input_messages(self, message: str) -> list[AnyMessage]:
		"""Build the native message list for the next invocation.

		Without a checkpointer, the full stored history is rebuilt as native
		messages each time and the new user message is appended.
		"""
		history = self.runtime.conversation_history or []
		messages: list[AnyMessage] = []
		for item in history:
			native = history_item_to_native_message(item)
			if native is not None:
				messages.append(native)

		messages.append(HumanMessage(content=message))
		return messages

	def run(self, message: str, conversation_history: list[dict[str, Any]]) -> dict[str, Any]:
		self.runtime.conversation_history = conversation_history or []
		input_messages = self._build_input_messages(message)
		result = self.agent.invoke({"messages": input_messages})
		response_text = ""
		result_messages = result.get("messages") if isinstance(result, dict) else None
		if result_messages:
			last = result_messages[-1]
			content = getattr(last, "content", None)
			if isinstance(content, str):
				response_text = content.strip()
			elif content is None:
				response_text = ""
			else:
				# Some providers return content as a list of blocks; coerce to text.
				response_text = str(content).strip()

		return {
			"response": response_text,
			"pending_operations": self.runtime.pending_operations,
			"document_extractions": self.runtime.document_extractions,
			"attached_files": self.runtime.attached_files,
		}


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


# ``create_agent`` is re-exported so the field agent (and any future stateless
# caller) can build a plain LangChain agent without the Deep Agents middleware
# stack.
def build_stateless_agent(model, tools, *, system_prompt: str):
	"""Build a plain LangChain agent with no checkpointer, no subagents, no VFS.

	Used by the field agent which must remain stateless and tool-restricted.
	"""
	return create_agent(
		model=model,
		tools=tools,
		system_prompt=system_prompt,
	)
