# Copyright (c) 2026, ALYF GmbH and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from ask_alyf.ask_alyf.checkpointer import checkpoint_write_row_name


class AskALYFCheckpointWrite(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		channel: DF.Data | None
		checkpoint_id: DF.Data
		checkpoint_ns: DF.Data | None
		idx: DF.Int
		task_id: DF.Data
		task_path: DF.Data | None
		thread_id: DF.Data
		value_data: DF.LongText | None
		value_type: DF.Data | None
	# end: auto-generated types

	def autoname(self):
		# Deterministic name: one row per (thread, namespace, checkpoint, task, index).
		self.name = checkpoint_write_row_name(
			self.thread_id, self.checkpoint_ns, self.checkpoint_id, self.task_id, self.idx
		)
