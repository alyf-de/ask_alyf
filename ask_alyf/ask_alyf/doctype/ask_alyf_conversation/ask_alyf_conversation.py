import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class AskALYFConversation(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		last_context_json: DF.Code | None
		last_message_at: DF.Datetime | None
		messages_json: DF.Code | None
		pending_operation_json: DF.Code | None
		route: DF.Data | None
		status: DF.Literal["Active", "Closed"]
		title: DF.Data
	# end: auto-generated types

	def on_trash(self):
		# Imported here: the checkpointer pulls in LangGraph, which the desk
		# requests that merely read conversations should not pay for.
		from ask_alyf.ask_alyf.checkpointer import delete_checkpoints

		delete_checkpoints([self.name])

	@staticmethod
	def clear_old_logs(days: int = 90):
		from ask_alyf.ask_alyf.checkpointer import delete_checkpoints

		table = frappe.qb.DocType("Ask ALYF Conversation")
		expired = table.creation < (Now() - Interval(days=days))
		# A bulk delete skips `on_trash`, so the checkpoints go first.
		delete_checkpoints(frappe.qb.from_(table).select(table.name).where(expired).run(pluck=True))
		frappe.db.delete(table, filters=expired)
