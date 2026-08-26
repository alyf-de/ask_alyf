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
from ask_alyf.ask_alyf.agent import ASK_ALYF_EXCLUDED_TOOLS, ask_alyfAgentRunner, build_chat_model
from ask_alyf.ask_alyf.history import history_item_to_native_message
from ask_alyf.ask_alyf.toolset import ask_alyfToolset, clear_messages_on_tool_error


class FakeSettings(SimpleNamespace):
	def __init__(self, *, allow_code_search: bool, reasoning_effort: str = ""):
		super().__init__(
			allow_code_search=allow_code_search,
			system_prompt="",
			model="gpt-test",
			llm_provider="OpenAI",
			base_url="",
			reasoning_effort=reasoning_effort,
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

	def test_build_chat_model_uses_responses_api_for_any_model(self):
		model = build_chat_model(FakeSettings(allow_code_search=False))

		self.assertTrue(model.use_responses_api)
		self.assertIsNone(model.reasoning_effort)

	def test_build_chat_model_passes_reasoning_effort_when_set(self):
		model = build_chat_model(FakeSettings(allow_code_search=False, reasoning_effort="high"))

		self.assertEqual(model.reasoning_effort, "high")

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

	def test_get_list_preserves_aggregate_field_dict(self):
		fields = [{"SUM": "grand_total", "as": "total"}]
		with patch("ask_alyf.ask_alyf.tools.client.get_list", return_value=[]) as get_list:
			result = tools.get_list("Sales Invoice", fields=fields, filters={"docstatus": 1})

		get_list.assert_called_once_with(
			doctype="Sales Invoice",
			fields=fields,
			filters={"docstatus": 1},
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
			invoke=lambda _input, config=None: {"messages": [AIMessage(content="Done.")]}
		)
		result = runner.run("do something", conversation_history=[])
		self.assertEqual(result["response"], "Done.")
		self.assertEqual(result["pending_operations"], [])
		self.assertEqual(result["document_extractions"], [])
		self.assertEqual(result["attached_files"], [])

	def test_run_extracts_text_from_responses_api_content_blocks(self):
		runner = self.make_runner(allow_code_search=False)
		response = AIMessage(
			content=[
				{"type": "reasoning", "summary": [], "content": [], "encrypted_content": "secret"},
				{"type": "text", "text": ":)", "annotations": [], "phase": "final_answer"},
			]
		)
		runner.agent = SimpleNamespace(invoke=lambda _input, config=None: {"messages": [response]})

		result = runner.run("hello", conversation_history=[])

		self.assertEqual(result["response"], ":)")

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

	def test_tool_wrapper_uses_public_frappe_lifecycle_and_caller_identity(self):
		parent_db = frappe.local.db
		parent_message_log = frappe.local.message_log
		caller = {
			"site": frappe.local.site,
			"sites_path": frappe.local.sites_path,
			"user": frappe.session.user,
			"language": frappe.local.lang,
		}
		parent_local_ids = {
			"flags": id(frappe.local.flags),
			"session": id(frappe.local.session),
			"response": id(frappe.local.response),
			"message_log": id(parent_message_log),
		}
		private_db = MagicMock()
		events = []
		original_init = frappe.init
		original_set_user = frappe.set_user
		original_destroy = frappe.destroy

		private_db.commit.side_effect = lambda: events.append("commit")

		def init(site, *, sites_path):
			events.append("init")
			original_init(site, sites_path=sites_path)

		def connect(*, set_admin_as_user):
			events.append("connect")
			self.assertFalse(set_admin_as_user)
			frappe.local.db = private_db

		def set_user(user):
			events.append("set_user")
			original_set_user(user)

		def destroy():
			events.append("destroy")
			original_destroy()

		def tool():
			events.append("tool")
			return {
				"site": frappe.local.site,
				"user": frappe.session.user,
				"language": frappe.local.lang,
				"local_ids": {
					"flags": id(frappe.local.flags),
					"session": id(frappe.local.session),
					"response": id(frappe.local.response),
					"message_log": id(frappe.local.message_log),
				},
			}

		wrapped = clear_messages_on_tool_error(tool)
		with (
			patch("ask_alyf.ask_alyf.toolset.frappe.init", side_effect=init) as init_mock,
			patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect) as connect_mock,
			patch("ask_alyf.ask_alyf.toolset.frappe.set_user", side_effect=set_user) as set_user_mock,
			patch("ask_alyf.ask_alyf.toolset.frappe.destroy", side_effect=destroy) as destroy_mock,
		):
			result = wrapped()

		self.assertEqual(result["site"], caller["site"])
		self.assertEqual(result["user"], caller["user"])
		self.assertEqual(result["language"], caller["language"])
		for key, parent_id in parent_local_ids.items():
			self.assertNotEqual(result["local_ids"][key], parent_id)
		self.assertEqual(events, ["init", "connect", "set_user", "tool", "commit", "destroy"])
		init_mock.assert_called_once_with(caller["site"], sites_path=caller["sites_path"])
		connect_mock.assert_called_once_with(set_admin_as_user=False)
		set_user_mock.assert_called_once_with(caller["user"])
		destroy_mock.assert_called_once_with()
		private_db.close.assert_called_once_with()
		self.assertIs(frappe.local.db, parent_db)
		self.assertIs(frappe.local.message_log, parent_message_log)

	def test_tool_wrapper_isolates_concurrent_copied_contexts(self):
		parent_db = frappe.local.db
		parent_flags = frappe.local.flags
		parent_session = frappe.local.session
		parent_response = frappe.local.response
		parent_message_log = frappe.local.message_log
		parent_currently_saving = list(parent_flags.currently_saving)
		parent_response_docs = list(parent_response.docs)
		parent_messages = list(parent_message_log)
		parent_message_log.append("parent message")
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
				"local_ids": {
					"flags": id(frappe.local.flags),
					"session": id(frappe.local.session),
					"response": id(frappe.local.response),
					"message_log": id(frappe.local.message_log),
				},
				"currently_saving": list(frappe.local.flags.currently_saving),
				"response_docs": list(frappe.local.response.docs),
				"message_log": list(frappe.local.message_log),
			}

		wrapped = clear_messages_on_tool_error(tool)
		contexts = [contextvars.copy_context(), contextvars.copy_context()]

		try:
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
				self.assertEqual(result["currently_saving"], [marker])
				self.assertEqual(result["response_docs"], [marker])
				self.assertEqual(result["message_log"], [marker])
			for key in ("flags", "session", "response", "message_log"):
				self.assertEqual(len({result["local_ids"][key] for result in results}), 2)
			for connection in connections:
				connection.commit.assert_called_once_with()
				connection.close.assert_called_once_with()
			self.assertIs(frappe.local.db, parent_db)
			self.assertIs(frappe.local.flags, parent_flags)
			self.assertIs(frappe.local.session, parent_session)
			self.assertIs(frappe.local.response, parent_response)
			self.assertIs(frappe.local.message_log, parent_message_log)
			self.assertEqual(parent_flags.currently_saving, parent_currently_saving)
			self.assertEqual(parent_response.docs, parent_response_docs)
			self.assertEqual(parent_message_log, [*parent_messages, "parent message"])
		finally:
			parent_message_log[:] = parent_messages

	def test_tool_wrapper_rolls_back_commit_failure_without_touching_parent_messages(self):
		parent_db = frappe.local.db
		parent_message_log = frappe.local.message_log
		parent_messages = list(parent_message_log)
		parent_message_log.append("parent message")
		private_db = MagicMock()
		private_db.commit.side_effect = RuntimeError("commit failed")

		def connect(*, set_admin_as_user):
			self.assertFalse(set_admin_as_user)
			frappe.local.db = private_db

		wrapped = clear_messages_on_tool_error(lambda: "done")
		try:
			with (
				patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect),
				patch(
					"ask_alyf.ask_alyf.toolset.frappe.clear_messages", wraps=frappe.clear_messages
				) as clear_messages,
				self.assertRaisesRegex(RuntimeError, "commit failed"),
			):
				wrapped()

			private_db.rollback.assert_called_once_with()
			private_db.close.assert_called_once_with()
			clear_messages.assert_called_once_with()
			self.assertIs(frappe.local.db, parent_db)
			self.assertIs(frappe.local.message_log, parent_message_log)
			self.assertEqual(parent_message_log, [*parent_messages, "parent message"])
		finally:
			parent_message_log[:] = parent_messages

	def test_tool_wrapper_does_not_mask_tool_error_when_destroy_fails(self):
		parent_db = frappe.local.db
		private_db = MagicMock()
		original_destroy = frappe.destroy

		def connect(*, set_admin_as_user):
			self.assertFalse(set_admin_as_user)
			frappe.local.db = private_db

		def destroy():
			original_destroy()
			raise RuntimeError("destroy failed")

		def tool():
			raise ValueError("tool failed")

		wrapped = clear_messages_on_tool_error(tool)
		with (
			patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect),
			patch("ask_alyf.ask_alyf.toolset.frappe.destroy", side_effect=destroy),
			self.assertRaisesRegex(ValueError, "tool failed"),
		):
			wrapped()

		private_db.rollback.assert_called_once_with()
		private_db.close.assert_called_once_with()
		self.assertIs(frappe.local.db, parent_db)

	def test_tool_wrapper_preserves_async_tools_and_context_across_await(self):
		parent_db = frappe.local.db
		private_db = MagicMock()

		def connect(*, set_admin_as_user):
			self.assertFalse(set_admin_as_user)
			frappe.local.db = private_db

		async def fake_tool(file_id):
			private_state = (id(frappe.local.db), id(frappe.local.flags))
			await asyncio.sleep(0)
			self.assertEqual((id(frappe.local.db), id(frappe.local.flags)), private_state)
			return {"file_id": file_id}

		wrapped = clear_messages_on_tool_error(fake_tool)
		with patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect):
			result = asyncio.run(wrapped("FILE-0001"))

		self.assertTrue(asyncio.iscoroutinefunction(wrapped))
		self.assertEqual(result, {"file_id": "FILE-0001"})
		private_db.commit.assert_called_once_with()
		private_db.close.assert_called_once_with()
		self.assertIs(frappe.local.db, parent_db)

	def test_tool_wrapper_isolates_async_tool_errors_and_parent_messages(self):
		parent_db = frappe.local.db
		parent_message_log = frappe.local.message_log
		parent_messages = list(parent_message_log)
		parent_message_log.append("parent message")
		private_db = MagicMock()

		def connect(*, set_admin_as_user):
			self.assertFalse(set_admin_as_user)
			frappe.local.db = private_db

		async def fake_tool():
			frappe.local.message_log.append("tool message")
			await asyncio.sleep(0)
			raise RuntimeError("boom")

		wrapped = clear_messages_on_tool_error(fake_tool)
		try:
			with (
				patch("ask_alyf.ask_alyf.toolset.frappe.connect", side_effect=connect),
				patch(
					"ask_alyf.ask_alyf.toolset.frappe.clear_messages", wraps=frappe.clear_messages
				) as clear_messages,
				self.assertRaisesRegex(RuntimeError, "boom"),
			):
				asyncio.run(wrapped())

			private_db.commit.assert_not_called()
			private_db.rollback.assert_called_once_with()
			private_db.close.assert_called_once_with()
			clear_messages.assert_called_once_with()
			self.assertIs(frappe.local.db, parent_db)
			self.assertIs(frappe.local.message_log, parent_message_log)
			self.assertEqual(parent_message_log, [*parent_messages, "parent message"])
		finally:
			parent_message_log[:] = parent_messages
