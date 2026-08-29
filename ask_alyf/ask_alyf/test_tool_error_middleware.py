# Copyright (c) 2026, ALYF GmbH and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe import _
from frappe.tests import UnitTestCase
from langchain_core.messages import ToolMessage

from ask_alyf.ask_alyf import api
from ask_alyf.ask_alyf.agent import (
	ToolCallLogMiddleware,
	_on_tool_error,
	build_tool_error_middleware,
)
from ask_alyf.ask_alyf.toolset import ask_alyfRuntime
from ask_alyf.ask_alyf.utils import dumps, loads


def _tool_call_request(tool_name: str = "get_meta"):
	return SimpleNamespace(tool_call={"name": tool_name, "args": {}, "id": "call-1"})


class UnitTestToolErrorMiddleware(UnitTestCase):
	def test_on_tool_error_includes_validation_message(self):
		exc = frappe.ValidationError("DocType 'Secret' is excluded from Agent mode.")
		content = _on_tool_error(exc, _tool_call_request("get_meta"))
		self.assertIn("`get_meta`", content)
		self.assertIn("ValidationError", content)
		self.assertIn("excluded from Agent mode", content)
		self.assertIn("retry", content)

	def test_on_tool_error_includes_permission_message(self):
		exc = frappe.PermissionError("Not permitted to read Customer.")
		content = _on_tool_error(exc, _tool_call_request("get"))
		self.assertIn("`get`", content)
		self.assertIn("PermissionError", content)
		self.assertIn("Not permitted to read Customer", content)

	def test_on_tool_error_sanitizes_unexpected_exceptions(self):
		exc = RuntimeError("connection string postgres://user:secret@db/erp")
		content = _on_tool_error(exc, _tool_call_request("run_read_only_sql"))
		self.assertIn("`run_read_only_sql`", content)
		self.assertIn("RuntimeError", content)
		self.assertNotIn("secret", content)
		self.assertNotIn("postgres://", content)

	def test_a_stopped_run_skips_the_rest_of_the_planned_tool_calls(self):
		# The model plans several calls at once. Once the user has pressed stop,
		# none of the ones that have not started yet may run.
		runtime = ask_alyfRuntime(
			conversation_name="TEST-CONVERSATION", mode="Ask", request_context={}, run_id="run-1"
		)
		middleware = ToolCallLogMiddleware(runtime)
		request = SimpleNamespace(
			tool=SimpleNamespace(name="get_meta"),
			tool_call={"name": "get_meta", "args": {}, "id": "call-1"},
		)

		def handler(_request):
			raise AssertionError("the tool ran after the user pressed stop")

		with patch("ask_alyf.ask_alyf.agent.is_stop_requested", return_value=True):
			result = middleware.wrap_tool_call(request, handler)

		# The call still gets its answer, so the stored thread stays valid.
		self.assertIsInstance(result, ToolMessage)
		self.assertEqual(result.tool_call_id, "call-1")
		self.assertIn("Stopped", result.content)
		self.assertTrue(runtime.stop_requested)
		# A call that never ran is not a step worth showing.
		self.assertEqual(runtime.tool_calls, [])

	def test_tool_error_middleware_returns_error_tool_message_instead_of_raising(self):
		middleware = build_tool_error_middleware()
		request = SimpleNamespace(
			tool=SimpleNamespace(name="boom"),
			tool_call={"name": "boom", "args": {}, "id": "call-1"},
		)

		def handler(_request):
			frappe.throw(_("DocType is excluded"))

		result = middleware.wrap_tool_call(request, handler)

		self.assertIsInstance(result, ToolMessage)
		self.assertEqual(result.status, "error")
		self.assertEqual(result.name, "boom")
		self.assertEqual(result.tool_call_id, "call-1")
		self.assertIn("ValidationError", result.content)
		self.assertIn(_("DocType is excluded"), result.content)

	def test_process_message_job_preserves_validation_error_message(self):
		user_message = api.make_message("user", "Hello", mode=api.MODE_ASK)
		conversation = frappe.get_doc(
			doctype="Ask ALYF Conversation",
			title="Configuration Error Test",
			status="Active",
			messages_json=dumps([user_message]),
		)
		conversation.insert(ignore_permissions=True)
		error_message = _("Configure an API key in Ask ALYF Settings before sending messages.")

		with patch(
			"ask_alyf.ask_alyf.api.run_message",
			side_effect=frappe.ValidationError(error_message),
		):
			with patch("ask_alyf.ask_alyf.api.frappe.publish_realtime"):
				api.process_message_job(
					conversation_name=conversation.name,
					message="Hello",
					mode=api.MODE_ASK,
					context_data={},
					user_message_id=user_message["id"],
				)

		conversation.reload()
		messages = loads(conversation.messages_json, [])
		self.assertEqual(messages[-1]["content"], error_message)

	def test_process_message_job_uses_generic_error_message(self):
		user_message = api.make_message("user", "Break please", mode=api.MODE_ASK)
		conversation = frappe.get_doc(
			doctype="Ask ALYF Conversation",
			title="Tool Error Test",
			status="Active",
			messages_json=dumps([user_message]),
		)
		conversation.insert(ignore_permissions=True)

		with patch(
			"ask_alyf.ask_alyf.api.run_message",
			side_effect=RuntimeError("secret internal detail"),
		):
			with patch("ask_alyf.ask_alyf.api.frappe.publish_realtime"):
				api.process_message_job(
					conversation_name=conversation.name,
					message="Break please",
					mode=api.MODE_ASK,
					context_data={},
					user_message_id=user_message["id"],
				)

		conversation.reload()
		messages = loads(conversation.messages_json, [])
		self.assertTrue(messages)
		assistant = messages[-1]
		self.assertEqual(assistant["role"], "assistant")
		self.assertEqual(
			assistant["content"],
			_("I hit an error while processing that request. Please try again."),
		)
		self.assertNotIn("secret", assistant["content"])
