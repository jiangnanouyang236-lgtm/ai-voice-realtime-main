import json
from collections import OrderedDict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from llm.llm_grpc_server import (
    ImprovedLLMServiceServicer,
    ToolLatencyRoute,
    _unavailable_tool_category_response,
)
from llm.workflow_protocol import WorkflowStep
from llm.workflow_runtime_service import WorkflowRuntimeService


class _SessionManager:
    def __init__(self):
        self.messages = []

    def get_messages_for_llm(self, session_id):
        return list(self.messages)

    def add_message(self, session_id, role, content):
        self.messages.append({"role": role, "content": content})


class _StreamingClient:
    async def stream_call_llm(self, messages, tools, temperature, max_tokens, model_name, **kwargs):
        yield {"type": "text", "content": "青岛有雨，"}
        yield {"type": "text", "content": "建议带伞。"}


class _MCPManager:
    def __init__(self):
        self.calls = []

    def resolve_tool_name(self, name):
        return name.replace("__", ".")

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        return {"isError": False, "content": [{"text": "发布成功"}]}


def _runtime():
    return SimpleNamespace(
        bot_id="xiaowen",
        robot_id="companion_01",
        session_id="session-1",
        user_text="查青岛天气，然后拨打视频",
        results={"step_1": {"status": "success", "result": {"weather": "rain"}}},
    )


def _bot():
    return SimpleNamespace(
        model="test-model",
        temperature=0.2,
        max_tokens=100,
        system_prompt="你是陪伴机器人。",
    )


class LLMWorkflowAdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_tool_bot_still_classifies_realtime_request(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.session_manager = _SessionManager()
        servicer._workflow_route_cache = OrderedDict()
        servicer.llm_client = object()
        servicer.router_llm_client = object()
        servicer.router_model_name = "router-model"
        bot = SimpleNamespace(bot_id="bot-without-mcp", name="无工具", model="main-model")
        request = SimpleNamespace(
            bot_id=bot.bot_id,
            robot_id="test_01",
            session_id="session-1",
            text="青岛今天天气怎么样",
        )

        async def prepare(bot_id):
            return bot, _MCPManager(), [], {}, set()

        async def classify(*args, **kwargs):
            self.assertTrue(kwargs["allow_unavailable_tool_category"])
            return ToolLatencyRoute("tool", category="websearch", source="llm_router_async")

        servicer._workflow_prepare_tools = prepare
        with patch(
            "llm.llm_grpc_server._classify_legacy_tool_latency_route_async",
            side_effect=classify,
        ):
            classified = await servicer._workflow_classify(request)

        self.assertEqual("websearch", classified.category)
        self.assertIn("联网查询能力", _unavailable_tool_category_response(classified.category))

    async def test_builds_separate_runtime_service(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)

        service = servicer.build_workflow_service()

        self.assertIsInstance(service, WorkflowRuntimeService)

    async def test_non_complex_preclassification_is_cached_once_for_stream_chat(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.session_manager = _SessionManager()
        servicer._workflow_route_cache = OrderedDict()
        servicer.llm_client = object()
        servicer.router_llm_client = object()
        servicer.router_model_name = "router-model"
        bot = SimpleNamespace(bot_id="xiaowen", name="温妮", model="test-model")
        request = SimpleNamespace(
            bot_id="xiaowen",
            robot_id="companion_01",
            session_id="session-1",
            text="青岛天气怎么样",
        )

        async def prepare(bot_id):
            return bot, _MCPManager(), [{"function": {"name": "websearch__search"}}], {}, set()

        servicer._workflow_prepare_tools = prepare
        async def classify(*args, **kwargs):
            return ToolLatencyRoute("tool", category="websearch", source="llm_router_async")

        with patch(
            "llm.llm_grpc_server._classify_legacy_tool_latency_route_async",
            side_effect=classify,
        ):
            classified = await servicer._workflow_classify(request)

        cached = servicer._pop_workflow_route(
            session_id="session-1",
            text="青岛天气怎么样",
            bot_id="xiaowen",
            robot_id="companion_01",
        )
        self.assertEqual("websearch", classified.category)
        self.assertEqual("websearch", cached.category)
        self.assertIsNone(
            servicer._pop_workflow_route(
                session_id="session-1",
                text="青岛天气怎么样",
                bot_id="xiaowen",
                robot_id="companion_01",
            )
        )

    async def test_complex_preclassification_uses_router_for_non_xiaowen_bot(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.session_manager = _SessionManager()
        servicer._workflow_route_cache = OrderedDict()
        servicer.llm_client = object()
        servicer.router_llm_client = object()
        servicer.router_model_name = "router-model"
        bot = SimpleNamespace(bot_id="wzk-yudazui", name="余大嘴", model="main-model")
        request = SimpleNamespace(
            bot_id=bot.bot_id,
            robot_id="companion_01",
            session_id="session-1",
            text="先查青岛天气，再查北京天气，最后比较两地温度",
        )

        async def prepare(bot_id):
            return bot, _MCPManager(), [{"function": {"name": "websearch__search"}}], {}, set()

        async def classify(*args, **kwargs):
            return ToolLatencyRoute("tool", category="complex", source="llm_router_async")

        servicer._workflow_prepare_tools = prepare
        with (
            patch(
                "llm.llm_grpc_server._build_tool_latency_route",
                return_value=ToolLatencyRoute("legacy", source="legacy"),
            ),
            patch(
                "llm.llm_grpc_server._classify_legacy_tool_latency_route_async",
                side_effect=classify,
            ) as classifier,
        ):
            classified = await servicer._workflow_classify(request)

        self.assertEqual("complex", classified.category)
        self.assertEqual("llm_router_async", classified.source)
        classifier.assert_awaited_once()
        self.assertIsNone(
            servicer._pop_workflow_route(
                session_id=request.session_id,
                text=request.text,
                bot_id=request.bot_id,
                robot_id=request.robot_id,
            )
        )

    async def test_planner_uses_main_model_with_zero_temperature(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.session_manager = _SessionManager()
        safe_tool = {
            "type": "function",
            "function": {
                "name": "websearch__search",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
        observed = {}

        async def prepare(bot_id):
            return _bot(), _MCPManager(), [safe_tool], {}, set()

        async def collect(messages, **kwargs):
            observed.update(kwargs)
            return json.dumps(
                {
                    "version": 1,
                    "goal": "查询天气",
                    "steps": [
                        {
                            "id": "step_1",
                            "type": "tool",
                            "intent": "查询",
                            "tool_name": "websearch__search",
                            "arguments": {"query": "青岛天气"},
                            "depends_on": [],
                            "failure_policy": "partial_response",
                            "terminal": False,
                        },
                        {
                            "id": "step_2",
                            "type": "respond",
                            "intent": "回答",
                            "depends_on": ["step_1"],
                            "failure_policy": "partial_response",
                            "terminal": False,
                            "vision": "none",
                        },
                    ],
                },
                ensure_ascii=False,
            )

        servicer._workflow_prepare_tools = prepare
        servicer._workflow_collect_model_text = collect
        request = SimpleNamespace(
            bot_id="xiaowen",
            session_id="session-1",
            text="帮我查青岛天气",
        )

        planned = await servicer._workflow_plan(request)

        self.assertEqual(0.0, observed["temperature"])
        self.assertEqual("test-model", observed["model_name"])
        self.assertEqual("step_1", planned.plan.steps[0].id)

    async def test_tool_step_injects_server_robot_id(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        manager = _MCPManager()
        raw_tool = {
            "type": "function",
            "function": {
                "name": "robot_remote__call_video",
                "parameters": {
                    "type": "object",
                    "properties": {"robot_id": {"type": "string"}},
                },
            },
        }

        async def prepare(bot_id):
            return _bot(), manager, [], {"robot_remote__call_video": raw_tool}, set()

        servicer._workflow_prepare_tools = prepare
        step = WorkflowStep(
            id="step_1",
            type="tool",
            intent="拨打视频",
            depends_on=(),
            failure_policy="abort",
            terminal=True,
            tool_name="robot_remote__call_video",
            arguments={},
        )

        outcome = await servicer._workflow_execute_step(_runtime(), step)

        self.assertTrue(outcome.success)
        self.assertEqual("companion_01", manager.calls[0][1]["robot_id"])
        self.assertEqual("发布成功", outcome.result["text"])

    async def test_synthesis_keeps_incremental_chunks_and_persists_clean_history(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.llm_client = _StreamingClient()
        servicer.session_manager = _SessionManager()
        servicer.vision_snapshot_client = None
        servicer.runtime_state = SimpleNamespace(
            bot_manager=SimpleNamespace(get_bot_or_default=lambda bot_id: _bot())
        )
        step = WorkflowStep(
            id="step_2",
            type="respond",
            intent="汇总",
            depends_on=("step_1",),
            failure_policy="partial_response",
            terminal=False,
            vision="none",
        )

        chunks = [text async for text in servicer._workflow_synthesize(_runtime(), step)]

        self.assertEqual(["青岛有雨，", "建议带伞。"], chunks)
        self.assertEqual(
            [
                {"role": "user", "content": "查青岛天气，然后拨打视频"},
                {"role": "assistant", "content": "青岛有雨，建议带伞。"},
            ],
            servicer.session_manager.messages,
        )


if __name__ == "__main__":
    unittest.main()
