"""Stream the answer to the chat while the model is still writing it.

The agent runs as a background job and stores its answer once the run is
over. Until then, `AnswerDraftPublisher` sends the text of the model call that
is being written as `ask_alyf_response_draft` events, so the chat (or any
other client listening for the event, such as a voice front end) can show or
read it sentence by sentence.

Only the main agent's own model calls count. A call that turns out to call a
tool is not the answer, so its text is withdrawn as soon as the first tool
call chunk shows up. Subagents run inside a tool and are not streamed.
"""

import time
from collections.abc import Callable
from typing import Any

import frappe
from langchain_core.messages import AIMessageChunk

DRAFT_EVENT = "ask_alyf_response_draft"
# The node of `create_agent` that calls the model.
MODEL_NODE = "model"
# Send at most this often, but always right after a sentence ends, so a voice
# client can speak each sentence as soon as it is complete.
MIN_PUBLISH_INTERVAL_SECONDS = 0.25
SENTENCE_ENDINGS = (".", "!", "?", ":", "\n")
INTERRUPT_KEY = "__interrupt__"


class AnswerDraftPublisher:
	"""Collect the text of the current model call and publish it, throttled.

	Every event carries the whole draft so far, not a delta: a client that
	missed an event is still right with the next one. An empty `text` means
	the previous draft is withdrawn.
	"""

	def __init__(
		self,
		conversation: str,
		*,
		publish: Callable[[dict[str, Any]], None] | None = None,
		clock: Callable[[], float] = time.monotonic,
	):
		self.conversation = conversation
		self.publish = publish or self.publish_realtime
		self.clock = clock
		self.call_id = None
		self.text = ""
		self.discarded = False
		self.published_text = ""
		self.published_at = 0.0

	def publish_realtime(self, payload: dict[str, Any]) -> None:
		frappe.publish_realtime(DRAFT_EVENT, payload, user=frappe.session.user)

	def on_message(self, chunk: Any, metadata: dict[str, Any]) -> None:
		"""Handle one item of LangGraph's `messages` stream."""
		if not isinstance(chunk, AIMessageChunk) or metadata.get("langgraph_node") != MODEL_NODE:
			return

		call_id = metadata.get("langgraph_checkpoint_ns") or ""
		if call_id != self.call_id:
			# A new model call: whatever the last one wrote was not the answer,
			# or the answer would have ended the run.
			self.call_id = call_id
			self.text = ""
			self.discarded = False
			self.withdraw()

		if self.discarded:
			return

		if chunk.tool_call_chunks:
			self.discarded = True
			self.text = ""
			self.withdraw()
			return

		text = chunk.text
		if not text:
			return

		self.text += text
		now = self.clock()
		if now - self.published_at >= MIN_PUBLISH_INTERVAL_SECONDS or self.text.rstrip(" ").endswith(
			SENTENCE_ENDINGS
		):
			self.send(self.text, now)

	def flush(self) -> None:
		"""Send the part of the draft that the throttle held back."""
		if self.text and self.text != self.published_text:
			self.send(self.text, self.clock())

	def withdraw(self) -> None:
		if self.published_text:
			self.send("", self.clock())

	def send(self, text: str, now: float) -> None:
		self.publish({"conversation": self.conversation, "text": text})
		self.published_text = text
		self.published_at = now


def invoke_streaming(
	graph: Any, payload: Any, config: dict[str, Any], publisher: AnswerDraftPublisher
) -> Any:
	"""Run the graph like `graph.invoke`, feeding the model's text to `publisher`.

	`invoke` itself streams `updates` and `values` and adds the interrupts it
	saw to the last state. This does the same, with the `messages` stream on
	top, so callers get exactly what `invoke` would have returned.
	"""
	latest: Any = None
	interrupts: list[Any] = []
	for mode, data in graph.stream(payload, config=config, stream_mode=["messages", "updates", "values"]):
		if mode == "messages":
			chunk, metadata = data
			publisher.on_message(chunk, metadata or {})
		elif mode == "updates":
			if isinstance(data, dict) and data.get(INTERRUPT_KEY) is not None:
				interrupts.extend(data[INTERRUPT_KEY])
		elif mode == "values":
			latest = data

	publisher.flush()
	if interrupts:
		return (
			{**latest, INTERRUPT_KEY: interrupts} if isinstance(latest, dict) else {INTERRUPT_KEY: interrupts}
		)
	return latest
