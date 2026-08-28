# Copyright (c) 2026, ALYF GmbH and Contributors
# See license.txt

from __future__ import annotations

import operator
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, TypedDict

import frappe
from frappe.tests import IntegrationTestCase
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ask_alyf.ask_alyf.checkpointer import (
	CHECKPOINT_DOCTYPE,
	CHECKPOINT_WRITE_DOCTYPE,
	FrappeCheckpointSaver,
)

THREAD = "test-checkpointer-thread"


def _config(checkpoint_id: str | None = None, thread: str = THREAD) -> dict:
	configurable = {"thread_id": thread, "checkpoint_ns": ""}
	if checkpoint_id:
		configurable["checkpoint_id"] = checkpoint_id
	return {"configurable": configurable}


class _State(TypedDict):
	steps: Annotated[list[str], operator.add]


class IntegrationTestFrappeCheckpointSaver(IntegrationTestCase):
	def setUp(self):
		self.saver = FrappeCheckpointSaver()
		self.addCleanup(self._clear)
		self._clear()

	def _clear(self):
		for doctype in (CHECKPOINT_DOCTYPE, CHECKPOINT_WRITE_DOCTYPE):
			frappe.db.delete(doctype, {"thread_id": ("like", "test-checkpointer-%")})

	def _put(self, checkpoint_id: str, parent_id: str | None = None, step: int = 0, thread: str = THREAD):
		checkpoint = empty_checkpoint()
		checkpoint["id"] = checkpoint_id
		checkpoint["channel_values"] = {"steps": [f"value-{checkpoint_id}"]}
		return self.saver.put(
			_config(parent_id, thread=thread), checkpoint, {"source": "loop", "step": step}, {}
		)

	def test_put_then_get_tuple_roundtrips_checkpoint_and_metadata(self):
		self._put("00000000-0000-6000-8000-000000000001", step=1)

		stored = self.saver.get_tuple(_config())
		self.assertIsNotNone(stored)
		self.assertEqual(stored.checkpoint["id"], "00000000-0000-6000-8000-000000000001")
		self.assertEqual(
			stored.checkpoint["channel_values"]["steps"], ["value-00000000-0000-6000-8000-000000000001"]
		)
		self.assertEqual(stored.metadata["step"], 1)
		self.assertIsNone(stored.parent_config)

	def test_get_tuple_without_id_returns_latest_and_links_parent(self):
		first = "00000000-0000-6000-8000-000000000001"
		second = "00000000-0000-6000-8000-000000000002"
		self._put(first)
		self._put(second, parent_id=first)

		latest = self.saver.get_tuple(_config())
		self.assertEqual(latest.checkpoint["id"], second)
		self.assertEqual(latest.parent_config["configurable"]["checkpoint_id"], first)

		# An explicit ID still reaches the older checkpoint.
		older = self.saver.get_tuple(_config(first))
		self.assertEqual(older.checkpoint["id"], first)

	def test_put_is_idempotent_per_checkpoint_id(self):
		checkpoint_id = "00000000-0000-6000-8000-000000000001"
		self._put(checkpoint_id, step=1)
		self._put(checkpoint_id, step=2)
		self.saver.flush()

		self.assertEqual(frappe.db.count(CHECKPOINT_DOCTYPE, {"thread_id": THREAD}), 1)
		self.assertEqual(self.saver.get_tuple(_config()).metadata["step"], 2)

	def test_put_writes_keeps_first_regular_write_and_overwrites_special_writes(self):
		checkpoint_id = "00000000-0000-6000-8000-000000000001"
		self._put(checkpoint_id)
		config = _config(checkpoint_id)

		self.saver.put_writes(config, [("steps", "first")], "task-1")
		self.saver.flush()
		# Once from the buffer, once against the stored row.
		self.saver.put_writes(config, [("steps", "ignored")], "task-1")
		self.saver.put_writes(config, [("steps", "ignored")], "task-1")
		# `__resume__` is a special channel: it maps to a negative index and
		# must replace the stored value instead of being skipped.
		self.saver.put_writes(config, [("__resume__", "one")], "task-1")
		self.saver.put_writes(config, [("__resume__", "two")], "task-1")

		pending = self.saver.get_tuple(config).pending_writes
		self.assertEqual(
			sorted((channel, value) for _, channel, value in pending),
			[("__resume__", "two"), ("steps", "first")],
		)

	def test_writes_of_other_tasks_and_threads_are_not_mixed_in(self):
		checkpoint_id = "00000000-0000-6000-8000-000000000001"
		self._put(checkpoint_id)
		config = _config(checkpoint_id)
		self.saver.put_writes(config, [("steps", "mine")], "task-1")
		self.saver.put_writes(config, [("steps", "theirs")], "task-2")

		# Same checkpoint ID, different thread: must stay separate.
		other_thread = "test-checkpointer-other"
		self._put(checkpoint_id, thread=other_thread)
		self.saver.put_writes(_config(checkpoint_id, thread=other_thread), [("steps", "elsewhere")], "task-1")

		pending = self.saver.get_tuple(config).pending_writes
		self.assertEqual(sorted(value for _, _, value in pending), ["mine", "theirs"])

	def test_list_orders_newest_first_and_honours_before_limit_and_filter(self):
		ids = [f"00000000-0000-6000-8000-00000000000{n}" for n in (1, 2, 3)]
		parent = None
		for step, checkpoint_id in enumerate(ids):
			self._put(checkpoint_id, parent_id=parent, step=step)
			parent = checkpoint_id

		listed = [tuple_.checkpoint["id"] for tuple_ in self.saver.list(_config())]
		self.assertEqual(listed, list(reversed(ids)))

		before = [t.checkpoint["id"] for t in self.saver.list(_config(), before=_config(ids[2]))]
		self.assertEqual(before, [ids[1], ids[0]])

		limited = [t.checkpoint["id"] for t in self.saver.list(_config(), limit=1)]
		self.assertEqual(limited, [ids[2]])

		filtered = [t.checkpoint["id"] for t in self.saver.list(_config(), filter={"step": 1})]
		self.assertEqual(filtered, [ids[1]])

	def test_delete_thread_removes_checkpoints_and_writes(self):
		checkpoint_id = "00000000-0000-6000-8000-000000000001"
		self._put(checkpoint_id)
		self.saver.put_writes(_config(checkpoint_id), [("steps", "first")], "task-1")

		self.saver.flush()
		self.saver.delete_thread(THREAD)

		self.assertIsNone(self.saver.get_tuple(_config()))
		self.assertEqual(frappe.db.count(CHECKPOINT_WRITE_DOCTYPE, {"thread_id": THREAD}), 0)

	def test_graph_resumes_state_from_the_database_across_invocations(self):
		def append(state: _State) -> _State:
			return {"steps": [f"step-{len(state['steps']) + 1}"]}

		builder = StateGraph(_State)
		builder.add_node("append", append)
		builder.add_edge(START, "append")
		builder.add_edge("append", END)
		graph = builder.compile(checkpointer=self.saver)

		config = {"configurable": {"thread_id": THREAD}}
		graph.invoke({"steps": []}, config)
		self.saver.flush()

		# A fresh saver proves the state came back from the database, not memory.
		second_saver = FrappeCheckpointSaver()
		resumed = builder.compile(checkpointer=second_saver).invoke({"steps": []}, config)
		second_saver.flush()

		self.assertEqual(resumed["steps"], ["step-1", "step-2"])
		self.assertEqual(self.saver.get_tuple(config).checkpoint["channel_values"]["steps"], resumed["steps"])

	def test_writes_from_a_background_thread_are_persisted_by_the_flushing_thread(self):
		# LangGraph calls put/put_writes on its background executor, where the
		# Frappe connection of this thread must not be touched.
		checkpoint_id = "00000000-0000-6000-8000-000000000001"
		with ThreadPoolExecutor(max_workers=1) as executor:
			executor.submit(self._put, checkpoint_id).result()
			executor.submit(
				self.saver.put_writes, _config(checkpoint_id), [("steps", "from-thread")], "task-1"
			).result()
			# Nothing has touched the database yet.
			self.assertEqual(frappe.db.count(CHECKPOINT_DOCTYPE, {"thread_id": THREAD}), 0)

		stored = self.saver.get_tuple(_config())
		self.assertEqual(stored.checkpoint["id"], checkpoint_id)
		self.assertEqual([value for _, _, value in stored.pending_writes], ["from-thread"])

	def test_interrupted_run_resumes_from_the_database_in_a_later_session(self):
		def ask(state: _State) -> _State:
			answer = interrupt("confirm?")
			return {"steps": [f"answered-{answer}"]}

		builder = StateGraph(_State)
		builder.add_node("ask", ask)
		builder.add_edge(START, "ask")
		builder.add_edge("ask", END)

		config = {"configurable": {"thread_id": THREAD}}
		result = builder.compile(checkpointer=self.saver).invoke({"steps": ["before"]}, config)
		self.saver.flush()
		self.assertTrue(result["__interrupt__"])

		# Fresh saver: the pending interrupt is read back from the database.
		resuming_saver = FrappeCheckpointSaver()
		resumed = builder.compile(checkpointer=resuming_saver).invoke(Command(resume="yes"), config)
		resuming_saver.flush()

		self.assertEqual(resumed["steps"], ["before", "answered-yes"])
