# Copyright (c) 2026, ALYF GmbH and contributors
# For license information, please see license.txt

"""LangGraph checkpointer backed by the Frappe ORM.

Stores checkpoints in `Ask ALYF Checkpoint` and pending writes in
`Ask ALYF Checkpoint Write`, so an agent thread survives across requests
without a second datastore. The `thread_id` of the graph config is expected to
be the `Ask ALYF Conversation` name.

Three properties worth knowing:

* LangGraph runs `put` and `put_writes` on its background executor, and a
  Frappe database connection belongs to the thread (context) that opened it.
  So writes are buffered in memory and persisted by `flush()`, which the
  caller runs on its own thread once the graph run returns. Reads flush first,
  so the database stays the single source of truth.
* Writes ride the current Frappe transaction — nothing is committed here. If
  the request rolls back, so do the checkpoints, which keeps stored state and
  conversation documents consistent.
* Sync only. Frappe's database connection is not usable from a different
  thread or event loop, so the async `BaseCheckpointSaver` methods are
  deliberately left unimplemented (they raise `NotImplementedError`).
"""

from __future__ import annotations

import hashlib
import threading
from base64 import b64decode, b64encode
from collections.abc import Iterator, Sequence
from typing import Any

import frappe
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
	WRITES_IDX_MAP,
	BaseCheckpointSaver,
	ChannelVersions,
	Checkpoint,
	CheckpointMetadata,
	CheckpointTuple,
	PendingWrite,
	get_checkpoint_id,
	get_checkpoint_metadata,
)

CHECKPOINT_DOCTYPE = "Ask ALYF Checkpoint"
CHECKPOINT_WRITE_DOCTYPE = "Ask ALYF Checkpoint Write"

CHECKPOINT_FIELDS = (
	"thread_id",
	"checkpoint_ns",
	"checkpoint_id",
	"parent_checkpoint_id",
	"checkpoint_type",
	"checkpoint_data",
	"metadata_type",
	"metadata_data",
)


def _row_name(*parts: Any) -> str:
	key = "\x00".join("" if part is None else str(part) for part in parts)
	return hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()


def checkpoint_row_name(thread_id: str, checkpoint_ns: str | None, checkpoint_id: str) -> str:
	"""Name of the `Ask ALYF Checkpoint` row for one checkpoint."""
	return _row_name(thread_id, checkpoint_ns or "", checkpoint_id)


def checkpoint_write_row_name(
	thread_id: str, checkpoint_ns: str | None, checkpoint_id: str, task_id: str, idx: int
) -> str:
	"""Name of the `Ask ALYF Checkpoint Write` row for one pending write."""
	return _row_name(thread_id, checkpoint_ns or "", checkpoint_id, task_id, int(idx))


class FrappeCheckpointSaver(BaseCheckpointSaver[int]):
	"""Checkpointer that persists LangGraph state through the Frappe ORM.

	Usage:

	    saver = FrappeCheckpointSaver()
	    agent = create_deep_agent(..., checkpointer=saver)
	    try:
	        agent.invoke(
	            {"messages": [HumanMessage(content=message)]},
	            config={"configurable": {"thread_id": conversation_name}},
	        )
	    finally:
	        saver.flush()
	"""

	def __init__(self, *, serde=None) -> None:
		super().__init__(serde=serde)
		# Rows written by the graph, keyed by (doctype, name), in write order.
		# Filled from LangGraph's background threads, drained by `flush()` on
		# the thread that owns the Frappe database connection.
		self._buffer: dict[tuple[str, str], dict[str, Any]] = {}
		self._lock = threading.Lock()

	# --- persisting ---------------------------------------------------------

	def flush(self) -> None:
		"""Write buffered checkpoints and writes to the database.

		Call this on the thread that owns the Frappe connection (the request
		thread) after a graph run. Reads call it too, so a checkpoint is never
		served from a stale database.
		"""
		with self._lock:
			buffered = list(self._buffer.items())
			self._buffer.clear()

		for (doctype, name), values in buffered:
			# A regular write (non-negative index) is immutable once stored;
			# only the special channels (`__resume__` and friends, which map to
			# a negative index) may replace an existing row.
			if doctype == CHECKPOINT_WRITE_DOCTYPE and values["idx"] >= 0:
				if frappe.db.exists(doctype, name):
					continue

			self._upsert(doctype, name, values)

	# --- reading ------------------------------------------------------------

	def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
		self.flush()
		thread_id = config["configurable"]["thread_id"]
		checkpoint_ns = config["configurable"].get("checkpoint_ns", "") or ""
		checkpoint_id = get_checkpoint_id(config)

		if checkpoint_id:
			row = frappe.db.get_value(
				CHECKPOINT_DOCTYPE,
				checkpoint_row_name(thread_id, checkpoint_ns, checkpoint_id),
				CHECKPOINT_FIELDS,
				as_dict=True,
			)
		else:
			rows = frappe.db.get_all(
				CHECKPOINT_DOCTYPE,
				filters={"thread_id": thread_id, "checkpoint_ns": checkpoint_ns},
				fields=CHECKPOINT_FIELDS,
				order_by="checkpoint_id desc",
				limit=1,
			)
			row = rows[0] if rows else None

		return self._to_tuple(row) if row else None

	def list(
		self,
		config: RunnableConfig | None,
		*,
		filter: dict[str, Any] | None = None,
		before: RunnableConfig | None = None,
		limit: int | None = None,
	) -> Iterator[CheckpointTuple]:
		self.flush()
		filters: list[list[Any]] = []
		if config:
			filters.append(["thread_id", "=", config["configurable"]["thread_id"]])
			checkpoint_ns = config["configurable"].get("checkpoint_ns")
			if checkpoint_ns is not None:
				filters.append(["checkpoint_ns", "=", checkpoint_ns])
			if checkpoint_id := get_checkpoint_id(config):
				filters.append(["checkpoint_id", "=", checkpoint_id])
		if before and (before_id := get_checkpoint_id(before)):
			filters.append(["checkpoint_id", "<", before_id])

		rows = frappe.db.get_all(
			CHECKPOINT_DOCTYPE,
			filters=filters,
			fields=CHECKPOINT_FIELDS,
			order_by="checkpoint_id desc",
			# ponytail: metadata lives in an opaque blob, so a metadata filter
			# is applied in Python over the whole thread. Add indexed metadata
			# columns if list(filter=...) ever gets hot.
			limit=None if filter else limit,
		)

		yielded = 0
		for row in rows:
			checkpoint_tuple = self._to_tuple(row)
			if filter and any(checkpoint_tuple.metadata.get(key) != value for key, value in filter.items()):
				continue

			yield checkpoint_tuple
			yielded += 1
			if limit is not None and yielded >= limit:
				return

	# --- writing ------------------------------------------------------------

	def put(
		self,
		config: RunnableConfig,
		checkpoint: Checkpoint,
		metadata: CheckpointMetadata,
		new_versions: ChannelVersions,
	) -> RunnableConfig:
		thread_id = config["configurable"]["thread_id"]
		checkpoint_ns = config["configurable"].get("checkpoint_ns", "") or ""
		checkpoint_id = checkpoint["id"]

		# ponytail: `channel_values` is stored inline with the checkpoint
		# instead of in a separate version-keyed blob table. Simpler, and the
		# large channels (messages) change nearly every superstep anyway, so a
		# blob table would deduplicate little. Split it out if thread size hurts.
		checkpoint_type, checkpoint_data = self._encode(checkpoint)
		metadata_type, metadata_data = self._encode(get_checkpoint_metadata(config, metadata))

		self._buffer_row(
			CHECKPOINT_DOCTYPE,
			checkpoint_row_name(thread_id, checkpoint_ns, checkpoint_id),
			{
				"thread_id": thread_id,
				"checkpoint_ns": checkpoint_ns,
				"checkpoint_id": checkpoint_id,
				"parent_checkpoint_id": config["configurable"].get("checkpoint_id"),
				"checkpoint_type": checkpoint_type,
				"checkpoint_data": checkpoint_data,
				"metadata_type": metadata_type,
				"metadata_data": metadata_data,
			},
		)

		return {
			"configurable": {
				"thread_id": thread_id,
				"checkpoint_ns": checkpoint_ns,
				"checkpoint_id": checkpoint_id,
			}
		}

	def put_writes(
		self,
		config: RunnableConfig,
		writes: Sequence[tuple[str, Any]],
		task_id: str,
		task_path: str = "",
	) -> None:
		thread_id = config["configurable"]["thread_id"]
		checkpoint_ns = config["configurable"].get("checkpoint_ns", "") or ""
		checkpoint_id = config["configurable"]["checkpoint_id"]

		for position, (channel, value) in enumerate(writes):
			idx = WRITES_IDX_MAP.get(channel, position)
			value_type, value_data = self._encode(value)
			self._buffer_row(
				CHECKPOINT_WRITE_DOCTYPE,
				checkpoint_write_row_name(thread_id, checkpoint_ns, checkpoint_id, task_id, idx),
				{
					"thread_id": thread_id,
					"checkpoint_ns": checkpoint_ns,
					"checkpoint_id": checkpoint_id,
					"task_id": task_id,
					"task_path": task_path,
					"idx": idx,
					"channel": channel,
					"value_type": value_type,
					"value_data": value_data,
				},
				keep_first=idx >= 0,
			)

	def delete_thread(self, thread_id: str) -> None:
		with self._lock:
			for key in [key for key, values in self._buffer.items() if values["thread_id"] == thread_id]:
				del self._buffer[key]

		frappe.db.delete(CHECKPOINT_DOCTYPE, {"thread_id": thread_id})
		frappe.db.delete(CHECKPOINT_WRITE_DOCTYPE, {"thread_id": thread_id})

	# --- internals ----------------------------------------------------------

	def _buffer_row(
		self, doctype: str, name: str, values: dict[str, Any], *, keep_first: bool = False
	) -> None:
		with self._lock:
			if keep_first:
				self._buffer.setdefault((doctype, name), values)
			else:
				self._buffer[(doctype, name)] = values

	def _encode(self, value: Any) -> tuple[str, str]:
		"""Serialize through the LangGraph serde, base64 for a text column."""
		type_, blob = self.serde.dumps_typed(value)
		return type_, b64encode(blob).decode()

	def _decode(self, type_: str, data: str | None) -> Any:
		return self.serde.loads_typed((type_, b64decode(data or "")))

	def _to_tuple(self, row: dict[str, Any]) -> CheckpointTuple:
		thread_id = row["thread_id"]
		checkpoint_ns = row["checkpoint_ns"] or ""
		checkpoint_id = row["checkpoint_id"]

		return CheckpointTuple(
			config={
				"configurable": {
					"thread_id": thread_id,
					"checkpoint_ns": checkpoint_ns,
					"checkpoint_id": checkpoint_id,
				}
			},
			checkpoint=self._decode(row["checkpoint_type"], row["checkpoint_data"]),
			metadata=self._decode(row["metadata_type"], row["metadata_data"]),
			parent_config=(
				{
					"configurable": {
						"thread_id": thread_id,
						"checkpoint_ns": checkpoint_ns,
						"checkpoint_id": row["parent_checkpoint_id"],
					}
				}
				if row["parent_checkpoint_id"]
				else None
			),
			pending_writes=self._pending_writes(thread_id, checkpoint_ns, checkpoint_id),
		)

	def _pending_writes(self, thread_id: str, checkpoint_ns: str, checkpoint_id: str) -> list[PendingWrite]:
		rows = frappe.db.get_all(
			CHECKPOINT_WRITE_DOCTYPE,
			filters={
				"thread_id": thread_id,
				"checkpoint_ns": checkpoint_ns,
				"checkpoint_id": checkpoint_id,
			},
			fields=("task_id", "channel", "value_type", "value_data"),
			order_by="task_id asc, idx asc",
		)
		return [
			(row["task_id"], row["channel"], self._decode(row["value_type"], row["value_data"]))
			for row in rows
		]

	def _upsert(self, doctype: str, name: str, values: dict[str, Any]) -> None:
		# ponytail: check-then-insert, not an atomic upsert. Two workers writing
		# the same checkpoint row would need INSERT ... ON DUPLICATE KEY; one
		# conversation is handled by one worker, so this is enough.
		if frappe.db.exists(doctype, name):
			frappe.db.set_value(doctype, name, values, update_modified=False)
			return

		doc = frappe.new_doc(doctype)
		doc.update(values)
		doc.insert(ignore_permissions=True)
