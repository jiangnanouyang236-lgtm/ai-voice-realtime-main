import asyncio
import unittest

from llm.llm_client import ImprovedQwenLLMClient
from voice_quick_replies import quick_reply_pool


class _ToolCallThenFinalClient(ImprovedQwenLLMClient):
    def __init__(self):
        self.model_name = "qwen3-5-9b"
        self.call_count = 0
        self.tool_counts = []
        self.tool_choices = []
        self.temperatures = []

    def _call_llm(
        self,
        messages,
        tools=None,
        temperature=0.7,
        max_tokens=2000,
        model_name=None,
        tool_choice=None,
    ):
        self.call_count += 1
        self.tool_counts.append(len(tools or []))
        self.tool_choices.append(tool_choice)
        self.temperatures.append(temperature)
        if self.call_count == 1:
            yield {
                "type": "tool_call",
                "content": {
                    "name": "websearch__search",
                    "arguments": {"query": "青岛天气"},
                },
            }
        else:
            yield {"type": "text", "content": "查到了。"}


class _NoCallThenToolClient(_ToolCallThenFinalClient):
    def _call_llm(
        self,
        messages,
        tools=None,
        temperature=0.7,
        max_tokens=2000,
        model_name=None,
        tool_choice=None,
    ):
        self.call_count += 1
        self.tool_counts.append(len(tools or []))
        self.tool_choices.append(tool_choice)
        self.temperatures.append(temperature)
        if self.call_count == 1:
            yield {"type": "text", "content": "我直接回答。"}
        elif self.call_count == 2:
            yield {
                "type": "tool_call",
                "content": {
                    "name": "websearch__search",
                    "arguments": {"query": "青岛天气"},
                },
            }
        else:
            yield {"type": "text", "content": "重试后查到了。"}


class _AlwaysNoCallClient(_ToolCallThenFinalClient):
    def _call_llm(
        self,
        messages,
        tools=None,
        temperature=0.7,
        max_tokens=2000,
        model_name=None,
        tool_choice=None,
    ):
        self.call_count += 1
        self.tool_counts.append(len(tools or []))
        self.tool_choices.append(tool_choice)
        self.temperatures.append(temperature)
        yield {"type": "text", "content": "我还是直接回答。"}


class _UtilsThenFinalClient(_ToolCallThenFinalClient):
    def _call_llm(
        self,
        messages,
        tools=None,
        temperature=0.7,
        max_tokens=2000,
        model_name=None,
        tool_choice=None,
    ):
        self.call_count += 1
        self.tool_counts.append(len(tools or []))
        self.tool_choices.append(tool_choice)
        self.temperatures.append(temperature)
        if self.call_count == 1:
            yield {
                "type": "tool_call",
                "content": {
                    "name": "utils_remote__get_now_context",
                    "arguments": {},
                },
            }
        else:
            yield {"type": "text", "content": "继续完成后续步骤。"}


async def _fake_utils_executor(tool_name, tool_args):
    return "现在是 2026年08月09日 17:30:00，周日。"


async def _fake_tool_executor(tool_name, tool_args):
    return "青岛今天晴。"


def _collect_chat_with_tools(client, **kwargs):
    async def _run():
        return [
            chunk
            async for chunk in client.chat_with_tools(
                messages=[{"role": "user", "content": "今天青岛天气怎么样？"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "websearch__search",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                            },
                        },
                    },
                }],
                tool_executor=_fake_tool_executor,
                max_tool_rounds=2,
                **kwargs,
            )
        ]

    return asyncio.run(_run())


def _collect_utils_chat(client, **kwargs):
    async def _run():
        return [
            chunk
            async for chunk in client.chat_with_tools(
                messages=[{"role": "user", "content": "现在几点？"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "utils_remote__get_now_context",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }],
                tool_executor=_fake_utils_executor,
                max_tool_rounds=2,
                emit_tool_wait_message=False,
                **kwargs,
            )
        ]

    return asyncio.run(_run())


class LLMClientToolWaitMessageTest(unittest.TestCase):
    def test_single_utils_route_returns_authoritative_result_directly(self):
        client = _UtilsThenFinalClient()

        chunks = _collect_utils_chat(client, router_category="utils")

        self.assertEqual(["tool_result", "text"], [chunk["type"] for chunk in chunks])
        self.assertEqual(
            "现在是 2026年08月09日 17:30:00，周日。",
            chunks[-1]["content"],
        )
        self.assertEqual(1, client.call_count)

    def test_complex_route_does_not_stop_after_first_utils_result(self):
        client = _UtilsThenFinalClient()

        chunks = _collect_utils_chat(client, router_category="complex")

        self.assertEqual(["tool_result", "text"], [chunk["type"] for chunk in chunks])
        self.assertEqual(2, client.call_count)

    def test_chat_with_tools_emits_wait_message_by_default(self):
        chunks = _collect_chat_with_tools(_ToolCallThenFinalClient())

        self.assertEqual("progress_text", chunks[0]["type"])
        self.assertIn(chunks[0]["content"], quick_reply_pool("tool.websearch"))

    def test_chat_with_tools_can_suppress_internal_wait_message(self):
        chunks = _collect_chat_with_tools(
            _ToolCallThenFinalClient(),
            emit_tool_wait_message=False,
        )

        self.assertNotEqual("progress_text", chunks[0]["type"])
        self.assertEqual("tool_result", chunks[0]["type"])

    def test_chat_with_tools_logs_llm_and_tool_elapsed_metrics(self):
        with self.assertLogs("llm.llm_client", level="INFO") as logs:
            _collect_chat_with_tools(
                _ToolCallThenFinalClient(),
                router_kind="tool",
                router_source="deterministic",
                router_category="websearch",
            )

        output = "\n".join(logs.output)
        self.assertIn("router_kind=tool", output)
        self.assertIn("router_source=deterministic", output)
        self.assertIn("router_category=websearch", output)
        self.assertIn("LLM 决策完成", output)
        self.assertIn("elapsed=", output)
        self.assertIn("工具执行完成", output)

    def test_chat_with_tools_uses_empty_followup_tools_for_final_answer(self):
        client = _ToolCallThenFinalClient()
        chunks = _collect_chat_with_tools(
            client,
            followup_tools=[],
            emit_tool_wait_message=False,
        )

        self.assertEqual(["tool_result", "text"], [chunk["type"] for chunk in chunks])
        self.assertEqual([1, 0], client.tool_counts)

    def test_websearch_uses_auto_tool_choice_for_provider_compatibility(self):
        client = _ToolCallThenFinalClient()

        _collect_chat_with_tools(
            client,
            router_category="websearch",
            require_tool_call=True,
            emit_tool_wait_message=False,
        )

        self.assertEqual([None, None], client.tool_choices)

    def test_websearch_retries_model_once_when_required_call_is_missing(self):
        client = _NoCallThenToolClient()

        chunks = _collect_chat_with_tools(
            client,
            router_category="websearch",
            require_tool_call=True,
            emit_tool_wait_message=False,
        )

        self.assertEqual(["tool_result", "text"], [chunk["type"] for chunk in chunks])
        self.assertEqual([None, None, None], client.tool_choices)
        self.assertEqual([0.7, 0.0, 0.7], client.temperatures)

    def test_websearch_falls_back_to_unique_search_tool_after_model_retry(self):
        client = _AlwaysNoCallClient()

        chunks = _collect_chat_with_tools(
            client,
            router_category="websearch",
            require_tool_call=True,
            emit_tool_wait_message=False,
        )

        self.assertEqual(3, client.call_count)
        self.assertEqual([None, None, None], client.tool_choices)
        self.assertEqual(["tool_result", "text"], [chunk["type"] for chunk in chunks])


if __name__ == "__main__":
    unittest.main()
