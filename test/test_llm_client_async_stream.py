import asyncio
import time
import unittest

from llm.llm_client import ImprovedQwenLLMClient
from llm.llm_grpc_server import ImprovedLLMServiceServicer
from llm import llm_service_pb2


class LLMClientAsyncStreamTest(unittest.TestCase):
    def test_sync_llm_stream_runs_without_blocking_event_loop(self):
        client = _SlowBlockingLLMClient()

        async def collect_stream(label):
            return [
                chunk
                async for chunk in client.stream_call_llm(
                    messages=[{"role": "user", "content": label}],
                    tools=None,
                    model_name=label,
                )
            ]

        async def short_timer():
            started_at = time.monotonic()
            await asyncio.sleep(0.02)
            return time.monotonic() - started_at

        async def run_test():
            return await asyncio.gather(
                collect_stream("first"),
                collect_stream("second"),
                short_timer(),
            )

        started_at = time.monotonic()
        first_chunks, second_chunks, timer_elapsed = asyncio.run(run_test())
        total_elapsed = time.monotonic() - started_at

        self.assertEqual([{"type": "text", "content": "first"}], first_chunks)
        self.assertEqual([{"type": "text", "content": "second"}], second_chunks)
        self.assertLess(timer_elapsed, 0.1)
        self.assertLess(total_elapsed, 0.35)

    def test_concurrent_stream_chat_requests_do_not_serialize_sync_provider_streams(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.llm_client = _SlowBlockingLLMClient()
        servicer.session_manager = _FakeSessionManager()
        servicer.agent_registry = _FakeAgentRegistry()
        servicer.agent_tool_invoker = None
        servicer.runtime_state = _FakeRuntimeState()

        async def collect_stream(session_id):
            request = llm_service_pb2.ChatRequest(text="讲个短句", session_id=session_id)
            return [
                response.text
                async for response in servicer.StreamChat(request, _FakeContext())
            ]

        async def run_test():
            return await asyncio.gather(
                collect_stream("session-a"),
                collect_stream("session-b"),
            )

        started_at = time.monotonic()
        first_responses, second_responses = asyncio.run(run_test())
        total_elapsed = time.monotonic() - started_at

        self.assertEqual(["test-model", ""], first_responses)
        self.assertEqual(["test-model", ""], second_responses)
        self.assertLess(total_elapsed, 0.35)

    def test_stream_call_llm_closes_provider_response_when_closed_early(self):
        client = _ClosableBlockingLLMClient()

        async def run_test():
            stream = client.stream_call_llm(
                messages=[{"role": "user", "content": "hello"}],
                tools=None,
                model_name="close-test",
            )
            first_chunk = await stream.__anext__()
            await stream.aclose()
            await asyncio.sleep(0.05)
            return first_chunk

        first_chunk = asyncio.run(run_test())

        self.assertEqual({"type": "text", "content": "first"}, first_chunk)
        self.assertTrue(client.response.closed)
        self.assertTrue(client.stop_seen)


class _SlowBlockingLLMClient(ImprovedQwenLLMClient):
    def __init__(self):
        self.model_name = "test-model"

    def _call_llm(
        self,
        messages,
        tools=None,
        temperature=0.7,
        max_tokens=2000,
        model_name=None,
        tool_choice=None,
    ):
        time.sleep(0.15)
        yield {"type": "text", "content": model_name or self.model_name}


class _FakeClosableResponse:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _ClosableBlockingLLMClient(ImprovedQwenLLMClient):
    def __init__(self):
        self.model_name = "test-model"
        self.response = _FakeClosableResponse()
        self.stop_seen = False

    def _call_llm(
        self,
        messages,
        tools=None,
        temperature=0.7,
        max_tokens=2000,
        model_name=None,
        tool_choice=None,
        stop_event=None,
        on_response=None,
    ):
        if on_response is not None:
            on_response(self.response)
        yield {"type": "text", "content": "first"}
        while stop_event is not None and not stop_event.is_set():
            time.sleep(0.01)
        self.stop_seen = stop_event is not None and stop_event.is_set()


class _FakeBot:
    bot_id = "test-bot"
    name = "test bot"
    model = "test-model"
    temperature = 0.7
    max_tokens = 2000
    max_response_chars = 0
    agents = []
    mcp_servers = []
    system_prompt = ""


class _FakeBotManager:
    def get_bot_or_default(self, bot_id):
        return _FakeBot()


class _FakeRuntimeState:
    def __init__(self):
        self.bot_manager = _FakeBotManager()
        self.mcp_manager = None


class _FakeAgentRegistry:
    def get(self, agent_id):
        return None

    def find_entry_agent(self, text, context, allowed_agent_ids=None):
        return None


class _FakeSessionManager:
    def __init__(self):
        self.messages = []

    def get_agent_state(self, session_id):
        return None

    def add_message(self, session_id, role, content):
        self.messages.append((session_id, role, content))

    def get_messages_for_llm(self, session_id):
        return [
            {"role": role, "content": content}
            for stored_session_id, role, content in self.messages
            if stored_session_id == session_id
        ]

    def add_tool_call(self, session_id, tool_name, tool_args, result):
        pass


class _FakeContext:
    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


if __name__ == "__main__":
    unittest.main()
