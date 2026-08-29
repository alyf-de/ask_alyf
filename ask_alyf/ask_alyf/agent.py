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
from langchain.agents.middleware import AgentMiddleware, ToolErrorMiddleware, hook_config
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command

from ask_alyf.ask_alyf import tools
from ask_alyf.ask_alyf.checkpointer import FrappeCheckpointSaver
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
	OPERATION_INTERRUPT_KEY,
	ask_alyfRuntime,
	ask_alyfToolset,
	clear_messages_on_tool_error,
	is_stop_requested,
)

OPERATION_RESUME_SAVEPOINT = "ask_alyf_operation_resume"


def _on_tool_error(exc: Exception, request: ToolCallRequest) -> str:
	"""Convert a tool exception into content for an error ToolMessage.

	Frappe validation/permission messages are intentional and help the model
	correct course. Unexpected exceptions stay type-only to avoid leaking
	internal detail (LangChain ToolErrorMiddleware guidance).
	"""
	tool_name = request.tool_call.get("name") or "tool"
	exc_name = type(exc).__name__
	if isinstance(exc, frappe.ValidationError | frappe.PermissionError):
		detail = str(exc).strip()
		if detail:
			return f"`{tool_name}` failed ({exc_name}): {detail}. Fix the inputs or approach and retry."
	return f"`{tool_name}` failed with {exc_name}. Fix the inputs or approach and retry."


def build_tool_error_middleware() -> ToolErrorMiddleware:
	"""Build middleware that returns tool failures to the model for retry."""
	return ToolErrorMiddleware(on_error=_on_tool_error)


# Tool arguments are shown to the user, so a large payload (a full document,
# an extracted PDF) is trimmed to keep the conversation record readable.
TOOL_CALL_ARGS_LIMIT = 500


def _summarize_tool_args(args: Any) -> dict[str, Any]:
	"""Trim tool arguments down to something worth showing in the chat."""
	if not isinstance(args, dict):
		return {}

	summary = {}
	for key, value in args.items():
		text = value if isinstance(value, str) else frappe.as_json(value, indent=None)
		if len(text) > TOOL_CALL_ARGS_LIMIT:
			text = text[:TOOL_CALL_ARGS_LIMIT] + "…"
		summary[key] = text
	return summary


def _tool_call_label(name: str, args: Any) -> str:
	"""Describe a tool call in the user's words, for the step list in the chat.

	Names the record being worked on wherever the arguments carry one, because
	"Reading Sales Invoice list" tells the user something that `get_list` does
	not. Anything unmapped falls back to a readable form of the tool name, so a
	new tool shows up in the list instead of disappearing from it.
	"""
	args = args if isinstance(args, dict) else {}
	doctype = _(args["doctype"]) if isinstance(args.get("doctype"), str) else ""
	docname = args["name"] if isinstance(args.get("name"), str) else ""
	target = " ".join(part for part in (doctype, docname) if part)

	if name == "get_list":
		return _("Reading {0} list").format(doctype)
	if name == "get_count":
		return _("Counting {0}").format(doctype)
	if name in ("get", "get_value", "get_single_value", "get_print"):
		return _("Reading {0}").format(target or doctype)
	if name == "get_meta":
		return _("Checking the {0} form").format(doctype)
	if name in ("has_permission", "get_doc_permissions"):
		return _("Checking your permissions on {0}").format(doctype)
	if name == "list_accessible_doctypes":
		return _("Looking up what you can access")
	if name == "list_accessible_reports":
		return _("Looking up available reports")
	if name == "translate_ui_labels":
		return _("Translating labels")
	if name == "run_read_only_sql":
		return _("Running a database query")
	if name in ("read_skill", "write_skill"):
		return _("Reading instructions") if name == "read_skill" else _("Saving instructions")
	if name in ("get_file_id", "read_file_record", "extract_document_data"):
		return _("Reading the attached document")
	if name in ("get_app_version", "read_github_releases", "read_documentation_page"):
		return _("Reading documentation")
	if name in ("ls", "read_file", "glob", "grep"):
		return _("Searching the source code")
	if name == "task":
		return _("Asking the {0} specialist").format(args.get("subagent_type") or _("assistant"))
	if name == "write_todos":
		return _("Planning the next steps")

	if name == "insert":
		return _("Creating {0}").format(doctype)
	if name == "batch_insert":
		return _("Creating several {0} records").format(doctype)
	if name in ("save", "set_value"):
		return _("Updating {0}").format(target or doctype)
	if name == "submit":
		return _("Submitting {0}").format(target or doctype)
	if name == "cancel":
		return _("Cancelling {0}").format(target or doctype)
	if name == "amend":
		return _("Amending {0}").format(target or doctype)
	if name == "delete":
		return _("Deleting {0}").format(target or doctype)
	if name == "rename_doc":
		return _("Renaming {0}").format(target or doctype)
	if name == "attach_file":
		return _("Attaching a file to {0}").format(target or doctype)
	if name == "run_whitelisted_method":
		return _("Running {0}").format(args.get("method") or _("an action"))

	if name == "set_route":
		return _("Opening a page")
	if name == "new_doc":
		return _("Opening a new {0}").format(doctype)
	if name == "scroll_to_field":
		return _("Highlighting a field")
	if name == "show_chart":
		return _("Preparing a chart")
	if name in ("frm_set_value", "frm_add_child"):
		return _("Filling in the open form")

	return name.replace("_", " ").capitalize()


class ToolCallLogMiddleware(AgentMiddleware):
	"""Show the user each tool call as it happens, and keep the list afterwards.

	Every call is announced when it starts and updated when it ends, so the
	chat builds up a live list of steps instead of a single status line that
	overwrites itself. The same list is persisted with the assistant message,
	so reopening the conversation still shows what the agent did.
	"""

	def __init__(self, runtime: ask_alyfRuntime):
		super().__init__()
		self.runtime = runtime

	@hook_config(can_jump_to=["end"])
	def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
		"""End the run here if the user pressed stop.

		Between two model calls every tool call already has its result, so the
		thread stays valid for the next turn and the checkpoint keeps the work
		done so far. A stop pressed during a long tool call — a subagent
		delegation, say — therefore lands once that call returns.
		"""
		if not is_stop_requested(self.runtime.run_id):
			return None

		self.runtime.stop_requested = True
		return {"jump_to": "end"}

	def wrap_tool_call(self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Any]) -> Any:
		call = request.tool_call
		call_id = call.get("id") or ""
		# The model plans a whole batch of calls at once and the graph runs the
		# batch in one step, so ending the run at the next model call would
		# still pay for every call in it. Skipping them here keeps the answer
		# each one owes, so the thread stays valid for the next turn, and costs
		# nothing but the cache read.
		if is_stop_requested(self.runtime.run_id):
			self.runtime.stop_requested = True
			return ToolMessage(
				content="Stopped by the user before this ran.",
				name=call.get("name") or "",
				tool_call_id=call_id,
			)

		self.runtime.begin_tool_call(
			call_id,
			call.get("name") or "",
			_summarize_tool_args(call.get("args")),
			_tool_call_label(call.get("name") or "", call.get("args")),
		)
		try:
			result = handler(request)
		except GraphBubbleUp:
			# A paused proposal has not happened yet: drop it from the list
			# until the user decides and the tool node runs again.
			self.runtime.drop_tool_call(call_id)
			raise
		except Exception:
			self.runtime.finish_tool_call(call_id, "failed")
			raise

		self.runtime.finish_tool_call(
			call_id, "failed" if getattr(result, "status", None) == "error" else "success"
		)
		return result


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
		use_responses_api=True,
		reasoning_effort=(settings.reasoning_effort or "").strip() or None,
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
		self.checkpointer = FrappeCheckpointSaver()
		self.agent = create_deep_agent(
			model=self.model,
			tools=self._build_tools(),
			system_prompt=self._build_instructions(),
			backend=self.backend,
			subagents=self._build_subagents(),
			permissions=self._build_permissions(),
			middleware=[build_tool_error_middleware(), ToolCallLogMiddleware(runtime)],
			checkpointer=self.checkpointer,
			name="ask_alyf",
		)

	@property
	def thread_config(self) -> dict[str, Any]:
		"""Graph config that ties this run to the conversation's stored state."""
		return {"configurable": {"thread_id": self.runtime.conversation_name}}

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
				"\n- Never call `read_file`, `glob`, or `grep` on `/source/` yourself. Anything "
				"that needs app source — field semantics, defaults, validation logic — goes to "
				"`source-code-analyzer` in a single delegation carrying the full question. For "
				"document fields, use `get_meta` and `document-planner` first and reach for "
				"source only when those cannot answer it."
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
- `Agent` mode supports mutation workflows with write tools while still handling read-only questions with read tools. A write tool call pauses and waits for the user to confirm; the tool then returns the real outcome to you, so you learn whether it succeeded before deciding what to do next.
- Call one write tool at a time and wait for its result. If a request needs several writes, do the first, read its result, then do the next. Never propose two write tools in the same step.
- Frontend action tools can navigate or adjust the current form in the browser, or display Frappe Charts under the assistant message via `show_chart` (pass `frappe_charts` as a list of chart option objects; validated server-side). See the `show_chart` tool docstring for the options shape.
- Frontend actions with `requires_confirmation` must be confirmed before the browser executes them.
- In `Agent` mode, prefer the `document-planner` subagent (via the `task` tool) before non-trivial `insert`, `save`, or `set_value` operations. If it returns `ready=false`, ask the user for the missing information instead of guessing. If it returns `ready=true`, use the matching write tool with the returned payload.
- When the user wants to create multiple documents of the same DocType, prefer `batch_insert` instead of preparing many separate `insert` proposals.
- Before insert or save, call get_meta for the target DocType and follow field types exactly.
- Child table fields (fieldtype Table) must be arrays of row objects, never plain strings.
- Act on clear intent immediately with sensible defaults. Only ask when required information is truly missing and cannot be inferred.
- Never repeat the user's data in your response. The UI shows a detailed preview of every pending write. After calling a write tool, confirm readiness in one sentence.
- A write tool that returns `rejected` means the user declined it. Acknowledge briefly, suggest an alternative if there is one, and never retry the same operation.
- When a write tool returns a result, confirm the outcome briefly. If a natural follow-up action exists (e.g. submitting a newly created document), proceed with it. Do not ask "would you like me to..." — just do it.
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
		#
		# Note on ``tools: []`` for source-code-analyzer: Deep Agents always
		# injects ``FilesystemMiddleware`` (and thus the read-only VFS tools
		# ``ls``, ``read_file``, ``glob``, ``grep``) into every subagent stack
		# regardless of the ``tools`` field — the ``tools`` list only controls
		# *extra* tools inherited from the parent. An empty list therefore means
		# "no parent tools", not "no tools at all"; the analyzer still gets the
		# ``/source/``-scoped read tools it needs while staying free of mutation
		# tools. Write tools are stripped by the harness profile + deny
		# permission, so the VFS remains strictly read-only here.
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
					# Reading source is where a run spends the most, so this
					# specialist carries the log middleware too: its reads show
					# up as steps, and a stop reaches it instead of waiting for
					# the whole delegation to finish.
					"middleware": [build_tool_error_middleware(), ToolCallLogMiddleware(self.runtime)],
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
						clear_messages_on_tool_error(fn)
						for fn in (
							self.toolset.get_list,
							self.toolset.get,
							self.toolset.get_value,
							self.toolset.get_single_value,
							self.toolset.get_meta,
							self.toolset.has_permission,
							self.toolset.get_doc_permissions,
							self.toolset.list_accessible_doctypes,
						)
					],
					"middleware": [build_tool_error_middleware(), ToolCallLogMiddleware(self.runtime)],
					"response_format": DocumentPlannerResult,
				}
			)

		return subagents

	def _build_input_messages(self, message: str) -> list[AnyMessage]:
		"""Build the native message list for the next invocation.

		The checkpointer restores what the agent saw and did in earlier turns,
		so a conversation with stored state only receives what is new to it:
		the stored items that follow the last assistant message (an action
		result, for example) plus the user message of this turn. A conversation
		without stored state — one from before the checkpointer, or one whose
		checkpoints were cleared — is seeded once with its full stored history.
		"""
		history = self.runtime.conversation_history or []
		if self.checkpointer.get_tuple(self.thread_config) is not None:
			history = _history_after_last_assistant_message(history)

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

		# A proposal the user never answered still holds the graph. Sending a
		# new message means they moved on, so the proposal counts as rejected
		# and the new message rides along with the same resume.
		abandoned = self._pending_operation_call_id()
		if abandoned:
			command = Command(
				resume={"call_id": abandoned, "status": "rejected"},
				update={"messages": input_messages},
			)
			return self._run_graph(command)

		return self._run_graph({"messages": input_messages})

	def _run_graph(self, payload: Any) -> dict[str, Any]:
		"""Invoke the graph, keeping the run's checkpoints even if it dies.

		A run killed mid-flight — RQ's death penalty, a provider error — has
		already completed super-steps whose checkpoints exist only in this
		process's memory. Storing them lets the next turn continue from the
		work the user already paid for instead of repeating it. The rows ride
		the job's transaction, which the caller commits along with the error it
		reports to the user.

		``resume`` deliberately does not go through here: a failed resume needs
		the stored pause left exactly as it was, so it owns its own handling.
		"""
		try:
			return self._finish(self.agent.invoke(payload, config=self.thread_config))
		except Exception:
			self.checkpointer.flush()
			raise

	def resume(self, call_id: str, status: str, **decision: Any) -> dict[str, Any]:
		"""Continue a run that paused on a proposal, with the user's decision."""
		command = Command(resume={"call_id": call_id, "status": status, **decision})
		frappe.db.savepoint(OPERATION_RESUME_SAVEPOINT)
		try:
			return self._finish(self.agent.invoke(command, config=self.thread_config))
		except Exception:
			frappe.db.rollback(save_point=OPERATION_RESUME_SAVEPOINT)
			if not self.runtime.backend_operation_committed:
				# Nothing was written and nothing was flushed, so the thread is
				# still paused on this proposal: keep it, so a confirmation that
				# failed on the way to the backend can be tried again.
				raise

			# The write landed on the tool's own connection while the thread
			# never recorded its outcome. Resuming that stale pause would run
			# the write a second time, so the thread goes and the next turn is
			# seeded from the stored conversation history instead.
			self.checkpointer.delete_thread(self.runtime.conversation_name)
			frappe.log_error("Ask ALYF Action Follow-Up Error")
			frappe.clear_messages()
			return {
				"response": _(
					"The operation was completed, but the follow-up response could not be generated."
				),
				"pending_operations": [],
				"document_extractions": self.runtime.document_extractions,
				"attached_files": self.runtime.attached_files,
				"tool_calls": self.runtime.tool_calls,
			}

	def _finish(self, result: Any) -> dict[str, Any]:
		# LangGraph checkpoints from its background threads, which must not use
		# this request's database connection. Nothing is stored until we flush
		# here, or — for a run that died mid-flight — in `_run_graph`.
		self.checkpointer.flush()
		result_messages = result.get("messages") if isinstance(result, dict) else None
		# A stopped run ends wherever it stood, so its last message is whatever
		# happened to be there — a tool result, or the user's own text. Nothing
		# in it is an answer, so the caller words the outcome instead.
		stopped = self.runtime.stop_requested
		response_text = "" if stopped else (result_messages[-1].text.strip() if result_messages else "")

		return {
			"response": response_text,
			"stopped": stopped,
			"pending_operations": [*self.runtime.pending_operations, *_interrupted_operations(result)],
			"document_extractions": self.runtime.document_extractions,
			"attached_files": self.runtime.attached_files,
			"tool_calls": self.runtime.tool_calls,
		}

	def _pending_operation_call_id(self) -> str:
		"""Return the call_id of the proposal this thread is paused on, if any."""
		state = self.agent.get_state(self.thread_config)
		for operation in _interrupted_operations({"__interrupt__": state.interrupts}):
			return operation.get("call_id") or ""
		return ""


def _interrupted_operations(result: Any) -> list[dict[str, Any]]:
	"""Read the operations a paused graph is waiting on out of its interrupts."""
	operations = []
	for item in (result.get("__interrupt__") if isinstance(result, dict) else None) or ():
		value = getattr(item, "value", item)
		operation = value.get(OPERATION_INTERRUPT_KEY) if isinstance(value, dict) else None
		if isinstance(operation, dict):
			operations.append(operation)

	return operations


def _history_after_last_assistant_message(
	history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	"""Return the stored items the agent cannot have seen yet.

	Everything up to and including the last assistant message is already in the
	checkpointed thread. What follows it was appended by the app afterwards.
	"""
	for index in range(len(history) - 1, -1, -1):
		if (history[index].get("role") or "").lower() == "assistant":
			return history[index + 1 :]

	return history


def run_message(
	conversation_name: str,
	message: str,
	mode: str,
	request_context: dict[str, Any],
	conversation_history: list[dict[str, Any]],
	run_id: str = "",
) -> dict[str, Any]:
	runtime = ask_alyfRuntime(
		conversation_name=conversation_name,
		mode=mode,
		request_context=request_context,
		conversation_history=conversation_history,
		run_id=run_id,
	)
	runner = ask_alyfAgentRunner(runtime)
	return runner.run(message, conversation_history)


def resume_operation(
	conversation_name: str,
	mode: str,
	request_context: dict[str, Any],
	*,
	call_id: str,
	status: str,
	**decision: Any,
) -> dict[str, Any]:
	"""Continue the paused run of a conversation with the user's decision.

	The agent is holding inside the tool that proposed the operation, so the
	decision reaches it as that tool's own result — there is no need to
	replay the conversation or describe the outcome back to the model.
	"""
	runtime = ask_alyfRuntime(
		conversation_name=conversation_name,
		mode=mode,
		request_context=request_context,
	)
	runner = ask_alyfAgentRunner(runtime)
	return runner.resume(call_id, status, **decision)


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
		middleware=[build_tool_error_middleware()],
	)
