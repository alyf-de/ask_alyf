import unittest

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from ask_alyf.ask_alyf.answer_stream import AnswerDraftPublisher, invoke_streaming


class FakeClock:
	def __init__(self):
		self.now = 100.0

	def __call__(self) -> float:
		return self.now


def model_meta(call: str) -> dict:
	return {"langgraph_node": "model", "langgraph_checkpoint_ns": f"model:{call}"}


class TestAnswerDraftPublisher(unittest.TestCase):
	def make_publisher(self):
		self.sent = []
		self.clock = FakeClock()
		return AnswerDraftPublisher("CONV-1", publish=self.sent.append, clock=self.clock)

	def texts(self) -> list[str]:
		return [payload["text"] for payload in self.sent]

	def test_sends_the_whole_draft_so_far(self):
		publisher = self.make_publisher()

		publisher.on_message(AIMessageChunk(content="Hello"), model_meta("a"))
		self.clock.now += 1
		publisher.on_message(AIMessageChunk(content=" world"), model_meta("a"))

		self.assertEqual(self.texts(), ["Hello", "Hello world"])
		self.assertEqual(self.sent[0]["conversation"], "CONV-1")

	def test_throttles_until_a_sentence_ends(self):
		publisher = self.make_publisher()

		publisher.on_message(AIMessageChunk(content="The"), model_meta("a"))
		publisher.on_message(AIMessageChunk(content=" total"), model_meta("a"))
		publisher.on_message(AIMessageChunk(content=" is 5."), model_meta("a"))
		publisher.on_message(AIMessageChunk(content=" Next"), model_meta("a"))
		publisher.flush()

		self.assertEqual(self.texts(), ["The", "The total is 5.", "The total is 5. Next"])

	def test_withdraws_the_text_of_a_call_that_calls_a_tool(self):
		publisher = self.make_publisher()

		publisher.on_message(AIMessageChunk(content="Let me check."), model_meta("a"))
		publisher.on_message(
			AIMessageChunk(
				content="",
				tool_call_chunks=[{"name": "get_list", "args": "{}", "id": "c1", "index": 0}],
			),
			model_meta("a"),
		)
		publisher.on_message(AIMessageChunk(content=" more text"), model_meta("a"))
		publisher.flush()

		self.assertEqual(self.texts(), ["Let me check.", ""])

	def test_a_new_model_call_starts_a_new_draft(self):
		publisher = self.make_publisher()

		publisher.on_message(AIMessageChunk(content="First."), model_meta("a"))
		publisher.on_message(AIMessageChunk(content="Answer."), model_meta("b"))

		self.assertEqual(self.texts(), ["First.", "", "Answer."])

	def test_ignores_tool_results_and_other_nodes(self):
		publisher = self.make_publisher()

		publisher.on_message(ToolMessage(content="rows", tool_call_id="c1"), {"langgraph_node": "tools"})
		publisher.on_message(AIMessageChunk(content="summary."), {"langgraph_node": "summarize"})
		publisher.flush()

		self.assertEqual(self.sent, [])


class FakeGraph:
	def __init__(self, parts):
		self.parts = parts
		self.calls = []

	def stream(self, payload, config=None, stream_mode=None):
		self.calls.append((payload, config, stream_mode))
		yield from self.parts


class TestInvokeStreaming(unittest.TestCase):
	def test_returns_the_last_state_like_invoke(self):
		final = {"messages": [AIMessage(content="Done.")]}
		graph = FakeGraph(
			[
				("values", {"messages": []}),
				("messages", (AIMessageChunk(content="Done."), model_meta("a"))),
				("values", final),
			]
		)
		sent = []

		result = invoke_streaming(
			graph, {"messages": []}, {"configurable": {}}, AnswerDraftPublisher("C", publish=sent.append)
		)

		self.assertIs(result, final)
		self.assertEqual([payload["text"] for payload in sent], ["Done."])
		self.assertEqual(graph.calls[0][2], ["messages", "updates", "values"])

	def test_adds_interrupts_to_the_last_state_like_invoke(self):
		interrupt = object()
		graph = FakeGraph(
			[
				("updates", {"__interrupt__": (interrupt,)}),
				("values", {"messages": ["m"]}),
			]
		)

		result = invoke_streaming(graph, {}, {}, AnswerDraftPublisher("C", publish=lambda _payload: None))

		self.assertEqual(result, {"messages": ["m"], "__interrupt__": [interrupt]})
