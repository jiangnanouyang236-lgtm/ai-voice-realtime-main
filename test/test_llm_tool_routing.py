import json
import os
import threading
import time
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch

from llm import llm_service_pb2
from llm.agent_runtime import AgentResult, AgentStreamEvent
from llm.llm_grpc_server import (
    ImprovedLLMServiceServicer,
    ToolLatencyRoute,
    _build_tool_router_classifier_messages,
    _build_tool_latency_route,
    _classify_legacy_tool_latency_route,
    _is_tool_router_classifier_enabled,
    _is_tool_latency_experiment_enabled,
    _limit_websearch_args,
    _tool_router_classifier_max_tokens,
    _WEBSEARCH_TOOL_TIMEOUT_SEC,
)
from llm.llm_client import _authoritative_tool_result_response
from llm.vision_context import VisionFetchResult, VisionSnapshot
from voice_quick_replies import quick_reply_pool, robot_action_phrase_pool


def _websearch_tool(name="websearch__search"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }


def _tool_with_name(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }


def _robot_remote_tool(name="robot_remote__move_robot"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "robot_id": {"type": "string"},
                },
                "required": [],
            },
        },
    }


def _robot_video_call_tool(name="robot_remote__call_video"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {
                    "robot_id": {"type": "string"},
                },
                "required": [],
            },
        },
    }


def _task_tool(name="robots_task_service__create_alarm"):
    return _tool_with_name(name)


def _utils_tool(name="utils_remote__get_current_time"):
    return _tool_with_name(name)


def _singing_tool(name="singing_remote__play_song"):
    return _tool_with_name(name)


def _tool_names(tools):
    return [tool["function"]["name"] for tool in tools]


def test_authoritative_utils_result_is_returned_without_llm_rewrite():
    result = "现在是 2026年08月09日 16:37:52，周日。今天是法定节假日。"

    assert (
        _authoritative_tool_result_response("utils_remote__get_now_context", result)
        == result
    )


def test_non_authoritative_or_failed_tool_result_is_not_directly_returned():
    assert _authoritative_tool_result_response("websearch__search", "搜索结果") is None
    assert (
        _authoritative_tool_result_response(
            "utils_remote__get_now_context",
            "查询失败：服务不可用",
        )
        is None
    )


def _robot_move_tool(name="robot__move_robot"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string"},
                    "duration_seconds": {"type": "number"},
                    "robot_id": {"type": "string"},
                },
                "required": ["direction", "duration_seconds", "robot_id"],
            },
        },
    }


def _alarm_tool(name="tasks__create_alarm"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {
                    "time": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["time", "title"],
            },
        },
    }


class _FakeBot:
    bot_id = "xiaowen"
    name = "xiaowen"
    model = "qwen3-5-9b"
    temperature = 0.7
    max_tokens = 2000
    max_response_chars = 0
    tts_profile_id = "default_tts_profile"
    agents = []
    mcp_servers = ["websearch"]
    system_prompt = "你是小文。"


class _FakeBotManager:
    def __init__(self, bot=None):
        self._bot = bot or _FakeBot()

    def get_bot_or_default(self, bot_id):
        return self._bot


class _FakeMCPManager:
    def __init__(self, tools):
        self._tools = tools
        self.calls = []

    def get_tools_for_llm(self, server_keys):
        return self._tools

    def resolve_tool_name(self, tool_name):
        return tool_name.replace("__", ".")

    async def call_tool(self, tool_name, tool_args, max_retries=2):
        self.calls.append((tool_name, tool_args))
        return "工具结果"


class _FakeRuntimeState:
    def __init__(self, tools, bot=None):
        self.bot_manager = _FakeBotManager(bot)
        self.mcp_manager = _FakeMCPManager(tools)


class _FakeAgentRegistry:
    def get(self, agent_id):
        return None

    def find_entry_agent(self, text, context, allowed_agent_ids=None):
        return None


class _FakeSessionManager:
    def __init__(self):
        self.messages = []
        self.agent_states = {}
        self.tool_calls = []

    def get_agent_state(self, session_id):
        return self.agent_states.get(session_id)

    def set_agent_state(self, session_id, state):
        self.agent_states[session_id] = state

    def clear_agent_state(self, session_id):
        self.agent_states.pop(session_id, None)

    def add_message(self, session_id, role, content):
        self.messages.append((session_id, role, content))

    def get_messages_for_llm(self, session_id):
        return [
            {"role": role, "content": content}
            for stored_session_id, role, content in self.messages
            if stored_session_id == session_id
        ]

    def add_tool_call(self, session_id, tool_name, tool_args, result):
        self.tool_calls.append((session_id, tool_name, tool_args, result))


class _StreamingAgent:
    id = "stream_agent"
    name = "测试流式 Agent"

    def can_enter(self, text, context):
        return True

    async def start(self, text, context):
        raise AssertionError("streaming agent should use start_stream")

    async def handle(self, text, state, context):
        raise AssertionError("streaming agent should use handle_stream")

    async def start_stream(self, text, context):
        yield AgentStreamEvent(text="第一段")
        yield AgentStreamEvent(text="第二段")
        yield AgentStreamEvent(
            result=AgentResult(
                text="第一段第二段",
                state={"agent_id": self.id, "step": 1},
                finished=False,
                metadata={"persist_history": False},
            )
        )

    async def handle_stream(self, text, state, context):
        yield AgentStreamEvent(text="继续一")
        yield AgentStreamEvent(text="继续二")
        yield AgentStreamEvent(
            result=AgentResult(
                text="继续一继续二",
                state=None,
                finished=True,
                metadata={"persist_history": False},
            )
        )


class _StreamingAgentRegistry:
    def __init__(self, agent):
        self.agent = agent

    def get(self, agent_id):
        return self.agent if agent_id == self.agent.id else None

    def find_entry_agent(self, text, context, allowed_agent_ids=None):
        if allowed_agent_ids is not None and self.agent.id not in allowed_agent_ids:
            return None
        return self.agent


class _AgentBot(_FakeBot):
    agents = ["stream_agent"]
    mcp_servers = []


class _FakeContext:
    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


class _DirectOnlyLLMClient:
    def __init__(self):
        self.direct_calls = 0
        self.last_messages = None

    def _call_llm(self, messages, tools, temperature, max_tokens, model_name, tool_choice=None):
        self.direct_calls += 1
        self.last_messages = messages
        if tools is not None:
            raise AssertionError("direct chat route must not pass tools")
        yield {"type": "text", "content": "直接回复"}

    async def chat_with_tools(self, **kwargs):
        raise AssertionError("chat route must bypass chat_with_tools")
        yield


class _ChunkedDirectLLMClient:
    def __init__(self, chunks):
        self.chunks = chunks

    def _call_llm(self, messages, tools, temperature, max_tokens, model_name, tool_choice=None):
        if tools is not None:
            raise AssertionError("direct chat route must not pass tools")
        for chunk in self.chunks:
            yield {"type": "text", "content": chunk}

    async def chat_with_tools(self, **kwargs):
        raise AssertionError("chat route must bypass chat_with_tools")
        yield


class _TaggedDirectLLMClient(_DirectOnlyLLMClient):
    def _call_llm(self, messages, tools, temperature, max_tokens, model_name, tool_choice=None):
        self.direct_calls += 1
        if tools is not None:
            raise AssertionError("direct chat route must not pass tools")
        yield {"type": "text", "content": "你好\n[robot_remote]\n继续"}


class _ToolFlowLLMClient:
    def __init__(self):
        self.tool_calls = 0
        self.last_kwargs = None

    async def chat_with_tools(self, **kwargs):
        self.tool_calls += 1
        self.last_kwargs = kwargs
        yield {"type": "text", "content": "最终回复"}


class _ExecutingWebSearchLLMClient(_ToolFlowLLMClient):
    async def chat_with_tools(self, **kwargs):
        self.tool_calls += 1
        self.last_kwargs = kwargs
        result = await kwargs["tool_executor"](
            "websearch__search",
            {
                "query": "青岛特色啤酒",
                "_meta": {"session_id": "forged", "trace_id": "forged"},
            },
        )
        yield {"type": "text", "content": str(result)}


class _ChunkedToolFlowLLMClient(_ToolFlowLLMClient):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = chunks

    async def chat_with_tools(self, **kwargs):
        self.tool_calls += 1
        self.last_kwargs = kwargs
        for chunk in self.chunks:
            yield {"type": "text", "content": chunk}


class _ClassifierThenDirectLLMClient:
    def __init__(self):
        self.direct_calls = 0
        self.classifier_calls = 0
        self.classifier_messages = None
        self.classifier_temperature = None
        self.classifier_max_tokens = None
        self.classifier_model_name = None

    def _call_llm(self, messages, tools, temperature, max_tokens, model_name, tool_choice=None):
        if max_tokens <= 8:
            self.classifier_calls += 1
            self.classifier_messages = messages
            self.classifier_temperature = temperature
            self.classifier_max_tokens = max_tokens
            self.classifier_model_name = model_name
            yield {"type": "text", "content": "C"}
            return
        self.direct_calls += 1
        if tools is not None:
            raise AssertionError("classifier chat route must not pass tools")
        yield {"type": "text", "content": "直接回复"}

    async def chat_with_tools(self, **kwargs):
        raise AssertionError("classifier chat route must bypass chat_with_tools")
        yield


class _ClassifierThenToolLLMClient:
    def __init__(self, decision="X"):
        self.classifier_calls = 0
        self.tool_calls = 0
        self.last_kwargs = None
        self.decision = decision
        self.classifier_temperature = None
        self.classifier_max_tokens = None
        self.classifier_model_name = None

    def _call_llm(self, messages, tools, temperature, max_tokens, model_name, tool_choice=None):
        if max_tokens <= 8:
            self.classifier_calls += 1
            self.classifier_temperature = temperature
            self.classifier_max_tokens = max_tokens
            self.classifier_model_name = model_name
            yield {"type": "text", "content": self.decision}
            return
        raise AssertionError("classifier tool route must not use direct chat")

    async def chat_with_tools(self, **kwargs):
        self.tool_calls += 1
        self.last_kwargs = kwargs
        yield {"type": "text", "content": "最终回复"}


class _SlowClassifierLLMClient(_ClassifierThenToolLLMClient):
    def _call_llm(self, messages, tools, temperature, max_tokens, model_name, tool_choice=None):
        if max_tokens <= 8:
            self.classifier_calls += 1
            self.classifier_temperature = temperature
            self.classifier_max_tokens = max_tokens
            self.classifier_model_name = model_name
            time.sleep(0.05)
            yield {"type": "text", "content": self.decision}
            return
        raise AssertionError("slow classifier route must not use direct chat")


class _TimeoutAwareClassifierLLMClient:
    def __init__(self):
        self.stop_event_seen = threading.Event()

    def _call_llm(
        self,
        messages,
        tools,
        temperature,
        max_tokens,
        model_name,
        tool_choice=None,
        stop_event=None,
        on_response=None,
    ):
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                self.stop_event_seen.set()
                return
            time.sleep(0.005)
        yield {"type": "text", "content": "C"}


class _NoCallLLMClient:
    def _call_llm(self, *args, **kwargs):
        raise AssertionError("exit hard guard must not call LLM")
        yield

    async def chat_with_tools(self, **kwargs):
        raise AssertionError("exit hard guard must not call chat_with_tools")
        yield


class _DefaultBot:
    bot_id = "default"
    name = "默认助手"
    model = "qwen3-5-9b"
    temperature = 0.7
    max_tokens = 2000
    max_response_chars = 0
    agents = []
    mcp_servers = ["websearch"]
    system_prompt = "你是默认助手。"


def _build_servicer(llm_client, tools, bot=None):
    servicer = object.__new__(ImprovedLLMServiceServicer)
    servicer.llm_client = llm_client
    servicer.session_manager = _FakeSessionManager()
    servicer.agent_registry = _FakeAgentRegistry()
    servicer.agent_tool_invoker = None
    servicer.vision_snapshot_client = None
    servicer.runtime_state = _FakeRuntimeState(tools, bot=bot)

    async def _ensure_connected(mcp_manager, server_keys):
        return None

    servicer._ensure_mcp_servers_connected = _ensure_connected
    return servicer


class _FakeVisionSnapshotClient:
    def __init__(self, result):
        self.result = result
        self.session_ids = []

    async def fetch_latest(self, session_id):
        self.session_ids.append(session_id)
        return self.result


class ToolLatencyRoutingTest(unittest.TestCase):
    def test_stream_chat_singing_direct_call_injects_tts_profile_and_returns_control(self):
        servicer = _build_servicer(_NoCallLLMClient(), [_singing_tool()])

        async def singing_call(tool_name, tool_args, max_retries=2):
            servicer.runtime_state.mcp_manager.calls.append((tool_name, tool_args))
            return json.dumps({
                "kind": "singing_playback",
                "song_id": "001",
                "title": "爱你",
                "voice_id": "serena-v1",
                "asset_id": "serena-v1:001",
            }, ensure_ascii=False)

        servicer.runtime_state.mcp_manager.call_tool = singing_call
        request = llm_service_pb2.ChatRequest(
            text="唱首爱你我听听",
            session_id="s-singing-play",
            trace_id="trace-singing-play",
            bot_id="default",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(
            ["好～我准备一下。", "[SINGING_PLAYBACK:serena-v1:001]", ""],
            [response.text for response in responses],
        )
        tool_name, tool_args = servicer.runtime_state.mcp_manager.calls[-1]
        self.assertEqual("singing_remote__play_song", tool_name)
        self.assertEqual("唱首爱你我听听", tool_args["query"])
        self.assertEqual("default_tts_profile", tool_args["_meta"]["tts_profile_id"])
        self.assertEqual("s-singing-play", tool_args["_meta"]["session_id"])
        self.assertNotIn("robot_id", tool_args)

    def test_stream_chat_singing_catalog_returns_natural_text_without_playback(self):
        servicer = _build_servicer(
            _NoCallLLMClient(),
            [_singing_tool("singing_remote__list_songs")],
        )

        async def singing_call(tool_name, tool_args, max_retries=2):
            servicer.runtime_state.mcp_manager.calls.append((tool_name, tool_args))
            return json.dumps({
                "kind": "singing_catalog",
                "voice_id": "serena-v1",
                "query": "",
                "total_available": 2,
                "songs": [
                    {"song_id": "001", "title": "爱你", "artist": "王心凌"},
                    {"song_id": "002", "title": "彩虹的微笑", "artist": "王心凌"},
                ],
                "has_more": False,
                "next_cursor": "",
            }, ensure_ascii=False)

        servicer.runtime_state.mcp_manager.call_tool = singing_call
        request = llm_service_pb2.ChatRequest(
            text="你会唱什么歌？",
            session_id="s-singing-list",
            bot_id="default",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(
            ["我现在会唱《爱你》、《彩虹的微笑》。你想听哪一首？", ""],
            [response.text for response in responses],
        )
        _, tool_args = servicer.runtime_state.mcp_manager.calls[-1]
        self.assertEqual({"query": "", "limit": 5}, {
            "query": tool_args["query"], "limit": tool_args["limit"],
        })

    def test_stream_chat_singing_failure_does_not_promise_playback_first(self):
        servicer = _build_servicer(_NoCallLLMClient(), [_singing_tool()])

        async def singing_call(tool_name, tool_args, max_retries=2):
            servicer.runtime_state.mcp_manager.calls.append((tool_name, tool_args))
            return json.dumps({
                "kind": "singing_unavailable",
                "reason": "song_not_found",
                "message": "这首歌我还没学会，换一首好吗？",
            }, ensure_ascii=False)

        servicer.runtime_state.mcp_manager.call_tool = singing_call
        request = llm_service_pb2.ChatRequest(
            text="唱首不存在的歌",
            session_id="s-singing-missing",
            bot_id="default",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(
            ["这首歌我还没学会，换一首好吗？", ""],
            [response.text for response in responses],
        )

    def test_stream_chat_visual_intent_attaches_image_only_for_current_turn(self):
        client = _DirectOnlyLLMClient()
        servicer = _build_servicer(client, [_websearch_tool()])
        servicer.vision_snapshot_client = _FakeVisionSnapshotClient(
            VisionFetchResult(
                status="ok",
                http_status=200,
                snapshot=VisionSnapshot(
                    jpeg=b"\xff\xd8vision\xff\xd9",
                    frame_id="17",
                    captured_at_ms=1_900_000_000_000,
                    age_ms=250,
                ),
            )
        )
        request = llm_service_pb2.ChatRequest(
            text="前面有什么？",
            session_id="s-vision",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(["直接回复", ""], [response.text for response in responses])
        self.assertEqual(["s-vision"], servicer.vision_snapshot_client.session_ids)
        self.assertEqual(1, client.direct_calls)
        self.assertIn("只根据本轮提供的图片", client.last_messages[0]["content"])
        current_user_content = client.last_messages[-1]["content"]
        self.assertIsInstance(current_user_content, list)
        self.assertEqual("text", current_user_content[0]["type"])
        self.assertEqual("image_url", current_user_content[1]["type"])
        self.assertTrue(
            current_user_content[1]["image_url"]["url"].startswith(
                "data:image/jpeg;base64,"
            )
        )
        history_contents = [
            content for _session_id, _role, content in servicer.session_manager.messages
        ]
        self.assertEqual(["前面有什么？", "直接回复"], history_contents)
        self.assertNotIn("data:image", str(history_contents))
        metrics = json.loads(responses[-1].metrics_json)
        self.assertTrue(metrics["llm_vision_intent"])
        self.assertEqual("vision_context", metrics["llm_router_source"])
        self.assertEqual(0, metrics["llm_tools_count"])

    def test_stream_chat_visual_intent_without_snapshot_does_not_call_llm(self):
        client = _DirectOnlyLLMClient()
        servicer = _build_servicer(client, [_websearch_tool()])
        servicer.vision_snapshot_client = _FakeVisionSnapshotClient(
            VisionFetchResult(status="unavailable", http_status=404)
        )
        request = llm_service_pb2.ChatRequest(
            text="你看到了什么？",
            session_id="s-no-vision",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(
            ["我现在没有拿到最新画面，你再试一次好吗？", ""],
            [response.text for response in responses],
        )
        self.assertEqual(0, client.direct_calls)
        metrics = json.loads(responses[-1].metrics_json)
        self.assertEqual("unavailable", metrics["llm_vision_fetch_status"])
        self.assertEqual("vision_unavailable", metrics["llm_stream_mode"])

    def test_stream_chat_non_visual_request_does_not_fetch_snapshot(self):
        client = _DirectOnlyLLMClient()
        servicer = _build_servicer(client, [_websearch_tool()])
        servicer.vision_snapshot_client = _FakeVisionSnapshotClient(
            VisionFetchResult(status="unavailable", http_status=404)
        )
        request = llm_service_pb2.ChatRequest(
            text="你怎么看这个方案？",
            session_id="s-non-visual",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(["直接回复", ""], [response.text for response in responses])
        self.assertEqual([], servicer.vision_snapshot_client.session_ids)
        metrics = json.loads(responses[-1].metrics_json)
        self.assertFalse(metrics["llm_vision_intent"])

    def test_experiment_flag_defaults_enabled(self):
        old_value = os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
        try:
            self.assertTrue(_is_tool_latency_experiment_enabled())
        finally:
            if old_value is not None:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_experiment_flag_accepts_false_values(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "false"
        try:
            self.assertFalse(_is_tool_latency_experiment_enabled())
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_experiment_flag_accepts_true_values(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            self.assertTrue(_is_tool_latency_experiment_enabled())
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_router_classifier_defaults_enabled(self):
        old_value = os.environ.pop("LLM_TOOL_ROUTER_CLASSIFIER", None)
        try:
            self.assertTrue(_is_tool_router_classifier_enabled())
        finally:
            if old_value is not None:
                os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = old_value

    def test_router_classifier_accepts_false_values(self):
        old_value = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "false"
        try:
            self.assertFalse(_is_tool_router_classifier_enabled())
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_ROUTER_CLASSIFIER", None)
            else:
                os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = old_value

    def test_router_classifier_max_tokens_defaults_to_one(self):
        old_value = os.environ.pop("LLM_TOOL_ROUTER_CLASSIFIER_MAX_TOKENS", None)
        try:
            self.assertEqual(1, _tool_router_classifier_max_tokens())
        finally:
            if old_value is not None:
                os.environ["LLM_TOOL_ROUTER_CLASSIFIER_MAX_TOKENS"] = old_value

    def test_router_prompt_contains_main_intent_examples_and_tool_summary(self):
        messages = _build_tool_router_classifier_messages("帮我讲一个青岛下雨的故事")
        prompt = messages[0]["content"]

        self.assertIn("判断用户的主意图", prompt)
        self.assertIn("讲一个青岛下雨的故事", prompt)
        self.assertIn("青岛现在下雨吗", prompt)
        self.assertIn("E、C、V、W、R、S、T、U、X", prompt)
        self.assertIn("websearch", prompt)
        self.assertIn("robot", prompt)
        self.assertIn("呼叫视频电话", prompt)
        self.assertIn("V 用于基于当前画面回答视觉问题", prompt)
        self.assertIn("task", prompt)
        self.assertIn("utils", prompt)
        self.assertIn("exit", prompt)
        self.assertIn("complex", prompt)
        self.assertIn("这身衣服合不合适", prompt)
        self.assertNotIn("不要输出标点", prompt)
        self.assertNotIn("unknown", prompt)
        self.assertEqual("帮我讲一个青岛下雨的故事", messages[1]["content"])

    def test_router_prompt_uses_recent_history_and_keeps_latest_user_plain(self):
        conversation = [
            {"role": "system", "content": "不应进入 Router"},
            {"role": "user", "content": "帮我看看青岛天气"},
            {"role": "assistant", "content": "青岛今天晴。"},
            {"role": "user", "content": "那北京呢"},
            {"role": "assistant", "content": "北京今天多云。"},
            {"role": "user", "content": "那明天呢"},
        ]

        messages = _build_tool_router_classifier_messages("那明天呢", conversation)

        self.assertIn("只分类最后一条用户消息", messages[0]["content"])
        self.assertIn("帮我看看青岛天气", messages[0]["content"])
        self.assertNotIn("不应进入 Router", messages[0]["content"])
        self.assertEqual(
            [
                {"role": "user", "content": "那北京呢"},
                {"role": "assistant", "content": "北京今天多云。"},
                {"role": "user", "content": "那明天呢"},
            ],
            messages[1:],
        )
        self.assertNotIn("用户最新输入", messages[-1]["content"])

    def test_router_prompt_limits_history_to_ten_messages(self):
        conversation = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"历史-{index}"}
            for index in range(14)
        ]
        conversation.append({"role": "user", "content": "最新问题"})

        messages = _build_tool_router_classifier_messages("最新问题", conversation)
        combined = "\n".join(message["content"] for message in messages)

        self.assertNotIn("历史-3", combined)
        self.assertIn("历史-4", combined)
        self.assertIn("历史-13", combined)
        self.assertEqual({"role": "user", "content": "最新问题"}, messages[-1])

    def test_router_classifier_timeout_signals_worker_stop(self):
        old_timeout = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS")
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS"] = "1"
        client = _TimeoutAwareClassifierLLMClient()
        try:
            route = _classify_legacy_tool_latency_route(
                client,
                "帮我讲个故事",
                "router-model",
                [_websearch_tool()],
            )
            self.assertEqual("legacy", route.kind)
            self.assertTrue(client.stop_event_seen.wait(timeout=0.5))
        finally:
            if old_timeout is None:
                os.environ.pop("LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS", None)
            else:
                os.environ["LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS"] = old_timeout

    def test_chat_route_for_plain_conversation(self):
        route = _build_tool_latency_route("你今天怎么样？", [_websearch_tool()])

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.progress_text)

    def test_story_request_uses_chat_route(self):
        route = _build_tool_latency_route("讲个故事。", [_websearch_tool()])

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.progress_text)

    def test_weather_route_emits_safe_progress_text(self):
        route = _build_tool_latency_route("今天青岛的天气怎么样？", [_websearch_tool()])

        self.assertEqual("tool", route.kind)
        self.assertIn(route.progress_text, quick_reply_pool("tool.weather"))
        self.assertNotIn("青岛", route.progress_text)

    def test_gold_price_route_emits_safe_progress_text(self):
        route = _build_tool_latency_route("今天黄金价格是多少？", [_websearch_tool()])

        self.assertEqual("tool", route.kind)
        self.assertIn(route.progress_text, quick_reply_pool("tool.websearch"))
        self.assertNotIn("黄金", route.progress_text)

    def test_robot_route_uses_action_acknowledgement(self):
        route = _build_tool_latency_route("往前走两秒", [_robot_move_tool()])

        self.assertEqual("tool", route.kind)
        self.assertIn(route.progress_text, robot_action_phrase_pool("move_forward"))

    def test_video_call_route_requires_explicit_positive_intent(self):
        tool = _robot_video_call_tool()
        for text in (
            "我要拨打视频电话",
            "呼叫视频电话",
            "帮我打个视频电话",
            "发起视频通话",
        ):
            with self.subTest(text=text):
                route = _build_tool_latency_route(text, [tool])
                self.assertEqual("tool", route.kind)
                self.assertEqual("robot_remote__call_video", route.selected_tool_name)
                self.assertIn(route.progress_text, robot_action_phrase_pool("video_call"))

    def test_video_call_route_rejects_questions_and_negations(self):
        tool = _robot_video_call_tool()
        for text in (
            "你会打视频电话吗",
            "怎么打视频电话",
            "不要拨打视频电话",
            "先别发起视频通话",
            "取消视频电话",
        ):
            with self.subTest(text=text):
                route = _build_tool_latency_route(text, [tool])
                self.assertNotEqual("tool", route.kind)

    def test_router_prompt_distinguishes_specific_visual_state_and_vision_workflow(self):
        prompt = _build_tool_router_classifier_messages("测试")[0]["content"]

        self.assertIn("某个具体物体的可见状态", prompt)
        self.assertIn("视觉观察后再创建提醒也是 X", prompt)
        self.assertIn("先别发起视频通话 -> C", prompt)
        self.assertIn("'取消一切'没有说明取消对象", prompt)
        self.assertIn("what's the time and weather -> X", prompt)

    def test_ambiguous_toolless_request_uses_legacy_route(self):
        route = _build_tool_latency_route("帮我处理一下", None)

        self.assertEqual("legacy", route.kind)
        self.assertIsNone(route.progress_text)

    def test_non_realtime_tool_request_uses_legacy_route(self):
        route = _build_tool_latency_route("明天早上八点提醒我开会", [_alarm_tool()])

        self.assertEqual("legacy", route.kind)
        self.assertIsNone(route.progress_text)

    def test_websearch_args_are_capped_to_three_results_when_supported(self):
        tool = {
            "type": "function",
            "function": {
                "name": "websearch__search",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                },
            },
        }

        args = _limit_websearch_args(
            "websearch.search",
            tool,
            {"query": "黄金价格", "count": 5},
        )

        self.assertEqual({"query": "黄金价格", "count": 3}, args)

    def test_websearch_args_default_to_three_results_when_count_is_supported(self):
        tool = {
            "type": "function",
            "function": {
                "name": "websearch__search",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                },
            },
        }

        args = _limit_websearch_args(
            "websearch.search",
            tool,
            {"query": "黄金价格"},
        )

        self.assertEqual({"query": "黄金价格", "count": 3}, args)

    def test_stream_chat_chat_route_bypasses_tool_orchestration_when_enabled(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_preface = os.environ.get("LLM_CHAT_PREFACE_PROBABILITY")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_CHAT_PREFACE_PROBABILITY"] = "1.0"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _DirectOnlyLLMClient()
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="你今天怎么样？", session_id="s-chat")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["直接回复", ""], [response.text for response in responses])
            self.assertEqual(1, servicer.llm_client.direct_calls)
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value
            if old_preface is None:
                os.environ.pop("LLM_CHAT_PREFACE_PROBABILITY", None)
            else:
                os.environ["LLM_CHAT_PREFACE_PROBABILITY"] = old_preface

    def test_stream_chat_no_tools_bypasses_tool_orchestration_when_enabled(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = _build_servicer(_DirectOnlyLLMClient(), [])
            request = llm_service_pb2.ChatRequest(text="随机讲一句话。", session_id="s-no-tools")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["直接回复", ""], [response.text for response in responses])
            self.assertEqual(1, servicer.llm_client.direct_calls)
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_time_request_without_tools_refuses_to_guess(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            client = _DirectOnlyLLMClient()
            servicer = _build_servicer(client, [])
            request = llm_service_pb2.ChatRequest(
                text="请通过时间工具告诉我现在星期几。",
                session_id="s-no-utils",
            )

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(
                ["我现在没有可用的时间查询能力，暂时不能确认准确时间。", ""],
                [response.text for response in responses],
            )
            self.assertEqual(0, client.direct_calls)
            metrics = json.loads(responses[-1].metrics_json)
            self.assertEqual("tool_unavailable", metrics["llm_stream_mode"])
            self.assertEqual("deterministic_unavailable", metrics["llm_router_source"])
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_preclassified_tool_without_mcp_uses_safe_fallback(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = _build_servicer(_DirectOnlyLLMClient(), [])
            servicer._workflow_route_cache = OrderedDict()
            servicer._cache_workflow_route(
                SimpleNamespace(
                    session_id="s-no-weather-tool",
                    text="青岛今天天气怎么样",
                    bot_id="xiaowen",
                    robot_id="",
                ),
                ToolLatencyRoute(
                    "tool",
                    source="llm_router_async",
                    category="websearch",
                ),
            )
            request = llm_service_pb2.ChatRequest(
                text="青岛今天天气怎么样",
                session_id="s-no-weather-tool",
            )

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(2, len(responses))
            self.assertIn("没有可用的联网查询能力", responses[0].text)
            self.assertEqual(0, servicer.llm_client.direct_calls)
            self.assertEqual("tool_unavailable", json.loads(responses[-1].metrics_json)["llm_stream_mode"])
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_value)

    def test_stream_chat_preclassified_utils_route_recovers_exact_tool(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            client = _ToolFlowLLMClient()
            servicer = _build_servicer(
                client,
                [
                    _utils_tool("utils_remote__get_now_context"),
                    _utils_tool("utils_remote__format_timestamp"),
                ],
            )
            servicer._workflow_route_cache = OrderedDict()
            servicer._cache_workflow_route(
                SimpleNamespace(
                    session_id="s-utils-preclassified",
                    text="请通过时间工具告诉我现在的完整日期、时间和星期。",
                    bot_id="xiaowen",
                    robot_id="",
                ),
                ToolLatencyRoute(
                    "tool",
                    source="llm_router_async",
                    category="utils",
                    tool_prefix="utils_remote.",
                    require_tool_call=True,
                ),
            )
            request = llm_service_pb2.ChatRequest(
                text="请通过时间工具告诉我现在的完整日期、时间和星期。",
                session_id="s-utils-preclassified",
                bot_id="xiaowen",
            )

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(
                ["utils_remote__get_now_context"],
                _tool_names(client.last_kwargs["tools"]),
            )
            metrics = json.loads(responses[-1].metrics_json)
            self.assertEqual(
                "utils_remote__get_now_context",
                metrics["llm_selected_tool_name"],
            )
            self.assertEqual(1, metrics["llm_first_round_tools_count"])
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_value)

    def test_stream_chat_sanitizes_standalone_tool_tag_before_history(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_preface = os.environ.get("LLM_CHAT_PREFACE_PROBABILITY")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_CHAT_PREFACE_PROBABILITY"] = "1.0"
        try:
            servicer = _build_servicer(_TaggedDirectLLMClient(), [_websearch_tool()])
            request = llm_service_pb2.ChatRequest(text="你今天怎么样？", session_id="s-tagged")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["你好\n[robot_remote]\n继续", ""], [response.text for response in responses])
            self.assertIn(("s-tagged", "assistant", "你好\n继续"), servicer.session_manager.messages)
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value
            if old_preface is None:
                os.environ.pop("LLM_CHAT_PREFACE_PROBABILITY", None)
            else:
                os.environ["LLM_CHAT_PREFACE_PROBABILITY"] = old_preface

    def test_stream_chat_caps_direct_llm_text_before_history(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"

        class _LimitedBot(_FakeBot):
            max_response_chars = 5
            mcp_servers = []

        try:
            servicer = _build_servicer(
                _ChunkedDirectLLMClient(["你好", "世界很长"]),
                [],
                bot=_LimitedBot(),
            )
            request = llm_service_pb2.ChatRequest(text="讲个故事", session_id="s-char-cap-chat")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["你好", "世界很", ""], [response.text for response in responses])
            self.assertIn(("s-char-cap-chat", "assistant", "你好世界很"), servicer.session_manager.messages)
            metrics = json.loads(responses[-1].metrics_json)
            self.assertEqual(5, metrics["llm_max_response_chars"])
            self.assertEqual(5, metrics["llm_response_chars"])
            self.assertEqual(5, metrics["llm_response_chars_sent"])
            self.assertTrue(metrics["llm_response_truncated"])
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_value)

    def test_stream_chat_caps_tool_orchestration_final_text(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "false"

        class _LimitedBot(_FakeBot):
            max_response_chars = 4

        try:
            servicer = _build_servicer(
                _ChunkedToolFlowLLMClient(["最终", "回复很长"]),
                [_websearch_tool()],
                bot=_LimitedBot(),
            )
            request = llm_service_pb2.ChatRequest(text="帮我查一下", session_id="s-char-cap-tool")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["最终", "回复", ""], [response.text for response in responses])
            self.assertIn(("s-char-cap-tool", "assistant", "最终回复"), servicer.session_manager.messages)
            metrics = json.loads(responses[-1].metrics_json)
            self.assertEqual("tool_or_legacy", metrics["llm_stream_mode"])
            self.assertEqual(4, metrics["llm_max_response_chars"])
            self.assertEqual(4, metrics["llm_response_chars"])
            self.assertTrue(metrics["llm_response_truncated"])
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_value)

    def test_stream_chat_does_not_truncate_exit_tagged_text(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "false"
        exit_text = "好的，我去充电了。[EXIT]"

        class _LimitedBot(_FakeBot):
            max_response_chars = 3

        try:
            servicer = _build_servicer(
                _ChunkedToolFlowLLMClient([exit_text]),
                [_websearch_tool()],
                bot=_LimitedBot(),
            )
            request = llm_service_pb2.ChatRequest(text="去充电", session_id="s-exit-tag-budget")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual([exit_text, ""], [response.text for response in responses])
            metrics = json.loads(responses[-1].metrics_json)
            self.assertEqual(3, metrics["llm_max_response_chars"])
            self.assertEqual(len(exit_text), metrics["llm_response_chars"])
            self.assertFalse(metrics["llm_response_truncated"])
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_value)

    def test_stream_chat_entry_agent_can_emit_incremental_chunks(self):
        agent = _StreamingAgent()
        servicer = _build_servicer(_NoCallLLMClient(), [], bot=_AgentBot())
        servicer.agent_registry = _StreamingAgentRegistry(agent)
        request = llm_service_pb2.ChatRequest(text="开始流式 Agent", session_id="s-agent-stream")

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(["第一段", "第二段", ""], [response.text for response in responses])
        self.assertTrue(responses[-1].is_final)
        self.assertEqual(
            {"agent_id": "stream_agent", "step": 1},
            servicer.session_manager.agent_states["s-agent-stream"],
        )

    def test_stream_chat_active_agent_can_emit_incremental_chunks(self):
        agent = _StreamingAgent()
        servicer = _build_servicer(_NoCallLLMClient(), [], bot=_AgentBot())
        servicer.agent_registry = _StreamingAgentRegistry(agent)
        servicer.session_manager.set_agent_state(
            "s-active-agent-stream",
            {"agent_id": "stream_agent", "step": 1},
        )
        request = llm_service_pb2.ChatRequest(text="继续", session_id="s-active-agent-stream")

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(["继续一", "继续二", ""], [response.text for response in responses])
        self.assertTrue(responses[-1].is_final)
        self.assertNotIn("s-active-agent-stream", servicer.session_manager.agent_states)

    def test_stream_chat_whole_session_exit_bypasses_llm_and_tools(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.llm_client = _NoCallLLMClient()
        servicer.session_manager = _FakeSessionManager()
        servicer.agent_registry = _FakeAgentRegistry()
        servicer.agent_tool_invoker = None
        servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])

        async def _ensure_connected(mcp_manager, server_keys):
            raise AssertionError("exit hard guard must not connect MCP")

        servicer._ensure_mcp_servers_connected = _ensure_connected
        request = llm_service_pb2.ChatRequest(text="退出吧", session_id="s-exit")

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(2, len(responses))
        self.assertTrue(responses[0].text.endswith("[EXIT]"))
        self.assertTrue(responses[1].is_final)

    def test_stream_chat_punctuated_exit_with_audio_context_bypasses_llm_and_tools(self):
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.llm_client = _NoCallLLMClient()
        servicer.session_manager = _FakeSessionManager()
        servicer.agent_registry = _FakeAgentRegistry()
        servicer.agent_tool_invoker = None
        servicer.runtime_state = _FakeRuntimeState([_robot_remote_tool()])

        async def _ensure_connected(mcp_manager, server_keys):
            raise AssertionError("punctuated exit hard guard must not connect MCP")

        servicer._ensure_mcp_servers_connected = _ensure_connected
        request = llm_service_pb2.ChatRequest(
            text="[语音上下文: 情绪=emo_unknown]\n用户说：退下吧。",
            session_id="s-exit-punctuated",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual(2, len(responses))
        self.assertTrue(responses[0].text.endswith("[EXIT]"))
        self.assertTrue(responses[1].is_final)

    def test_stream_chat_tool_route_emits_progress_before_existing_tool_flow(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _ToolFlowLLMClient()
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="今天青岛的天气怎么样？", session_id="s-weather")

            responses = _collect_stream_chat(servicer, request)

            self.assertIn(responses[0].text, quick_reply_pool("tool.weather"))
            self.assertNotIn("青岛", responses[0].text)
            self.assertEqual(["最终回复", ""], [response.text for response in responses[1:]])
            self.assertEqual(1, servicer.llm_client.tool_calls)
            self.assertEqual(["websearch__search"], _tool_names(servicer.llm_client.last_kwargs["tools"]))
            self.assertEqual([], _tool_names(servicer.llm_client.last_kwargs["followup_tools"]))
            # 回退 d0236fc7: 不传 forced_function, 让 9B streaming 自己选
            self.assertIsNone(servicer.llm_client.last_kwargs["tool_choice"])
            self.assertTrue(servicer.llm_client.last_kwargs["require_tool_call"])
            self.assertFalse(servicer.llm_client.last_kwargs["emit_tool_wait_message"])
            self.assertEqual("tool", servicer.llm_client.last_kwargs["router_kind"])
            self.assertEqual("deterministic", servicer.llm_client.last_kwargs["router_source"])
            self.assertEqual("websearch", servicer.llm_client.last_kwargs["router_category"])
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_deterministic_robot_move_directly_calls_mcp(self):
        cases = [
            ("往前。", "forward", "move_forward"),
            ("打个招呼。", "greet", "greet"),
            ("招个手吧。", "greet", "greet"),
            ("来握个手。", "handshake", "handshake"),
            ("欢呼一下。", "cheer", "cheer"),
        ]
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            for text, expected_action, progress_pool_action in cases:
                with self.subTest(text=text):
                    servicer = _build_servicer(_NoCallLLMClient(), [_robot_remote_tool()])
                    request = llm_service_pb2.ChatRequest(
                        text=text,
                        session_id=f"s-direct-robot-{expected_action}",
                        robot_id="human_e1_01",
                        trace_id=f"trace-direct-robot-{expected_action}",
                    )

                    responses = _collect_stream_chat(servicer, request)

                    self.assertEqual(2, len(responses))
                    self.assertIn(responses[0].text, robot_action_phrase_pool(progress_pool_action))
                    self.assertEqual("", responses[1].text)
                    self.assertTrue(responses[1].is_final)
                    self.assertEqual(
                        [
                            (
                                "robot_remote__move_robot",
                                {
                                    "action": expected_action,
                                    "robot_id": "human_e1_01",
                                    "_meta": {
                                        "session_id": f"s-direct-robot-{expected_action}",
                                        "trace_id": f"trace-direct-robot-{expected_action}",
                                    },
                                },
                            )
                        ],
                        servicer.runtime_state.mcp_manager.calls,
                    )
                    self.assertEqual(
                        [
                            (
                                f"s-direct-robot-{expected_action}",
                                "robot_remote.move_robot",
                                {
                                    "action": expected_action,
                                    "robot_id": "human_e1_01",
                                    "_meta": {
                                        "session_id": f"s-direct-robot-{expected_action}",
                                        "trace_id": f"trace-direct-robot-{expected_action}",
                                    },
                                },
                                "工具结果",
                            )
                        ],
                        servicer.session_manager.tool_calls,
                    )
                    metrics = json.loads(responses[1].metrics_json)
                    self.assertEqual("deterministic_robot_tool_direct", metrics["llm_stream_mode"])
                    self.assertTrue(metrics["llm_direct_tool_call"])
                    self.assertEqual(len(responses[0].text), metrics["llm_response_chars"])
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_deterministic_video_call_injects_session_robot_id(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = _build_servicer(_NoCallLLMClient(), [_robot_video_call_tool()])
            request = llm_service_pb2.ChatRequest(
                text="我要拨打视频电话。",
                session_id="s-direct-video-call",
                robot_id="human_e1_01",
                trace_id="trace-direct-video-call",
            )

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(3, len(responses))
            self.assertIn(responses[0].text, robot_action_phrase_pool("video_call"))
            self.assertEqual("[EXIT]", responses[1].text)
            self.assertTrue(responses[2].is_final)
            self.assertEqual(
                [
                    (
                        "robot_remote__call_video",
                        {
                            "robot_id": "human_e1_01",
                            "_meta": {
                                "session_id": "s-direct-video-call",
                                "trace_id": "trace-direct-video-call",
                            },
                        },
                    )
                ],
                servicer.runtime_state.mcp_manager.calls,
            )
            metrics = json.loads(responses[2].metrics_json)
            self.assertEqual("deterministic_robot_tool_direct", metrics["llm_stream_mode"])
            self.assertTrue(metrics["llm_direct_tool_call"])
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_failed_video_call_does_not_exit(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = _build_servicer(_NoCallLLMClient(), [_robot_video_call_tool()])

            async def failed_call_tool(tool_name, tool_args):
                servicer.runtime_state.mcp_manager.calls.append((tool_name, tool_args))
                return "工具未执行：MQTT 发布失败"

            servicer.runtime_state.mcp_manager.call_tool = failed_call_tool
            request = llm_service_pb2.ChatRequest(
                text="呼叫视频电话。",
                session_id="s-failed-video-call",
                robot_id="human_e1_01",
            )

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(3, len(responses))
            self.assertIn(responses[0].text, robot_action_phrase_pool("video_call"))
            self.assertIn("MQTT 发布失败", responses[1].text)
            self.assertNotIn("[EXIT]", "".join(response.text for response in responses))
            self.assertTrue(responses[2].is_final)
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_weather_without_city_rewrites_query_to_qingdao_for_model(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _ToolFlowLLMClient()
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="帮我查一下天气。", session_id="s-weather-default")

            responses = _collect_stream_chat(servicer, request)

            self.assertIn(responses[0].text, quick_reply_pool("tool.weather"))
            self.assertNotIn("青岛", responses[0].text)
            sent_messages = servicer.llm_client.last_kwargs["messages"]
            self.assertTrue(
                any(
                    message["role"] == "user" and message["content"] == "帮我查一下青岛今天的天气。"
                    for message in sent_messages
                )
            )
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_weather_with_city_keeps_original_query_for_model(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _ToolFlowLLMClient()
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="今天北京天气怎么样？", session_id="s-weather-city")

            responses = _collect_stream_chat(servicer, request)

            self.assertIn(responses[0].text, quick_reply_pool("tool.weather"))
            self.assertNotIn("北京", responses[0].text)
            sent_messages = servicer.llm_client.last_kwargs["messages"]
            self.assertTrue(
                any(
                    message["role"] == "user" and message["content"] == "今天北京天气怎么样？"
                    for message in sent_messages
                )
            )
            self.assertFalse(
                any(
                    message["role"] == "user" and message["content"] == "帮我查一下青岛今天的天气。"
                    for message in sent_messages
                )
            )
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_default_uses_shared_tool_route(self):
        old_value = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _ToolFlowLLMClient()
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()], bot=_DefaultBot())

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="今天青岛的天气怎么样？", session_id="s-default")

            responses = _collect_stream_chat(servicer, request)

            self.assertTrue(servicer.llm_client.last_kwargs["require_tool_call"])
            self.assertEqual(
                ["websearch__search"],
                _tool_names(servicer.llm_client.last_kwargs["tools"]),
            )
        finally:
            if old_value is None:
                os.environ.pop("LLM_TOOL_LATENCY_EXPERIMENT", None)
            else:
                os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = old_value

    def test_stream_chat_classifier_decision_one_uses_direct_streaming(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _ClassifierThenDirectLLMClient()
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])
            servicer.session_manager.add_message("s-classifier-chat", "user", "你能做什么？")
            servicer.session_manager.add_message(
                "s-classifier-chat",
                "assistant",
                "我可以陪你聊天，也可以使用一些工具。",
            )

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="给我介绍一下你自己。", session_id="s-classifier-chat")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["直接回复", ""], [response.text for response in responses])
            self.assertEqual(1, servicer.llm_client.classifier_calls)
            self.assertEqual(1, servicer.llm_client.direct_calls)
            self.assertEqual(0.0, servicer.llm_client.classifier_temperature)
            self.assertEqual(1, servicer.llm_client.classifier_max_tokens)
            self.assertEqual(
                [
                    {"role": "user", "content": "你能做什么？"},
                    {"role": "assistant", "content": "我可以陪你聊天，也可以使用一些工具。"},
                    {"role": "user", "content": "给我介绍一下你自己。"},
                ],
                servicer.llm_client.classifier_messages[1:],
            )
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_stream_chat_classifier_complex_uses_tool_flow_without_progress_text(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _ClassifierThenToolLLMClient("X")
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="帮我确认一下这件事。", session_id="s-classifier-tool")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["最终回复", ""], [response.text for response in responses])
            self.assertEqual(1, servicer.llm_client.classifier_calls)
            self.assertEqual(1, servicer.llm_client.tool_calls)
            self.assertFalse(servicer.llm_client.last_kwargs["emit_tool_wait_message"])
            # d0236fc7: complex 类别下 require_tool_call=False（不在 _append_required_tool_instruction
            # 路径里走强制 tool_choice 逻辑），所以 tool_choice 保持 None
            self.assertIsNone(servicer.llm_client.last_kwargs["tool_choice"])
            self.assertFalse(servicer.llm_client.last_kwargs["require_tool_call"])
            self.assertEqual("tool", servicer.llm_client.last_kwargs["router_kind"])
            self.assertEqual("complex", servicer.llm_client.last_kwargs["router_category"])
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_stream_chat_classifier_exit_uses_fixed_exit_response(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            client = _ClassifierThenToolLLMClient("E")
            servicer = _build_servicer(
                client,
                [_websearch_tool(), _robot_remote_tool(), _task_tool(), _utils_tool()],
            )
            request = llm_service_pb2.ChatRequest(text="我不聊了，你退出吧。", session_id="s-router-exit")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(2, len(responses))
            self.assertTrue(responses[0].text.endswith("[EXIT]"))
            self.assertTrue(responses[1].is_final)
            self.assertEqual(1, client.classifier_calls)
            self.assertEqual(0, client.tool_calls)
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_stream_chat_classifier_websearch_category_narrows_first_round_tools(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            client = _ClassifierThenToolLLMClient("W")
            servicer = _build_servicer(
                client,
                [_websearch_tool(), _robot_remote_tool(), _task_tool(), _utils_tool()],
            )
            request = llm_service_pb2.ChatRequest(text="帮我确认一下这件事。", session_id="s-router-web")

            responses = _collect_stream_chat(servicer, request)

            self.assertIn(responses[0].text, quick_reply_pool("tool.websearch"))
            self.assertEqual(["最终回复", ""], [response.text for response in responses[1:]])
            self.assertEqual(["websearch__search"], _tool_names(client.last_kwargs["tools"]))
            self.assertEqual([], _tool_names(client.last_kwargs["followup_tools"]))
            # 回退 d0236fc7 forced_function: 让 9B streaming 自己选
            self.assertIsNone(client.last_kwargs["tool_choice"])
            self.assertTrue(client.last_kwargs["require_tool_call"])
            self.assertEqual("tool", client.last_kwargs["router_kind"])
            self.assertEqual("llm_router", client.last_kwargs["router_source"])
            self.assertEqual("websearch", client.last_kwargs["router_category"])
            self.assertEqual(0.0, client.classifier_temperature)
            self.assertEqual(1, client.classifier_max_tokens)
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_websearch_transient_tool_failure_retries_once_without_mcp_internal_retry(self):
        client = _ExecutingWebSearchLLMClient()
        servicer = _build_servicer(client, [_websearch_tool()])
        attempts = []

        async def flaky_call_tool(tool_name, tool_args, max_retries=2):
            attempts.append(max_retries)
            if len(attempts) == 1:
                raise ConnectionError("connection reset by peer")
            return "搜索成功"

        servicer.runtime_state.mcp_manager.call_tool = flaky_call_tool
        request = llm_service_pb2.ChatRequest(
            text="今天青岛天气怎么样？",
            session_id="s-websearch-retry",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual([0, 0], attempts)
        self.assertEqual("搜索成功", responses[1].text)

    def test_non_robot_tool_drops_forged_audit_meta(self):
        client = _ExecutingWebSearchLLMClient()
        servicer = _build_servicer(client, [_websearch_tool()])
        request = llm_service_pb2.ChatRequest(
            text="今天青岛天气怎么样？",
            session_id="s-websearch-meta",
            trace_id="trace-websearch-meta",
        )

        _collect_stream_chat(servicer, request)

        self.assertEqual(
            [("websearch__search", {"query": "青岛特色啤酒"})],
            servicer.runtime_state.mcp_manager.calls,
        )

    def test_robot_audit_meta_separates_same_session_turns_across_bots(self):
        class BotByIdManager:
            def __init__(self):
                self.bots = {
                    bot_id: SimpleNamespace(
                        bot_id=bot_id,
                        name=bot_id,
                        model="qwen3-5-9b",
                        temperature=0.7,
                        max_tokens=2000,
                        max_response_chars=0,
                        agents=[],
                        mcp_servers=["robot_remote"],
                        system_prompt=f"你是 {bot_id}。",
                    )
                    for bot_id in ("bot-a", "bot-b")
                }

            def get_bot_or_default(self, bot_id):
                return self.bots[bot_id]

        servicer = _build_servicer(_NoCallLLMClient(), [_robot_remote_tool()])
        servicer.runtime_state.bot_manager = BotByIdManager()

        for bot_id, text, trace_id in (
            ("bot-a", "往前。", "trace-turn-a"),
            ("bot-b", "往后。", "trace-turn-b"),
        ):
            request = llm_service_pb2.ChatRequest(
                text=text,
                session_id="shared-session",
                bot_id=bot_id,
                robot_id="human_e1_01",
                trace_id=trace_id,
            )
            _collect_stream_chat(servicer, request)

        self.assertEqual(
            [
                {
                    "session_id": "shared-session",
                    "trace_id": "trace-turn-a",
                },
                {
                    "session_id": "shared-session",
                    "trace_id": "trace-turn-b",
                },
            ],
            [tool_args["_meta"] for _, tool_args in servicer.runtime_state.mcp_manager.calls],
        )

    def test_websearch_non_transient_tool_failure_does_not_retry(self):
        client = _ExecutingWebSearchLLMClient()
        servicer = _build_servicer(client, [_websearch_tool()])
        attempts = []

        async def invalid_call_tool(tool_name, tool_args, max_retries=2):
            attempts.append(max_retries)
            raise ValueError("工具参数无效")

        servicer.runtime_state.mcp_manager.call_tool = invalid_call_tool
        request = llm_service_pb2.ChatRequest(
            text="今天青岛天气怎么样？",
            session_id="s-websearch-no-retry",
        )

        responses = _collect_stream_chat(servicer, request)

        self.assertEqual([0], attempts)
        self.assertEqual("我这边暂时没查到你要的信息，你稍后再问我一次吧。", responses[1].text)

    def test_websearch_uses_eight_second_timeout_and_retries_timeout_once(self):
        self.assertEqual(8.0, _WEBSEARCH_TOOL_TIMEOUT_SEC)
        client = _ExecutingWebSearchLLMClient()
        servicer = _build_servicer(client, [_websearch_tool()])
        attempts = []

        async def slow_call_tool(tool_name, tool_args, max_retries=2):
            attempts.append(max_retries)
            import asyncio

            await asyncio.sleep(0.05)
            return "不应返回"

        servicer.runtime_state.mcp_manager.call_tool = slow_call_tool
        request = llm_service_pb2.ChatRequest(
            text="今天青岛天气怎么样？",
            session_id="s-websearch-timeout",
        )

        with patch("llm.llm_grpc_server._WEBSEARCH_TOOL_TIMEOUT_SEC", 0.001):
            responses = _collect_stream_chat(servicer, request)

        self.assertEqual([0, 0], attempts)
        self.assertEqual("我这边暂时没查到你要的信息，你稍后再问我一次吧。", responses[1].text)

    def test_stream_chat_classifier_can_use_separate_router_model(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            main_client = _ToolFlowLLMClient()
            router_client = _ClassifierThenToolLLMClient("W")
            servicer = _build_servicer(
                main_client,
                [_websearch_tool(), _robot_remote_tool(), _task_tool(), _utils_tool()],
            )
            servicer.router_llm_client = router_client
            servicer.router_model_name = "qwen3-5-9b"
            request = llm_service_pb2.ChatRequest(text="帮我确认一下这件事。", session_id="s-router-model")

            responses = _collect_stream_chat(servicer, request)

            self.assertIn(responses[0].text, quick_reply_pool("tool.websearch"))
            self.assertEqual(["最终回复", ""], [response.text for response in responses[1:]])
            self.assertEqual(1, router_client.classifier_calls)
            self.assertEqual("qwen3-5-9b", router_client.classifier_model_name)
            self.assertEqual(1, main_client.tool_calls)
            self.assertEqual("websearch", main_client.last_kwargs["router_category"])
            self.assertEqual(["websearch__search"], _tool_names(main_client.last_kwargs["tools"]))
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_stream_chat_classifier_robot_task_utils_and_complex_categories(self):
        cases = [
            ("R", "robot", robot_action_phrase_pool("generic"), ["robot_remote__move_robot"], True),
            ("T", "task", quick_reply_pool("tool.task"), ["robots_task_service__create_alarm"], True),
            ("U", "utils", quick_reply_pool("tool.utils"), ["utils_remote__get_current_time"], True),
            (
                "X",
                "complex",
                None,
                [
                    "websearch__search",
                    "robot_remote__move_robot",
                    "robots_task_service__create_alarm",
                    "utils_remote__get_current_time",
                ],
                False,
            ),
        ]
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            for decision, expected_category, progress_text, expected_tool_names, expect_required in cases:
                with self.subTest(decision=decision):
                    client = _ClassifierThenToolLLMClient(decision)
                    servicer = _build_servicer(
                        client,
                        [_websearch_tool(), _robot_remote_tool(), _task_tool(), _utils_tool()],
                    )
                    request = llm_service_pb2.ChatRequest(text="帮我处理一下。", session_id=f"s-{decision}")

                    responses = _collect_stream_chat(servicer, request)

                    if progress_text:
                        self.assertIn(responses[0].text, progress_text)
                    else:
                        self.assertEqual("最终回复", responses[0].text)
                    self.assertEqual(expected_tool_names, _tool_names(client.last_kwargs["tools"]))
                    expected_followup_tool_names = [] if len(expected_tool_names) == 1 else expected_tool_names
                    self.assertEqual(expected_followup_tool_names, _tool_names(client.last_kwargs["followup_tools"]))
                    # 回退 d0236fc7 强制 tool_choice 的改动: 9B streaming 模式下
                    # forced_function / "required" 在 vLLM 端点有 bug, 会把 args
                    # 写到 content 字段。统一不传 tool_choice, 让 9B 自己选,
                    # selected_tool_name 已通过 tools 收窄到单一选项, 9B 没得选错。
                    self.assertIsNone(client.last_kwargs["tool_choice"])
                    self.assertEqual(expect_required, client.last_kwargs["require_tool_call"])
                    self.assertEqual("tool", client.last_kwargs["router_kind"])
                    self.assertEqual("llm_router", client.last_kwargs["router_source"])
                    self.assertEqual(expected_category, client.last_kwargs["router_category"])
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_stream_chat_classifier_invalid_output_falls_back_to_legacy(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            client = _ClassifierThenToolLLMClient("Z")
            servicer = _build_servicer(client, [_websearch_tool(), _robot_remote_tool()])
            request = llm_service_pb2.ChatRequest(text="帮我确认一下这件事。", session_id="s-invalid-router")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["最终回复", ""], [response.text for response in responses])
            self.assertEqual(["websearch__search", "robot_remote__move_robot"], _tool_names(client.last_kwargs["tools"]))
            self.assertEqual(1, client.classifier_calls)
            self.assertEqual(1, client.tool_calls)
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_stream_chat_classifier_category_without_matching_tool_returns_unavailable_response(self):
        """d0236fc7 + 修复: 4B 分类命中 category 但当前 Bot 无匹配工具时
        返回 unavailable_<category> 友好提示（"我现在没有可用的 xxx 工具..."），
        而不是回退 legacy 让 9B 自由生成鸡汤/无关文本。"""
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        try:
            client = _ClassifierThenToolLLMClient("W")
            servicer = _build_servicer(client, [_robot_remote_tool()])
            request = llm_service_pb2.ChatRequest(text="帮我确认一下这件事。", session_id="s-missing-category")

            responses = _collect_stream_chat(servicer, request)

            # 4B 命中 websearch，但当前 Bot 只有 robot_remote 工具，
            # 应直接返回 websearch unavailable 提示，不再调 LLM
            self.assertEqual(
                ["我现在没有可用的联网查询能力，暂时不能确认实时信息。", ""],
                [response.text for response in responses],
            )
            self.assertEqual(1, client.classifier_calls)
            # 关键：不再调 LLM（unavailable 路径直接返回固定文本）
            # 注意：last_kwargs 是上一次 stream_call_llm 的参数；这里因为
            # category=unavailable_websearch 提前 return，last_kwargs 还是上次的，
            # 可能是 None 或上一次的值。这里只断言 classifier 被调了 1 次就够了。
            self.assertEqual(0, client.tool_calls)  # 没调 9B 生成
            self.assertIsNone(client.last_kwargs)  # 整个 LLM 生成流都没进
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)

    def test_stream_chat_classifier_timeout_falls_back_to_legacy_quickly(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        old_timeout = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS"] = "1"
        try:
            client = _SlowClassifierLLMClient("W")
            servicer = _build_servicer(client, [_websearch_tool(), _robot_remote_tool()])
            request = llm_service_pb2.ChatRequest(text="帮我确认一下这件事。", session_id="s-slow-router")

            started_at = time.monotonic()
            responses = _collect_stream_chat(servicer, request)
            elapsed = time.monotonic() - started_at

            self.assertLess(elapsed, 0.04)
            self.assertEqual(["最终回复", ""], [response.text for response in responses])
            self.assertEqual(["websearch__search", "robot_remote__move_robot"], _tool_names(client.last_kwargs["tools"]))
            self.assertEqual(1, client.classifier_calls)
            self.assertEqual(1, client.tool_calls)
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS", old_timeout)

    def test_stream_chat_classifier_can_be_disabled(self):
        old_experiment = os.environ.get("LLM_TOOL_LATENCY_EXPERIMENT")
        old_classifier = os.environ.get("LLM_TOOL_ROUTER_CLASSIFIER")
        os.environ["LLM_TOOL_LATENCY_EXPERIMENT"] = "true"
        os.environ["LLM_TOOL_ROUTER_CLASSIFIER"] = "false"
        try:
            servicer = object.__new__(ImprovedLLMServiceServicer)
            servicer.llm_client = _ToolFlowLLMClient()
            servicer.session_manager = _FakeSessionManager()
            servicer.agent_registry = _FakeAgentRegistry()
            servicer.agent_tool_invoker = None
            servicer.runtime_state = _FakeRuntimeState([_websearch_tool()])

            async def _ensure_connected(mcp_manager, server_keys):
                return None

            servicer._ensure_mcp_servers_connected = _ensure_connected
            request = llm_service_pb2.ChatRequest(text="给我介绍一下你自己。", session_id="s-classifier-off")

            responses = _collect_stream_chat(servicer, request)

            self.assertEqual(["最终回复", ""], [response.text for response in responses])
            self.assertEqual(1, servicer.llm_client.tool_calls)
        finally:
            _restore_env("LLM_TOOL_LATENCY_EXPERIMENT", old_experiment)
            _restore_env("LLM_TOOL_ROUTER_CLASSIFIER", old_classifier)


def _collect_stream_chat(servicer, request):
    async def _run():
        return [
            response
            async for response in servicer.StreamChat(request, _FakeContext())
        ]

    import asyncio

    return asyncio.run(_run())


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
