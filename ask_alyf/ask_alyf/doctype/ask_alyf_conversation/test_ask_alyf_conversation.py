# Copyright (c) 2026, ALYF GmbH and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from ask_alyf.ask_alyf import agent, api, tools
from ask_alyf.ask_alyf.utils import dumps, loads


class UnitTestAskALYFConversation(UnitTestCase):
	def make_conversation(
		self, *, messages: list[dict] | None = None, pending_operations: list[dict] | None = None
	):
		doc = frappe.get_doc(
			doctype="Ask ALYF Conversation",
			title="Test Conversation",
			status="Active",
			messages_json=dumps(messages or []),
			pending_operation_json=dumps(pending_operations) if pending_operations else "",
		)
		doc.insert(ignore_permissions=True)
		return doc

	def test_process_message_job_publishes_pending_operations(self):
		user_message = api.make_message("user", "Open Sales Invoice list", mode=api.MODE_ASK)
		conversation = self.make_conversation(messages=[user_message])
		expected_operation = {
			"kind": "frontend_action",
			"tool": "set_route",
			"summary": "Navigate to Sales Invoice list",
			"requires_confirmation": False,
			"payload": {"route": ["List", "Sales Invoice"]},
			"call_id": "call-123",
		}
		realtime_calls = []

		def record_realtime(event, payload=None, user=None, **kwargs):
			realtime_calls.append({"event": event, "payload": payload, "user": user, "kwargs": kwargs})

		with patch(
			"ask_alyf.ask_alyf.api.run_message",
			return_value={"response": "Done", "pending_operations": [expected_operation]},
		):
			with patch("ask_alyf.ask_alyf.api.frappe.publish_realtime", side_effect=record_realtime):
				api.process_message_job(
					conversation_name=conversation.name,
					message="Open Sales Invoice list",
					mode=api.MODE_ASK,
					context_data={},
					user_message_id=user_message["id"],
				)

		conversation.reload()
		payload = api.conversation_payload(conversation)
		messages = payload["messages"]
		self.assertTrue(messages)
		assistant = messages[-1]
		expected_with_id = {**expected_operation, "assistant_message_id": assistant["id"]}
		self.assertEqual(payload["pending_operations"], [expected_with_id])

		complete_events = [call for call in realtime_calls if call["event"] == "ask_alyf_response_complete"]
		self.assertEqual(len(complete_events), 1)
		self.assertEqual(complete_events[0]["payload"]["pending_operations"], [expected_with_id])

	def test_process_message_job_persists_document_extractions(self):
		user_message = api.make_message("user", "What is on this invoice?", mode=api.MODE_ASK)
		conversation = self.make_conversation(messages=[user_message])
		document_extractions = [
			{
				"file_id": "FILE-0001",
				"file_name": "invoice.pdf",
				"pages_processed": 2,
				"total_pages": 2,
				"truncated": False,
				"warning": "",
				"extraction_prompt": "Extract line items and totals.",
				"extracted_data": {"supplier": "ACME", "total": "123.45"},
			}
		]

		with patch(
			"ask_alyf.ask_alyf.api.run_message",
			return_value={
				"response": "I found the supplier and total.",
				"pending_operations": [],
				"document_extractions": document_extractions,
			},
		):
			api.process_message_job(
				conversation_name=conversation.name,
				message="What is on this invoice?",
				mode=api.MODE_ASK,
				context_data={},
				user_message_id=user_message["id"],
			)

		conversation.reload()
		messages = loads(conversation.messages_json, [])
		self.assertTrue(messages)
		self.assertEqual(messages[-1]["content"], "I found the supplier and total.")
		self.assertEqual(messages[-1]["metadata"].get("document_extractions"), document_extractions)

		prompt = agent._history_item_to_native_message(
			{
				"role": "user",
				"content": "Create the Purchase Invoice from that document.",
				"metadata": {"document_extractions": document_extractions},
			}
		)
		self.assertIsNotNone(prompt)
		self.assertIn("Create the Purchase Invoice from that document.", prompt.content)
		self.assertIn("Stored document extraction:", prompt.content)
		self.assertIn("Extraction request: Extract line items and totals.", prompt.content)
		self.assertIn("id=FILE-0001", prompt.content)
		self.assertNotIn("/private/files/invoice.pdf", prompt.content)
		self.assertIn('"supplier": "ACME"', prompt.content)
		self.assertIn('"total": "123.45"', prompt.content)

	def test_attach_file_persists_file_url_in_message_metadata(self):
		conversation = self.make_conversation(messages=[])
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "invoice.txt",
				"content": "Test Content",
				"is_private": 1,
			}
		).save()

		with patch("ask_alyf.ask_alyf.api.can_access_ask_alyf", return_value=True):
			with patch(
				"ask_alyf.ask_alyf.api.get_settings",
				return_value=SimpleNamespace(allow_file_upload=True),
			):
				response = api.attach_file(conversation=conversation.name, file={"name": file_doc.name})

		messages = response["conversation"]["messages"]
		self.assertTrue(messages)
		file_entry = messages[-1]["metadata"]["files"][0]
		self.assertEqual(file_entry["name"], file_doc.name)
		self.assertEqual(file_entry["file_name"], file_doc.file_name)
		self.assertEqual(file_entry["file_url"], file_doc.file_url)

	def test_parse_json_object_text_accepts_markdown_fenced_json(self):
		parsed = tools._parse_json_object_text(
			"""```json
{"supplier":"ACME","total":"123.45"}
```"""
		)
		self.assertEqual(parsed, {"supplier": "ACME", "total": "123.45"})

	def test_invalid_frontend_operation_rejected_server_side(self):
		with self.assertRaises(frappe.ValidationError):
			tools.execute_pending_operation(
				{
					"kind": "frontend_action",
					"tool": "unsupported_tool",
					"payload": {},
				}
			)

	def test_confirm_pending_operation_executes_backend_path(self):
		pending_operation = {
			"kind": "backend_action",
			"tool": "set_value",
			"summary": "Set status",
			"requires_confirmation": True,
			"payload": {"doctype": "ToDo", "name": "TODO-0001", "fieldname": "status", "value": "Closed"},
			"call_id": "call-backend-1",
		}
		conversation = self.make_conversation(messages=[], pending_operations=[pending_operation])
		realtime_calls = []

		def record_realtime(event, payload=None, user=None, **kwargs):
			realtime_calls.append({"event": event, "payload": payload, "user": user, "kwargs": kwargs})

		with patch("ask_alyf.ask_alyf.api.can_access_ask_alyf", return_value=True):
			with patch(
				"ask_alyf.ask_alyf.api.execute_pending_operation",
				return_value={"name": "TODO-0001", "message": "Updated"},
			) as execute_operation:
				with patch(
					"ask_alyf.ask_alyf.api.run_message",
					return_value={
						"response": "The ToDo TODO-0001 was updated successfully.",
						"pending_operations": [],
					},
				) as summarize_call:
					with patch("ask_alyf.ask_alyf.api.frappe.publish_realtime", side_effect=record_realtime):
						response = api.confirm_pending_operation(
							conversation=conversation.name,
							call_id="call-backend-1",
							mode=api.MODE_ASK,
						)

		execute_operation.assert_called_once_with(pending_operation)
		summarize_call.assert_called_once()
		status_updates = [
			call["payload"]["text"] for call in realtime_calls if call["event"] == "ask_alyf_status"
		]
		self.assertEqual(status_updates, ["Confirming action...", "Generating response...", ""])
		system_message = summarize_call.call_args.kwargs["conversation_history"][-1]
		self.assertEqual(system_message["role"], "system")
		self.assertIn('"status": "success"', system_message["content"])
		self.assertEqual(response["conversation"]["pending_operations"], [])

		conversation.reload()
		messages = loads(conversation.messages_json, [])
		self.assertTrue(messages)
		self.assertEqual(messages[-1]["content"], "The ToDo TODO-0001 was updated successfully.")

	def test_frontend_action_result_clears_pending_operation(self):
		pending_operation = {
			"kind": "frontend_action",
			"tool": "set_route",
			"summary": "Navigate to Sales Invoice list",
			"requires_confirmation": False,
			"payload": {"route": ["List", "Sales Invoice"]},
			"call_id": "call-frontend-1",
		}
		conversation = self.make_conversation(messages=[], pending_operations=[pending_operation])
		realtime_calls = []

		def record_realtime(event, payload=None, user=None, **kwargs):
			realtime_calls.append({"event": event, "payload": payload, "user": user, "kwargs": kwargs})

		with patch("ask_alyf.ask_alyf.api.can_access_ask_alyf", return_value=True):
			with patch(
				"ask_alyf.ask_alyf.api.run_message",
				return_value={
					"response": "Opened the Sales Invoice list view in your browser.",
					"pending_operations": [],
				},
			) as summarize_call:
				with patch("ask_alyf.ask_alyf.api.frappe.publish_realtime", side_effect=record_realtime):
					response = api.frontend_action_result(
						conversation=conversation.name,
						call_id="call-frontend-1",
						status="success",
						mode=api.MODE_ASK,
						result={"route": ["List", "Sales Invoice"]},
					)

		summarize_call.assert_called_once()
		status_updates = [
			call["payload"]["text"] for call in realtime_calls if call["event"] == "ask_alyf_status"
		]
		self.assertEqual(status_updates, ["Generating response...", ""])
		system_message = summarize_call.call_args.kwargs["conversation_history"][-1]
		self.assertEqual(system_message["role"], "system")
		self.assertIn('"status": "success"', system_message["content"])
		self.assertEqual(response["conversation"]["pending_operations"], [])

		conversation.reload()
		messages = loads(conversation.messages_json, [])
		self.assertTrue(messages)
		self.assertEqual(messages[-1]["content"], "Opened the Sales Invoice list view in your browser.")
		self.assertEqual(messages[-1]["metadata"].get("frontend_action_status"), "success")
		self.assertTrue(messages[-1]["metadata"].get("frontend_action_result"))

	def test_show_chart_frontend_action_persists_charts_on_assistant_message(self):
		assistant_message = api.make_message(
			"assistant",
			"Here is your data.",
			mode=api.MODE_ASK,
			pending_operation=True,
		)
		frappe_charts = [
			{
				"type": "bar",
				"title": "Units",
				"height": 300,
				"colors": ["#7cd6fd"],
				"data": {
					"labels": ["A", "B"],
					"datasets": [{"name": "Qty", "values": [1, 2]}],
				},
			},
			{
				"type": "line",
				"title": "",
				"height": 0,
				"colors": [],
				"data": {
					"labels": ["Mon", "Tue"],
					"datasets": [{"name": "", "values": [3, 4]}],
				},
			},
		]
		pending_operation = {
			"kind": "frontend_action",
			"tool": "show_chart",
			"summary": "Show 2 charts",
			"requires_confirmation": False,
			"payload": {"frappe_charts": frappe_charts},
			"call_id": "call-chart-1",
			"assistant_message_id": assistant_message["id"],
		}
		conversation = self.make_conversation(
			messages=[assistant_message],
			pending_operations=[pending_operation],
		)

		with patch("ask_alyf.ask_alyf.api.can_access_ask_alyf", return_value=True):
			response = api.frontend_action_result(
				conversation=conversation.name,
				call_id="call-chart-1",
				status="success",
				mode=api.MODE_ASK,
				result={"tool": "show_chart"},
			)

		self.assertEqual(response["conversation"]["pending_operations"], [])
		conversation.reload()
		messages = loads(conversation.messages_json, [])
		self.assertEqual(len(messages), 1)
		stored = messages[0]["metadata"].get("frappe_charts")
		self.assertTrue(isinstance(stored, list))
		self.assertEqual(len(stored), 2)
		self.assertEqual(stored[0]["type"], "bar")
		self.assertEqual(stored[0]["title"], "Units")
		self.assertEqual(stored[0]["height"], 300)
		self.assertNotIn("height", stored[1])
		self.assertFalse(messages[0]["metadata"].get("pending_operation"))
