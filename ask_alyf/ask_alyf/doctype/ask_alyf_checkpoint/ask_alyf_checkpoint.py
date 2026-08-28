# Copyright (c) 2026, ALYF GmbH and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from ask_alyf.ask_alyf.checkpointer import checkpoint_row_name


class AskALYFCheckpoint(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		checkpoint_data: DF.LongText | None
		checkpoint_id: DF.Data
		checkpoint_ns: DF.Data | None
		checkpoint_type: DF.Data | None
		metadata_data: DF.LongText | None
		metadata_type: DF.Data | None
		parent_checkpoint_id: DF.Data | None
		thread_id: DF.Data
	# end: auto-generated types

	def autoname(self):
		# Deterministic name, so the primary key enforces one row per
		# (thread, namespace, checkpoint) and writes can upsert without a lock.
		self.name = checkpoint_row_name(self.thread_id, self.checkpoint_ns, self.checkpoint_id)
