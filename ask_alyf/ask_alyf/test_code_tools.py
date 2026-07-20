import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ask_alyf.ask_alyf import tools
from ask_alyf.ask_alyf.agent import ASK_ALYF_EXCLUDED_TOOLS, ask_alyfAgentRunner
from ask_alyf.ask_alyf.history import history_item_to_native_message
from ask_alyf.ask_alyf.toolset import ask_alyfToolset, clear_messages_on_tool_error


class FakeSettings(SimpleNamespace):
	def __init__(self, *, allow_code_search: bool):
		super().__init__(
			allow_code_search=allow_code_search,
			system_prompt="",
			model="gpt-test",
			llm_provider="OpenAI",
			base_url="",
		)

	def is_code_search_enabled(self) -> bool:
		return bool(self.allow_code_search)

	def get_password(self, _fieldname, raise_exception=False):
		return "test-key"


class UnitTestCodeTools(FrappeTestCase):
	def make_runtime(self, *, mode: str = "Ask"):
		return SimpleNamespace(
			conversation_name="TEST-CONVERSATION",
			mode=mode,
			request_context={},
			conversation_history=[],
			pending_operations=[],
			document_extractions=[],
			attached_files=[],
			emit_status=lambda _text: None,
		)

	def make_runner(self, *, allow_code_search: bool, mode: str = "Ask"):
		runtime = self.make_runtime(mode=mode)
		runner = object.__new__(ask_alyfAgentRunner)
		runner.runtime = runtime
		runner.settings = FakeSettings(allow_code_search=allow_code_search)
		runner.toolset = ask_alyfToolset(runtime, settings=runner.settings)
		return runner

	def test_subagents_register_source_code_analyzer_only_when_code_search_enabled(self):
		raw_code_tool_names = {"search_code", "read_code_file", "ls", "find", "grep"}

		disabled_runner = self.make_runner(allow_code_search=False)
		disabled_subagents = disabled_runner._build_subagents()
		disabled_names = {sub["name"] for sub in disabled_subagents}
		self.assertNotIn("source-code-analyzer", disabled_names)
		# Raw code tools are never exposed as parent tools regardless of setting.
		disabled_tool_names = {tool.__name__ for tool in disabled_runner._build_tools()}
		self.assertFalse(raw_code_tool_names.intersection(disabled_tool_names))

		enabled_runner = self.make_runner(allow_code_search=True)
		enabled_subagents = enabled_runner._build_subagents()
		enabled_names = {sub["name"] for sub in enabled_subagents}
		self.assertIn("source-code-analyzer", enabled_names)
		# Source code tools are not parent-visible; they live inside the subagent.
		enabled_tool_names = {tool.__name__ for tool in enabled_runner._build_tools()}
		self.assertFalse(raw_code_tool_names.intersection(enabled_tool_names))

	def test_subagents_register_document_planner_only_in_agent_mode(self):
		ask_runner = self.make_runner(allow_code_search=False, mode="Ask")
		ask_subagents = ask_runner._build_subagents()
		ask_names = {sub["name"] for sub in ask_subagents}
		self.assertNotIn("document-planner", ask_names)
		ask_tool_names = {tool.__name__ for tool in ask_runner._build_tools()}
		self.assertNotIn("batch_insert", ask_tool_names)

		agent_runner = self.make_runner(allow_code_search=False, mode="Agent")
		agent_subagents = agent_runner._build_subagents()
		agent_names = {sub["name"] for sub in agent_subagents}
		self.assertIn("document-planner", agent_names)
		agent_tool_names = {tool.__name__ for tool in agent_runner._build_tools()}
		self.assertIn("batch_insert", agent_tool_names)

	def test_build_tools_only_adds_write_skill_when_user_can_create_skills(self):
		ask_runner = self.make_runner(allow_code_search=False, mode="Ask")
		with patch.object(ask_runner, "_can_write_skill", return_value=True):
			ask_tool_names = {tool.__name__ for tool in ask_runner._build_tools()}
		self.assertNotIn("write_skill", ask_tool_names)

		agent_runner = self.make_runner(allow_code_search=False, mode="Agent")
		with patch.object(agent_runner, "_can_write_skill", return_value=False):
			agent_tool_names = {tool.__name__ for tool in agent_runner._build_tools()}
		self.assertNotIn("write_skill", agent_tool_names)

		with patch.object(agent_runner, "_can_write_skill", return_value=True):
			agent_tool_names = {tool.__name__ for tool in agent_runner._build_tools()}
		self.assertIn("write_skill", agent_tool_names)

	def test_build_instructions_lists_available_skills(self):
		runner = self.make_runner(allow_code_search=False)

		with (
			patch("ask_alyf.ask_alyf.skill_utils.get_available_skill_summaries") as get_skills,
			patch("ask_alyf.ask_alyf.tools.get_excluded_doctypes", return_value=set()),
		):
			get_skills.return_value = [{"name": "expense-guide", "title": "Expense Guide"}]
			instructions = runner._build_instructions()

		self.assertIn("Use `read_skill`", instructions)
		self.assertIn("name: expense-guide | title: Expense Guide", instructions)

	def test_read_skill_returns_accessible_skill_content(self):
		runtime = self.make_runtime(mode="Ask")
		toolset = ask_alyfToolset(runtime)
		skill_doc = SimpleNamespace(
			name="expense-guide",
			title="Expense Guide",
			description="Use this skill for expense questions.",
			roles=[SimpleNamespace(role="Accounts User")],
		)

		with (
			patch("ask_alyf.ask_alyf.skill_utils.frappe.get_doc", return_value=skill_doc),
			patch("ask_alyf.ask_alyf.skill_utils.frappe.get_roles", return_value=["Accounts User"]),
		):
			result = toolset.read_skill("expense-guide")

		self.assertEqual(
			result,
			{
				"name": "expense-guide",
				"title": "Expense Guide",
				"description": "Use this skill for expense questions.",
			},
		)

	def test_read_skill_rejects_skill_without_matching_role(self):
		runtime = self.make_runtime(mode="Ask")
		toolset = ask_alyfToolset(runtime)
		skill_doc = SimpleNamespace(
			name="expense-guide",
			title="Expense Guide",
			description="Use this skill for expense questions.",
			roles=[SimpleNamespace(role="Accounts User")],
		)

		with (
			patch("ask_alyf.ask_alyf.skill_utils.frappe.get_doc", return_value=skill_doc),
			patch("ask_alyf.ask_alyf.skill_utils.frappe.get_roles", return_value=["Employee"]),
		):
			with self.assertRaises(frappe.ValidationError):
				toolset.read_skill("expense-guide")

	def test_write_skill_proposes_ask_alyf_skill_insert(self):
		runtime = self.make_runtime(mode="Agent")
		toolset = ask_alyfToolset(runtime)

		result = toolset.write_skill(
			title="Expense Guide",
			description="Use this skill for expense questions.",
			roles=["Accounts User", "Employee"],
			reason="Create reusable expense guidance.",
		)

		self.assertTrue(result["success"])
		self.assertEqual(result["proposal"]["tool"], "insert")
		self.assertEqual(result["proposal"]["payload"]["doctype"], "Ask ALYF Skill")
		self.assertEqual(
			result["proposal"]["payload"]["values"],
			{
				"title": "Expense Guide",
				"description": "Use this skill for expense questions.",
				"roles": [{"role": "Accounts User"}, {"role": "Employee"}],
			},
		)

	def test_write_skill_accepts_comma_separated_roles_string(self):
		runtime = self.make_runtime(mode="Agent")
		toolset = ask_alyfToolset(runtime)

		result = toolset.write_skill(
			title="Expense Guide",
			description="Use this skill for expense questions.",
			roles="Accounts User, Employee",
		)

		self.assertEqual(
			result["proposal"]["payload"]["values"]["roles"],
			[{"role": "Accounts User"}, {"role": "Employee"}],
		)

	def test_code_tools_require_setting_to_be_enabled(self):
		with patch(
			"ask_alyf.ask_alyf.tools.get_settings", return_value=FakeSettings(allow_code_search=False)
		):
			with self.assertRaises(frappe.ValidationError):
				tools.ls("ask_alyf")

	def test_read_code_file_rejects_non_app_paths(self):
		with patch("ask_alyf.ask_alyf.tools.get_settings", return_value=FakeSettings(allow_code_search=True)):
			with self.assertRaises(frappe.ValidationError):
				tools.read_code_file("sites/common_site_config.json")

	def test_ls_find_and_grep_are_scoped_to_installed_app_paths(self):
		with patch("ask_alyf.ask_alyf.tools.get_settings", return_value=FakeSettings(allow_code_search=True)):
			listing = tools.ls("ask_alyf", "ask_alyf", limit=10)
			self.assertEqual(listing["app_name"], "ask_alyf")
			self.assertTrue(listing["entries"])
			self.assertTrue(all(entry["path"].startswith("apps/ask_alyf/") for entry in listing["entries"]))

			find_result = tools.find("ask_alyf", name_pattern="agent.py", limit=10)
			find_paths = {match["path"] for match in find_result["matches"]}
			self.assertIn("apps/ask_alyf/ask_alyf/ask_alyf/agent.py", find_paths)

			grep_result = tools.grep(
				"ask_alyf",
				"class ask_alyfAgentRunner",
				file_pattern="*.py",
				limit=10,
			)
			grep_paths = {match["path"] for match in grep_result["matches"]}
			self.assertIn("apps/ask_alyf/ask_alyf/ask_alyf/agent.py", grep_paths)

	def test_get_list_coerces_single_field_string_to_list(self):
		with patch("ask_alyf.ask_alyf.tools.client.get_list", return_value=[]) as get_list:
			result = tools.get_list("Item Group", fields="name", filters={})

		get_list.assert_called_once_with(
			doctype="Item Group",
			fields=["name"],
			filters={},
			order_by=None,
			limit_page_length=20,
			group_by=None,
		)
		self.assertEqual(result, [])

	def test_get_list_coerces_comma_separated_fields_to_list(self):
		with patch("ask_alyf.ask_alyf.tools.client.get_list", return_value=[]) as get_list:
			result = tools.get_list(
				"Purchase Taxes and Charges Template",
				fields="name,company",
				filters={"company": "ALYF GmbH"},
			)

		get_list.assert_called_once_with(
			doctype="Purchase Taxes and Charges Template",
			fields=["name", "company"],
			filters={"company": "ALYF GmbH"},
			order_by=None,
			limit_page_length=20,
			group_by=None,
		)
		self.assertEqual(result, [])

	def test_get_file_id_uses_reference_filters(self):
		expected_filters = {
			"attached_to_doctype": "Sales Invoice",
			"attached_to_name": "SINV-0001",
			"attached_to_field": "custom_attachment",
			"file_name": "invoice.pdf",
		}
		with patch("ask_alyf.ask_alyf.tools.get_list", return_value=[{"name": "FILE-0001"}]) as get_list:
			file_id = tools.get_file_id(
				reference_doctype="Sales Invoice",
				reference_name="SINV-0001",
				reference_field="custom_attachment",
				file_name="invoice.pdf",
			)

		get_list.assert_called_once_with(
			"File",
			fields=["name"],
			filters=expected_filters,
			order_by="modified desc",
			limit=2,
		)
		self.assertEqual(file_id, "FILE-0001")

	def test_get_file_id_rejects_ambiguous_matches(self):
		with patch(
			"ask_alyf.ask_alyf.tools.get_list",
			return_value=[{"name": "FILE-0001"}, {"name": "FILE-0002"}],
		):
			with self.assertRaises(frappe.ValidationError):
				tools.get_file_id(reference_doctype="Sales Invoice", reference_name="SINV-0001")

	def test_attach_file_proposal_uses_linked_file_name_summary(self):
		runtime = self.make_runtime(mode="Agent")
		toolset = ask_alyfToolset(runtime)
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "invoice.txt",
				"content": "Test Content",
				"is_private": 1,
			}
		).save()

		result = toolset.attach_file("ToDo", "TODO-0001", file_doc.name)

		self.assertTrue(result["success"])
		self.assertEqual(result["proposal"]["payload"]["file_id"], file_doc.name)
		self.assertIn(f"[{file_doc.file_name}](", result["proposal"]["summary"])
		self.assertIn(file_doc.file_url, result["proposal"]["summary"])

	def test_batch_insert_proposal_uses_record_count_summary(self):
		runtime = self.make_runtime(mode="Agent")
		toolset = ask_alyfToolset(runtime)
		records = [{"description": "Call customer"}, {"description": "Send quotation"}]

		result = toolset.batch_insert("ToDo", records, reason="Imported open tasks.")

		self.assertTrue(result["success"])
		self.assertEqual(result["proposal"]["tool"], "batch_insert")
		self.assertEqual(result["proposal"]["payload"]["doctype"], "ToDo")
		self.assertEqual(result["proposal"]["payload"]["records"], records)
		self.assertEqual(result["proposal"]["summary"], "Create 2 ToDo records")

	def test_validate_pending_action_payload_rejects_invalid_batch_insert_rows(self):
		self.assertEqual(
			tools.validate_pending_action_payload("batch_insert", {"doctype": "ToDo", "records": []}),
			"Action field 'records' must be a non-empty list.",
		)
		self.assertEqual(
			tools.validate_pending_action_payload(
				"batch_insert",
				{"doctype": "ToDo", "records": [{"description": "Call customer"}, "bad row"]},
			),
			"Action field 'records' row #2 must be an object.",
		)

	def test_execute_action_batch_insert_collects_successes_and_failures(self):
		records = [
			{"description": "Call customer"},
			{"description": "Send quotation"},
			{"description": "Review delivery"},
		]
		insert_side_effect = [
			{"name": "TODO-0001"},
			Exception("Missing description"),
			SimpleNamespace(name="TODO-0003"),
		]

		with (
			patch("ask_alyf.ask_alyf.tools.ensure_editable_doctype"),
			patch("ask_alyf.ask_alyf.tools.client.insert", side_effect=insert_side_effect) as insert,
		):
			result = tools.execute_action({"action": "batch_insert", "doctype": "ToDo", "records": records})

		self.assertEqual(insert.call_count, 3)
		insert.assert_any_call(doc={"doctype": "ToDo", "description": "Call customer"})
		insert.assert_any_call(doc={"doctype": "ToDo", "description": "Send quotation"})
		insert.assert_any_call(doc={"doctype": "ToDo", "description": "Review delivery"})
		self.assertEqual(result["created_count"], 2)
		self.assertEqual(result["failed_count"], 1)
		self.assertEqual(result["created_names"], ["TODO-0001", "TODO-0003"])
		self.assertEqual(result["failed"], [{"row": 2, "error": "Missing description"}])
		self.assertIn("Created 2 of 3 ToDo records.", result["message"])
		self.assertIn("row 2: Missing description", result["message"])

	def test_source_code_analyzer_subagent_descriptor_has_empty_tools(self):
		runner = self.make_runner(allow_code_search=True, mode="Ask")
		subagents = runner._build_subagents()
		analyzer = next(sub for sub in subagents if sub["name"] == "source-code-analyzer")

		self.assertEqual(analyzer["tools"], [])
		# The descriptor must not inherit any parent proposal/mutation tools.
		self.assertNotIn("insert", {getattr(t, "__name__", "") for t in analyzer["tools"]})
		self.assertNotIn("save", {getattr(t, "__name__", "") for t in analyzer["tools"]})

	def test_source_code_analyzer_subagent_has_response_format(self):
		from ask_alyf.ask_alyf.subagents import SourceCodeAnalysisResult

		runner = self.make_runner(allow_code_search=True, mode="Ask")
		analyzer = next(sub for sub in runner._build_subagents() if sub["name"] == "source-code-analyzer")
		self.assertIs(analyzer["response_format"], SourceCodeAnalysisResult)

	def test_document_planner_subagent_descriptor_has_read_only_tools(self):
		runner = self.make_runner(allow_code_search=False, mode="Agent")
		subagents = runner._build_subagents()
		planner = next(sub for sub in subagents if sub["name"] == "document-planner")

		tool_names = {getattr(t, "__name__", "") for t in planner["tools"]}
		# Only read-only metadata/tools are allowed.
		self.assertIn("get_meta", tool_names)
		self.assertIn("get", tool_names)
		# No proposal or mutation tools leak into the planner.
		for forbidden in ("insert", "save", "set_value", "submit", "cancel", "delete", "batch_insert"):
			self.assertNotIn(forbidden, tool_names)
		# No frontend action tools either.
		for forbidden in ("set_route", "new_doc", "show_chart", "frm_set_value"):
			self.assertNotIn(forbidden, tool_names)

	def test_document_planner_subagent_has_response_format(self):
		from ask_alyf.ask_alyf.subagents import DocumentPlannerResult

		runner = self.make_runner(allow_code_search=False, mode="Agent")
		planner = next(sub for sub in runner._build_subagents() if sub["name"] == "document-planner")
		self.assertIs(planner["response_format"], DocumentPlannerResult)

	def test_harness_profile_excludes_write_edit_execute(self):
		from ask_alyf.ask_alyf.agent import _ensure_ask_alyf_harness_profile

		self.assertEqual(
			ASK_ALYF_EXCLUDED_TOOLS,
			frozenset({"write_file", "edit_file", "execute"}),
		)
		# Registration is idempotent and safe to call repeatedly.
		_ensure_ask_alyf_harness_profile()
		_ensure_ask_alyf_harness_profile()

	def test_build_permissions_denies_write_everywhere(self):
		from deepagents import FilesystemPermission

		runner = self.make_runner(allow_code_search=False, mode="Ask")
		permissions = runner._build_permissions()
		self.assertEqual(len(permissions), 1)
		perm = permissions[0]
		self.assertIsInstance(perm, FilesystemPermission)
		self.assertEqual(perm.operations, ["write"])
		self.assertEqual(perm.paths, ["/**"])
		self.assertEqual(perm.mode, "deny")

	def test_coordinator_exposes_built_in_write_todos_tool(self):
		"""The Deep Agents coordinator must include the built-in `write_todos`
		planning tool in its model-visible tool surface. `write_todos` is added
		by `TodoListMiddleware` during agent assembly and is the planning
		mechanism (filesystem writes are denied by the harness profile and
		permissions).
		"""
		from deepagents import create_deep_agent
		from langchain_openai import ChatOpenAI

		from ask_alyf.ask_alyf.deep_agent_backend import build_ask_alyf_backend

		runner = self.make_runner(allow_code_search=False, mode="Ask")
		agent = create_deep_agent(
			model=ChatOpenAI(model="dummy", api_key="dummy"),
			tools=runner._build_tools(),
			system_prompt="dummy",
			backend=build_ask_alyf_backend({}),
			subagents=runner._build_subagents(),
			permissions=runner._build_permissions(),
			name="ask_alyf",
		)
		tool_names = set(agent.nodes["tools"].bound.tools_by_name.keys())
		self.assertIn("write_todos", tool_names)

	def test_history_item_to_native_message_maps_roles(self):
		self.assertIsInstance(
			history_item_to_native_message({"role": "user", "content": "hi"}),
			HumanMessage,
		)
		self.assertIsInstance(
			history_item_to_native_message({"role": "assistant", "content": "ok"}),
			AIMessage,
		)
		self.assertIsInstance(
			history_item_to_native_message({"role": "system", "content": "note"}),
			SystemMessage,
		)
		self.assertIsNone(history_item_to_native_message({"role": "user", "content": ""}))

	def test_history_item_to_native_message_appends_extraction_metadata(self):
		item = {
			"role": "user",
			"content": "What is on this invoice?",
			"metadata": {
				"files": [{"name": "FILE-0001", "file_name": "invoice.pdf"}],
				"document_extractions": [
					{
						"file_id": "FILE-0001",
						"file_name": "invoice.pdf",
						"pages_processed": 2,
						"total_pages": 2,
						"extraction_prompt": "Extract line items and totals.",
						"extracted_data": {"supplier": "ACME", "total": "123.45"},
					}
				],
			},
		}
		message = history_item_to_native_message(item)
		self.assertIsInstance(message, HumanMessage)
		text = message.content
		self.assertIn("What is on this invoice?", text)
		self.assertIn("Stored document extraction: id=FILE-0001, name=invoice.pdf", text)
		self.assertIn("Extraction request: Extract line items and totals.", text)
		self.assertIn('"supplier": "ACME"', text)
		self.assertIn('"total": "123.45"', text)
		# File URLs are never leaked into the native message context.
		self.assertNotIn("/private/files/", text)
		self.assertNotIn("/files/", text)

	def test_build_input_messages_rebuilds_full_history(self):
		runner = self.make_runner(allow_code_search=False, mode="Ask")
		runner.runtime.conversation_history = [
			{"role": "user", "content": "one"},
			{"role": "assistant", "content": "one-ans"},
			{"role": "system", "content": "result"},
		]
		messages = runner._build_input_messages("two")
		# Full history is rebuilt as native messages + the new user turn.
		self.assertEqual(len(messages), 4)
		self.assertIsInstance(messages[0], HumanMessage)
		self.assertIsInstance(messages[1], AIMessage)
		self.assertIsInstance(messages[2], SystemMessage)
		self.assertIsInstance(messages[-1], HumanMessage)
		self.assertEqual(messages[-1].content, "two")

	def test_run_preserves_result_envelope_and_proposal_shapes(self):
		runner = self.make_runner(allow_code_search=False, mode="Agent")
		runner.agent = SimpleNamespace(
			invoke=lambda _input, config=None: {"messages": [SimpleNamespace(content="Done.")]}
		)
		result = runner.run("do something", conversation_history=[])
		self.assertEqual(result["response"], "Done.")
		self.assertEqual(result["pending_operations"], [])
		self.assertEqual(result["document_extractions"], [])
		self.assertEqual(result["attached_files"], [])

	def test_agent_mode_tools_include_proposals_but_no_host_mutation_or_shell(self):
		"""Model-visible tools must contain proposal operations but no direct
		Frappe mutation, host filesystem, delete-of-host, or shell execution."""
		host_mutation_tools = {"write_file", "edit_file", "execute", "shell", "run_shell"}
		proposal_tools = {
			"insert",
			"batch_insert",
			"save",
			"set_value",
			"submit",
			"cancel",
			"amend",
			"delete",
			"rename_doc",
			"attach_file",
			"run_whitelisted_method",
		}

		agent_runner = self.make_runner(allow_code_search=False, mode="Agent")
		agent_tool_names = {tool.__name__ for tool in agent_runner._build_tools()}
		# Proposal ops are present (they only create pending proposals, never mutate directly).
		self.assertTrue(proposal_tools.issubset(agent_tool_names))
		# No host filesystem, shell, or direct execution capability leaks in.
		self.assertFalse(host_mutation_tools.intersection(agent_tool_names))

	def test_ask_mode_tools_exclude_all_write_proposals_and_host_mutation(self):
		host_mutation_tools = {"write_file", "edit_file", "execute", "shell", "run_shell"}
		proposal_tools = {"insert", "save", "set_value", "submit", "cancel", "delete", "batch_insert"}

		ask_runner = self.make_runner(allow_code_search=False, mode="Ask")
		ask_tool_names = {tool.__name__ for tool in ask_runner._build_tools()}
		self.assertFalse(proposal_tools.intersection(ask_tool_names))
		self.assertFalse(host_mutation_tools.intersection(ask_tool_names))

	def test_clear_messages_wrapper_connects_without_changing_user(self):
		inherited_db = frappe.local.db
		private_db = MagicMock()

		def connect(*, set_admin_as_user):
			self.assertFalse(set_admin_as_user)
			frappe.local.db = private_db

		wrapped = clear_messages_on_tool_error(lambda: frappe.session.user)
		with (
			patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect),
			patch("ask_alyf.ask_alyf.toolset.frappe.set_user") as set_user,
		):
			result = wrapped()

		self.assertEqual(result, frappe.session.user)
		set_user.assert_not_called()
		private_db.commit.assert_called_once_with()
		private_db.close.assert_called_once_with()
		self.assertIs(frappe.local.db, inherited_db)

	def test_clear_messages_wrapper_isolates_concurrent_copied_contexts(self):
		inherited_db = frappe.local.db
		inherited_currently_saving = list(frappe.local.flags.currently_saving)
		inherited_response_docs = list(frappe.local.response.docs)
		inherited_message_log = list(frappe.local.message_log)
		connections = []
		connection_barrier = threading.Barrier(2)
		tool_barrier = threading.Barrier(2)
		connections_lock = threading.Lock()

		def connect(*, set_admin_as_user):
			self.assertFalse(set_admin_as_user)
			private_db = MagicMock()
			with connections_lock:
				connections.append(private_db)
			frappe.local.db = private_db
			connection_barrier.wait(timeout=5)

		def tool():
			bound_db = frappe.local.db
			marker = id(bound_db)
			frappe.local.flags.tool_marker = marker
			frappe.local.flags.currently_saving.append(marker)
			frappe.local.session.tool_marker = marker
			frappe.local.response.tool_marker = marker
			frappe.local.response.docs.append(marker)
			frappe.local.message_log.append(marker)
			tool_barrier.wait(timeout=5)
			return {
				"db": bound_db,
				"currently_saving": list(frappe.local.flags.currently_saving),
				"response_docs": list(frappe.local.response.docs),
				"message_log": list(frappe.local.message_log),
			}

		wrapped = clear_messages_on_tool_error(tool)
		contexts = [contextvars.copy_context(), contextvars.copy_context()]

		with (
			patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect),
			ThreadPoolExecutor(max_workers=2) as executor,
		):
			futures = [executor.submit(context.run, wrapped) for context in contexts]
			results = [future.result(timeout=5) for future in futures]

		self.assertEqual(
			{id(result["db"]) for result in results}, {id(connection) for connection in connections}
		)
		for result in results:
			marker = id(result["db"])
			self.assertEqual(result["currently_saving"], [*inherited_currently_saving, marker])
			self.assertEqual(result["response_docs"], [*inherited_response_docs, marker])
			self.assertEqual(result["message_log"], [*inherited_message_log, marker])
		for connection in connections:
			connection.commit.assert_called_once_with()
			connection.close.assert_called_once_with()
		self.assertIs(frappe.local.db, inherited_db)
		self.assertNotIn("tool_marker", frappe.local.flags)
		self.assertNotIn("tool_marker", frappe.local.session)
		self.assertNotIn("tool_marker", frappe.local.response)
		self.assertEqual(frappe.local.flags.currently_saving, inherited_currently_saving)
		self.assertEqual(frappe.local.response.docs, inherited_response_docs)
		self.assertEqual(frappe.local.message_log, inherited_message_log)

	def test_clear_messages_wrapper_propagates_commit_failure(self):
		inherited_db = frappe.local.db
		private_db = MagicMock()
		private_db.commit.side_effect = RuntimeError("commit failed")

		def connect(*, set_admin_as_user):
			frappe.local.db = private_db

		wrapped = clear_messages_on_tool_error(lambda: "done")
		with (
			patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect),
			patch("ask_alyf.ask_alyf.toolset.frappe.clear_messages") as clear_messages,
			self.assertRaisesRegex(RuntimeError, "commit failed"),
		):
			wrapped()

		private_db.rollback.assert_called_once_with()
		private_db.close.assert_called_once_with()
		clear_messages.assert_called_once_with()
		self.assertIs(frappe.local.db, inherited_db)

	def test_clear_messages_wrapper_preserves_async_tools(self):
		async def fake_tool(file_id):
			return {"file_id": file_id}

		wrapped = clear_messages_on_tool_error(fake_tool)
		result = asyncio.run(wrapped("FILE-0001"))

		self.assertTrue(asyncio.iscoroutinefunction(wrapped))
		self.assertEqual(result, {"file_id": "FILE-0001"})

	def test_clear_messages_wrapper_clears_messages_for_async_tool_errors(self):
		async def fake_tool():
			frappe.local.message_log.append("tool message")
			raise RuntimeError("boom")

		wrapped = clear_messages_on_tool_error(fake_tool)
		inherited_message_log = frappe.local.message_log
		original_messages = list(inherited_message_log)
		inherited_message_log.append("parent message")

		try:
			with patch(
				"ask_alyf.ask_alyf.toolset.frappe.clear_messages", wraps=frappe.clear_messages
			) as clear_messages:
				with self.assertRaisesRegex(RuntimeError, "boom"):
					asyncio.run(wrapped())

			clear_messages.assert_called_once_with()
			self.assertIs(frappe.local.message_log, inherited_message_log)
			self.assertEqual(inherited_message_log, [*original_messages, "parent message"])
		finally:
			frappe.local.message_log = inherited_message_log
			inherited_message_log[:] = original_messages
