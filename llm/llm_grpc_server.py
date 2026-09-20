"""
改进版 LLM gRPC 服务器

主要改进：
1. 工具调用后，LLM 会处理工具结果并回答用户问题
2. 支持会话上下文管理，实现多轮对话
3. 支持多机器人配置（Bot）
4. 完整的对话流程
"""

import grpc
import logging
import sys
import os
import asyncio
import concurrent.futures
import copy
from collections import OrderedDict
import faulthandler
import json
import re
import signal
import threading
import time
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import llm_service_pb2
from llm import llm_service_pb2_grpc
from llm import workflow_service_pb2_grpc
from llm.llm_client import (
    ImprovedQwenLLMClient,
    _authoritative_tool_result_response,
    _extract_tool_result_text,
    _terminal_exit_response_for_tool,
    _tool_result_looks_failed,
    call_llm_with_optional_controls,
    get_llm_stream_diagnostics,
)
from llm.session_manager import SessionManager
from llm.config_admin_http import ConfigAdminHTTPServer
from llm.runtime_state import LLMRuntimeState
from llm.agent_runtime import AgentContext, AgentResult
from llm.agent_tools import DefaultAgentToolInvoker
from llm.agents.cognitive import normalize_answer_by_rule
from llm.agents.registry import build_default_agent_registry
from llm.tool_router import build_fixed_exit_response
from llm.tool_router import build_tool_latency_route as _build_tool_latency_route_v2
from llm.tool_router import build_whole_session_exit_response
from llm.tool_router import detect_required_tool_category
from llm.tool_router import infer_deterministic_robot_tool_args
from llm.tool_router import infer_deterministic_singing_tool_args
from llm.tool_router import is_xiaowen_profile
from llm.tool_router import parse_llm_router_decision
from llm.tool_router import router_category_to_tool_prefix
from llm.tool_router import router_progress_text
from llm.vision_context import (
    VISION_PROMPT,
    VisionSnapshotClient,
    build_visual_user_content,
    is_visual_context_intent,
)
from llm.workflow_planner import ComplexWorkflowPlanner
from llm.workflow_protocol import tool_schemas_from_openai_tools
from llm.workflow_runtime_service import (
    StepExecutionOutcome,
    WorkflowClassification,
    WorkflowRuntimeService,
)
from server_config.repository import ConfigRepository
import config as app_config
from voice_logging import configure_logging

configure_logging("llm", force=True)
logger = logging.getLogger(__name__)


def _config_str(name: str, default: str = "") -> str:
    value = getattr(app_config, name, None)
    if value is not None:
        return str(value).strip()
    return os.getenv(name, default).strip()


def _config_int(name: str, default: int) -> int:
    value = getattr(app_config, name, None)
    if value is None:
        value = os.getenv(name, str(default))
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("配置项 %s=%r 不是有效整数，使用默认值 %s", name, value, default)
        return default


def _config_float(name: str, default: float) -> float:
    value = getattr(app_config, name, None)
    if value is None:
        value = os.getenv(name, str(default))
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("配置项 %s=%r 不是有效数字，使用默认值 %s", name, value, default)
        return default


def _config_bool(name: str, default: bool) -> bool:
    value = getattr(app_config, name, None)
    if value is None:
        value = os.getenv(name, "true" if default else "false")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


CONFIG_DATABASE_URL = _config_str("CONFIG_DATABASE_URL")
LLM_API_KEY = _config_str("LLM_API_KEY")
LLM_ADMIN_BIND_HOST = _config_str("LLM_ADMIN_BIND_HOST", "127.0.0.1") or "127.0.0.1"
LLM_BASE_URL = _config_str("LLM_BASE_URL")
LLM_GRPC_BIND_HOST = _config_str("LLM_GRPC_BIND_HOST", "127.0.0.1") or "127.0.0.1"
LLM_ROUTER_API_KEY = _config_str("LLM_ROUTER_API_KEY")
LLM_ROUTER_BASE_URL = _config_str("LLM_ROUTER_BASE_URL")
LLM_ROUTER_MODEL_NAME = _config_str("LLM_ROUTER_MODEL_NAME")
LLM_ADMIN_PORT = _config_int("LLM_ADMIN_PORT", 18053)
LLM_GRPC_SERVER_PORT = _config_int("LLM_GRPC_SERVER_PORT", 50053)
LLM_GRPC_MAX_WORKERS = _config_int("LLM_GRPC_MAX_WORKERS", 10)
LLM_HTTP_TIMEOUT_SEC = max(1.0, _config_float("LLM_HTTP_TIMEOUT_SEC", 60.0))
LLM_ROUTER_HTTP_TIMEOUT_SEC = max(1.0, _config_float("LLM_ROUTER_HTTP_TIMEOUT_SEC", 8.0))
LLM_VISION_ENABLED = _config_bool("LLM_VISION_ENABLED", True)
LLM_VISION_GATEWAY_BASE_URL = _config_str(
    "LLM_VISION_GATEWAY_BASE_URL",
    "http://127.0.0.1:8282",
)
LLM_VISION_GATEWAY_TOKEN = _config_str("LLM_VISION_GATEWAY_TOKEN")
LLM_VISION_FETCH_TIMEOUT_SEC = max(
    0.1,
    _config_float("LLM_VISION_FETCH_TIMEOUT_SEC", 1.0),
)
LLM_MAX_TOOL_ROUNDS = max(1, _config_int("LLM_MAX_TOOL_ROUNDS", 5))
MCP_ENABLED = _config_bool("MCP_ENABLED", True)
LLM_COMPLEX_WORKFLOW_ENABLED = _config_bool("LLM_COMPLEX_WORKFLOW_ENABLED", False)
is_missing_secret = getattr(
    app_config,
    "is_missing_secret",
    lambda value: not (value or "").strip(),
)

_AUDIO_CONTEXT_PREFIX = "[语音上下文:"
_AUDIO_CONTEXT_USER_MARKER = "\n用户说："
_RUNTIME_TIMEZONE = "Asia/Shanghai"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_ROUTER_CLASSIFIER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="tool-router",
)
_WEBSEARCH_RESULT_LIMIT = 3
_WEBSEARCH_TOOL_TIMEOUT_SEC = 8.0
_WEBSEARCH_TOOL_MAX_ATTEMPTS = 2
_WEBSEARCH_RESULT_LIMIT_ARG_NAMES = (
    "count",
    "limit",
    "top_k",
    "topK",
    "page_size",
    "pageSize",
    "num_results",
    "numResults",
    "max_results",
    "maxResults",
)


@dataclass(frozen=True)
class ToolLatencyRoute:
    kind: str
    progress_text: str | None = None
    tool_prefix: str | None = None
    selected_tool_name: str | None = None
    require_tool_call: bool = False
    model_text: str | None = None
    source: str | None = None
    category: str | None = None


_STANDALONE_TOOL_TAG_RE = re.compile(
    r"(?m)^[ \t]*\[(?:robot_remote|websearch|utils_remote|robots_task_service)[^\]\r\n]*\][ \t]*(?:\r?\n)?"
)


def _strip_standalone_tool_tags(text: str) -> str:
    if not text:
        return ""
    return _STANDALONE_TOOL_TAG_RE.sub("", text)


def _elapsed_monotonic_ms(started_at: float | None) -> float:
    if not started_at:
        return 0.0
    return round((time.monotonic() - started_at) * 1000.0, 1)


def _safe_metric_value(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return round(float(value), 1)
    return str(value)


def _metrics_json(metrics: dict) -> str:
    safe_metrics = {
        key: _safe_metric_value(value)
        for key, value in metrics.items()
        if value is not None
    }
    return json.dumps(safe_metrics, ensure_ascii=False, separators=(",", ":"))


def _runtime_tzinfo():
    try:
        return ZoneInfo(_RUNTIME_TIMEZONE)
    except Exception:
        return timezone(timedelta(hours=8), name=_RUNTIME_TIMEZONE)


def _runtime_now() -> datetime:
    return datetime.now(_runtime_tzinfo())


def _format_runtime_now(dt: datetime) -> str:
    weekday_cn = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    return f"{dt.strftime('%Y年%m月%d日 %H:%M:%S')} {weekday_cn[dt.weekday()]} {_RUNTIME_TIMEZONE}"


def _runtime_lunar_date(dt: datetime) -> str:
    """返回当前日期对应的农历；失败时不阻断主对话。"""
    try:
        from zhdate import ZhDate

        local_dt = dt.replace(tzinfo=None) if dt.tzinfo else dt
        return ZhDate.from_datetime(local_dt).chinese()
    except Exception as exc:
        logger.warning("当前农历计算失败: %s", exc)
        return ""


async def _iter_llm_client_stream(
    llm_client,
    messages,
    tools,
    temperature,
    max_tokens,
    model_name,
    *,
    tool_choice=None,
):
    stream_call_llm = getattr(llm_client, "stream_call_llm", None)
    if callable(stream_call_llm):
        async for chunk in stream_call_llm(
            messages,
            tools,
            temperature,
            max_tokens,
            model_name,
            tool_choice=tool_choice,
        ):
            yield chunk
        return

    for chunk in llm_client._call_llm(
        messages,
        tools,
        temperature,
        max_tokens,
        model_name,
        tool_choice=tool_choice,
    ):
        yield chunk


def _build_runtime_context_message(now: datetime | None = None) -> dict[str, str]:
    current = now or _runtime_now()
    lunar_text = _runtime_lunar_date(current)
    lunar_context = f"当前农历是：{lunar_text}。" if lunar_text else ""
    return {
        "role": "system",
        "content": (
            f"当前系统时间是：{_format_runtime_now(current)}。"
            f"{lunar_context}"
            "处理提醒、闹钟、定时任务、相对日期和相对时间时，必须以这个时间为准。"
            "回答农历、阴历、生肖、干支相关问题时，必须以当前农历为准，不要自行推算。"
            "如果需要生成具体提醒时间，必须生成未来时间；禁止编造已经过去的日期时间。"
            "如果时间信息不明确，请先调用时间工具或向用户确认。"
        ),
    }


def _split_audio_context_text(text: str) -> tuple[str, str | None]:
    """拆分 Gateway 拼接的语音上下文，返回 (干净用户文本, 上下文描述)。"""
    value = str(text or "")
    if not value.startswith(_AUDIO_CONTEXT_PREFIX) or _AUDIO_CONTEXT_USER_MARKER not in value:
        return value, None

    context_part, clean_text = value.split(_AUDIO_CONTEXT_USER_MARKER, 1)
    context = context_part.removeprefix(_AUDIO_CONTEXT_PREFIX).removesuffix("]").strip()
    return clean_text.strip(), context or None


def _replace_latest_user_message(messages: list[dict], clean_text: str, model_text: str) -> list[dict]:
    """仅本轮发给模型时使用语音上下文，持久历史仍保留 clean_text。"""
    patched = copy.deepcopy(messages)
    for message in reversed(patched):
        if message.get("role") == "user" and message.get("content") == clean_text:
            message["content"] = model_text
            break
    return patched


def _agent_catalog_payload(agent) -> dict:
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "description": getattr(agent, "description", "") or "",
        "module": agent.__class__.__module__,
        "class_name": agent.__class__.__name__,
        "enabled_by_default": bool(getattr(agent, "enabled_by_default", True)),
        "trigger_examples_json": list(getattr(agent, "trigger_examples", ()) or ()),
        "allowed_tools_json": list(getattr(agent, "allowed_tools", ()) or ()),
        "tool_groups_json": list(getattr(agent, "tool_groups", ()) or ()),
    }


def _tool_name_matches(tool_name: str, suffix: str) -> bool:
    """兼容内部真实名 server.tool 与 LLM 安全名 server__tool。"""
    return (tool_name or "").endswith(suffix) or (tool_name or "").endswith(suffix.replace(".", "__"))


def _is_websearch_tool_name(tool_name: str) -> bool:
    return "websearch" in (tool_name or "").lower()


def _is_transient_websearch_error(error: Exception) -> bool:
    if isinstance(
        error,
        (
            asyncio.TimeoutError,
            TimeoutError,
            ConnectionError,
            OSError,
            grpc.RpcError,
        ),
    ):
        return True

    message = str(error).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "超时",
        "connection",
        "连接",
        "temporarily unavailable",
        "temporary failure",
        "session closed",
        "channel closed",
        "broken pipe",
        "reset by peer",
        "429",
        "502",
        "503",
        "504",
    )
    return any(marker in message for marker in transient_markers)


def _tool_name_equal(tool_name: str, candidate: str) -> bool:
    safe_tool = tool_name or ""
    safe_candidate = candidate or ""
    return safe_tool == safe_candidate or safe_tool.replace("__", ".") == safe_candidate.replace("__", ".")


def _forced_tool_choice(tool_name: str) -> dict[str, Any]:
    """保留函数签名以兼容旧测试/调用点，但实际不再使用——
    9B streaming 模式下 forced_function 失效 (vLLM 端点 bug: 把 args 写到
    content 字段)，统一回退到 round_tool_choice=None 让 9B 自己选工具。"""
    return {
        "type": "function",
        "function": {"name": str(tool_name)},
    }


def _find_llm_tool_schema(tools: list[dict] | None, tool_name: str) -> dict | None:
    for tool in tools or []:
        name = tool.get("function", {}).get("name", "")
        if _tool_name_equal(name, tool_name):
            return tool
    return None


def _tool_parameter_names(tool_schema: dict | None) -> set[str]:
    parameters = ((tool_schema or {}).get("function") or {}).get("parameters") or {}
    properties = parameters.get("properties") or {}
    if isinstance(properties, dict):
        return set(properties.keys())
    return set()


def _limit_websearch_args(tool_name: str, tool_schema: dict | None, tool_args: dict) -> dict:
    if not _is_websearch_tool_name(tool_name):
        return tool_args

    limited_args = dict(tool_args or {})
    parameter_names = _tool_parameter_names(tool_schema)
    supported_limit_names = [
        name
        for name in _WEBSEARCH_RESULT_LIMIT_ARG_NAMES
        if name in parameter_names or name in limited_args
    ]

    if not supported_limit_names:
        return limited_args

    if not any(name in limited_args for name in supported_limit_names):
        limited_args[supported_limit_names[0]] = _WEBSEARCH_RESULT_LIMIT
        return limited_args

    for name in supported_limit_names:
        if name not in limited_args:
            continue
        try:
            current_value = int(limited_args[name])
        except (TypeError, ValueError):
            limited_args[name] = _WEBSEARCH_RESULT_LIMIT
            continue
        if current_value <= 0 or current_value > _WEBSEARCH_RESULT_LIMIT:
            limited_args[name] = _WEBSEARCH_RESULT_LIMIT
    return limited_args


def _build_tool_failure_message(tool_name: str) -> str:
    if _tool_name_matches(tool_name, ".move_robot"):
        return "我这边暂时没能完成这个动作，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".create_map"):
        return "我这边暂时没能开始创建地图，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".patrol"):
        return "我这边暂时没能开始巡检，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".follow"):
        return "我这边暂时没能开始追随，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".recharge_robot") or _tool_name_matches(tool_name, ".return_to_charge"):
        return "我这边暂时没能开始回桩充电，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".dance"):
        return "我这边暂时没能开始跳舞，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".play_song"):
        return "我这边暂时没能开始唱歌，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".call_video"):
        return "我这边暂时没能呼叫视频电话，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".detect_gesture"):
        return "我这边暂时没能开始手势检测，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".detect_pet"):
        return "我这边暂时没能开始宠物检测，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".understand_environment"):
        return "我这边暂时没能开始环境理解，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".cancel_robot_task"):
        return "我这边暂时没能取消当前任务，你稍后再让我试一次吧。"
    if _tool_name_matches(tool_name, ".get_weather"):
        return "我这边暂时没查到天气，你稍后再问我一次吧。"
    if _tool_name_matches(tool_name, ".get_now_context"):
        return "我这边暂时没拿到当前时间信息，你稍后再问我一次吧。"
    if _tool_name_matches(tool_name, ".format_timestamp"):
        return "我这边暂时没换算成功这个时间，你稍后再试一次吧。"
    if "websearch" in (tool_name or "").lower():
        return "我这边暂时没查到你要的信息，你稍后再问我一次吧。"
    return "我这边暂时没处理成功，你稍后再试一次吧。"


def _unavailable_tool_category_response(category: str | None) -> str:
    safe_category = str(category or "").removeprefix("unavailable_")
    if safe_category == "websearch":
        return "我现在没有可用的联网查询能力，暂时不能确认实时信息。"
    if safe_category == "utils":
        return "我现在没有可用的时间查询能力，暂时不能确认准确时间。"
    if safe_category == "task":
        return "我现在没有可用的任务工具，暂时不能创建或修改提醒。"
    if safe_category == "robot":
        return "我现在没有可用的机器人控制工具，暂时不能执行这个操作。"
    if safe_category == "robot_capability":
        return "我目前只支持移动、巡逻、回桩、跳舞、互动和检测等固定动作，暂时不能控制家电或灯光。"
    return "我现在缺少完成这个请求所需的工具，你可以换一种方式问我。"


def _direct_robot_result_to_speech(tool_name: str, result_text: str) -> str:
    if not result_text:
        return ""

    failure_message = _build_tool_failure_message(tool_name)
    if (
        result_text == failure_message
        or result_text.startswith("我这边暂时")
        or result_text.startswith("工具未执行")
    ):
        return result_text

    if any(keyword in result_text for keyword in ("失败", "错误", "无法", "未能", "超时")):
        return failure_message

    return ""


ROBOT_TOOL_SUFFIXES = (
    ".move_robot",
    ".create_map",
    ".patrol",
    ".follow",
    ".recharge_robot",
    ".return_to_charge",
    ".dance",
    ".call_video",
    ".detect_gesture",
    ".detect_pet",
    ".understand_environment",
    ".cancel_robot_task",
)


def _is_tool_latency_experiment_enabled() -> bool:
    return os.getenv("LLM_TOOL_LATENCY_EXPERIMENT", "true").strip().lower() in _TRUE_ENV_VALUES


def _is_tool_router_classifier_enabled() -> bool:
    return os.getenv("LLM_TOOL_ROUTER_CLASSIFIER", "true").strip().lower() in _TRUE_ENV_VALUES


TASK_TOOL_QUERY_KEYWORDS = (
    "查询",
    "查",
    "查看",
    "列出",
    "列表",
)

TASK_TOOL_CREATE_KEYWORDS = (
    "提醒我",
    "添加",
    "创建",
    "设置",
    "新增",
    "记一条",
    "记一下",
    "安排",
)

_TASK_TOOL_CREATE_SUFFIXES = (
    ".create_reminder",
    ".create_task",
    ".create_alarm",
    ".add_reminder",
    ".add_task",
    ".add_alarm",
    ".set_reminder",
    ".set_task",
    ".set_alarm",
)

_TASK_TOOL_LIST_SUFFIXES = (
    ".list_reminders",
    ".list_reminder",
    ".list_task",
    ".list_tasks",
    ".query_reminder",
    ".query_task",
    ".query_tasks",
    ".get_reminder",
    ".get_task",
)


def _infer_task_tool_name_from_text(text: str, tools: list[dict] | None) -> str | None:
    normalized = (text or "").lower()
    create_tools = [
        tool.get("function", {}).get("name", "")
        for tool in tools or []
        if any(_tool_name_matches(tool.get("function", {}).get("name", ""), suffix) for suffix in _TASK_TOOL_CREATE_SUFFIXES)
    ]
    list_tools = [
        tool.get("function", {}).get("name", "")
        for tool in tools or []
        if any(_tool_name_matches(tool.get("function", {}).get("name", ""), suffix) for suffix in _TASK_TOOL_LIST_SUFFIXES)
    ]
    has_query_intent = any(keyword in normalized for keyword in TASK_TOOL_QUERY_KEYWORDS)
    has_create_intent = any(keyword in normalized for keyword in TASK_TOOL_CREATE_KEYWORDS)

    if has_query_intent and list_tools:
        return list_tools[0]
    if has_create_intent and create_tools:
        return create_tools[0]
    return None


def _tool_router_classifier_timeout_ms() -> int:
    raw_value = os.getenv("LLM_TOOL_ROUTER_CLASSIFIER_TIMEOUT_MS", "500").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 500


def _tool_router_classifier_max_tokens() -> int:
    raw_value = os.getenv("LLM_TOOL_ROUTER_CLASSIFIER_MAX_TOKENS", "1").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 1


def _tool_router_classifier_history_messages() -> int:
    raw_value = os.getenv("LLM_TOOL_ROUTER_CLASSIFIER_HISTORY_MESSAGES", "10").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 10


def _tool_router_classifier_history_message_chars() -> int:
    raw_value = os.getenv("LLM_TOOL_ROUTER_CLASSIFIER_HISTORY_MESSAGE_CHARS", "200").strip()
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 200


def _tool_router_classifier_history_char_budget() -> int:
    raw_value = os.getenv("LLM_TOOL_ROUTER_CLASSIFIER_HISTORY_CHAR_BUDGET", "700").strip()
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 700


def _tool_router_classifier_fallback_history_messages() -> int:
    raw_value = os.getenv("LLM_TOOL_ROUTER_CLASSIFIER_FALLBACK_HISTORY_MESSAGES", "3").strip()
    try:
        return max(1, int(raw_value))
    except ValueError:
        return 3


def _tool_router_classifier_prompt_char_limit() -> int:
    # 4B 模型 max=4096 tokens, 1 token 约 0.5-2 chars (中英混合), 4096 tokens ≈ 8000-16000 chars
    # 留 2x 安全余量: 默认 8192 chars (≈ 2000-4000 tokens, 不会撞模型硬限)
    # 仍走 _trim_tool_router_classifier_history 裁剪 + 超限 fallback 3 条历史保护
    raw_value = os.getenv("LLM_TOOL_ROUTER_CLASSIFIER_PROMPT_CHAR_LIMIT", "8192").strip()
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 8192


def _is_router_context_length_error(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "maximum context length" in message
        or "input_tokens" in message
        or "context length" in message
    )


def _trim_tool_router_classifier_history(history: list[dict]) -> list[dict]:
    """按上下文预算裁剪分类器历史上下文，优先保留最新消息。"""

    if not history:
        return []

    max_messages = _tool_router_classifier_history_messages()
    if max_messages <= 0:
        return []
    trimmed_history = [message.copy() for message in history[-max_messages:]]

    message_char_limit = _tool_router_classifier_history_message_chars()
    if message_char_limit > 0:
        for message in trimmed_history:
            content = message.get("content")
            if isinstance(content, str) and len(content) > message_char_limit:
                message["content"] = content[:message_char_limit]

    history_budget = _tool_router_classifier_history_char_budget()
    if history_budget <= 0:
        return trimmed_history

    while trimmed_history:
        total_chars = sum(len(msg.get("content", "") or "") for msg in trimmed_history)
        if total_chars <= history_budget:
            break
        if len(trimmed_history) == 1:
            content = (trimmed_history[0].get("content", "") or "")
            if len(content) <= history_budget:
                break
            trimmed_history[0]["content"] = content[:history_budget]
            break
        trimmed_history = trimmed_history[1:]

    return trimmed_history


def _build_tool_router_classifier_messages_for_fallback(
    conversation_messages: list[dict] | None,
) -> list[dict] | None:
    if not conversation_messages:
        return None
    return conversation_messages[
        -_tool_router_classifier_fallback_history_messages() :
    ]


def _build_tool_router_classifier_messages(
    text: str,
    conversation_messages: list[dict] | None = None,
) -> list[dict[str, str]]:
    system_prompt = (
        "你是语音助手的工具路由分类器。只允许输出一个单字符白名单代码："
        "E、C、V、W、R、S、T、U、X。"
        "不要解释，不要输出白名单代码之外的任何文字。"
        "判断用户的主意图，不要只看关键词。"
        "代码含义：E=exit，C=chat，V=vision，W=websearch，R=robot，S=singing，T=task，U=utils，X=complex。"
        "默认倾向：视觉类问题必须输出 V；工具类问题按可用工具类别输出；不确定其它场景时输出 C 保护体验。"
        "E 用于用户明确结束整个对话，如再见、退下吧、我不聊了你退出吧；"
        "退出这个话题、换个话题、退出小游戏不是 E，应输出 C。"
        "'取消一切'没有说明取消对象，既不是明确退出会话，也不能擅自取消机器人任务，应输出 C；"
        "C 用于普通聊天、能力问题、解释概念、创作故事和情绪陪伴；当用户没有视觉意图时输出 C。"
        "可用工具类别："
        "V 用于基于当前画面回答视觉问题（看到什么、穿的怎么样、桌上有什么、画面里是谁）；"
        "用户包含'看到/看到画面/看到画面里/看到桌上/画面里有什么/穿这个颜色/穿这身'等"
        "描述、识别或询问当前摄像头画面时必须输出 V（不能因为像聊天就输出 C）；"
        "V 是 chat 路径，会拉取最新摄像头画面让 LLM 描述；"
        "V 必须是描述/识别/询问当前摄像头实时画面，不包括画画、画图、想象场景、'用文字画'这类创作；"
        "询问画面中某个具体物体的可见状态（如门是否关好、灯是否亮着）属于 V，不是环境理解工具；"
        "V 的判定必须有具体描述/询问对象，'帮我看看'/'你看一下'/'look 一下'等无具体对象"
        "的'看看/看一下/look'不是 V，应输出 C（用户没说看什么时按 chat 处理）"
        "；"
        "'我能看到你'/'看到天气很好'等陈述性'看到'不是视觉问题，输出 C；"
        "例外：如果用户消息以'请根据以下环境主动回应'、'请根据环境主动回应'等"
        "客户端固定引导语开头，消息内容是预先描述的环境（穿什么/坐什么），"
        "不是用户问视觉问题，必须输出 C；"
        "E 用于结束整个对话的明确指令，仅'再见/退下/结束对话/我不聊了/你先休息吧'"
        "'先挂了/算了不聊了/下次再聊/我们下次再聊/先到这里吧回头再聊'等"
        "明确告别才算 E；'换个话题/不想聊这个了/退小游戏/退出故事/我不想说话'不是 E，应输出 C；"
        "W 用于天气、新闻、价格、赛事、实时信息和联网搜索；"
        "W 必须有明确搜索目标，'搜一下'/'百度一下'/'有什么好玩的'等无具体对象"
        "不是 W，应输出 C（用户没说搜什么时按 chat 处理）"
        "；"
        "R 用于移动、停止、跳舞、巡检、回桩、呼叫视频电话和纯机器人动作指令；"
        "如果用户的指令是让机器人做当前已定义的固定动作（往前/往后退/左转/右转/停/建图/创建地图/回桩/充电/开始巡逻/追随/跳舞/打个招呼/握手/喝彩/播个视频电话），"
        "即使表面像'创建任务'或'操作'，只要目标是机器人本体，输出 R；"
        "当前没有空调、灯光、电视或窗帘控制工具，这些请求不能输出 R，应输出 C；"
        "机器人只能执行工具中定义的固定动作，没有舞种、移动距离、转向角度、音量或视频联系人参数；"
        "出现街舞/芭蕾/拉丁舞等舞种词，或米/步/度/圈等动作参数单位时都输出 C；"
        "用户指定这些不支持的参数时输出 C，先说明限制或澄清，不能擅自执行相近固定动作；"
        "用户用'不要/不用/先别'否定一个尚未开始的机器人动作时输出 C；"
        "其他口语否定前缀（如'别给我做某动作'、上一轮提议后回答先不执行）同样输出 C；"
        "'别跳了/停下'表示停止正在执行的动作，才输出 R；"
        "只有单个含糊动作词、没有方向/对象/历史上下文时输出 C，不能猜测为前进、回桩或取消；"
        "尤其是脱离上下文的单字动词不能触发物理动作；"
        "R 还用于基于当前画面的检测类工具：手势检测/宠物检测/环境识别/理解环境/看看周围"
        "都属于 .detect_gesture / .detect_pet / .understand_environment 工具，必须输出 R（不是 V），"
        "因为 9b 调这些工具能返回结构化结果（位置/种类/场景描述），比单纯描述画面更精准；"
        "S 用于 AI 点歌和查询当前音色会唱什么歌；唱首歌、随便唱一首、会唱什么歌、会唱某首歌吗都输出 S；"
        "唱歌不属于机器人 MQTT 动作；你唱歌好听吗等主观聊天问题输出 C；"
        "T 用于提醒、闹钟、定时任务和任务列表；T 是给'我'创建闹钟或待办，不是让机器人做事。"
        "创建提醒必须同时给出提醒内容和触发时间；缺少任一项时输出 C 先澄清，不能强制调用 T；"
        "U 用于当前时间、日期、星期、农历、节假日和时间戳换算；普通数字计算输出 C；"
        "询问用户自己的生肖、年龄等个人信息需要用户资料，不属于当前时间工具，应输出 C；"
        "假设某种天气会怎样、天气故事或天气原理不是查询真实天气，应输出 C；"
        "X 用于需要先查询再判断、条件触发、多工具或多步骤编排的请求。"
        "只要一个条件的结果决定后续动作，就必须输出 X；即使前后两个动作都属于 robot，也不能只按第一个动作输出 R。"
        "同一句中需要调用两个或以上工具时必须输出 X，不能因先看到某个动作词就降级为单工具类别。"
        "同一工具需要针对两个目标调用两次也属于 X，例如依次查询两个城市；"
        "视觉观察后再创建提醒也是 X，不能只输出 V；"
        "多轮短回复必须结合历史：上一轮只是询问是否执行时，拒绝/先不做输出 C；"
        "历史明确说明机器人动作正在执行时，用户要求继续或再执行一次才输出 R；"
        "例子：讲一个青岛下雨的故事 -> C；"
        "咱们安全好 -> C；"
        "你会跳舞吗 -> C；"
        "解释一下黄金价格为什么波动 -> C；"
        "给我画一只小猫 -> C（创作类，不是描述当前画面）"
        "；"
        "你能 give me a hug 吗 -> C（能力问句）"
        "；"
        "嗨，你好呀 -> C（用户用嗨/你好跟助手语音打招呼）"
        "；"
        "不想聊这个了 -> C（换话题，不是结束会话）"
        "；"
        "我不想说话 -> C（表达情绪，不是结束会话）"
        "；"
        "你看到了什么 -> V；"
        "你现在看到了什么 -> V；"
        "我能看到你 -> C（陈述，不是问画面）"
        "；"
        "看到天气很好 -> C（陈述，不是问画面）"
        "；"
        "我穿这个颜色合适吗 -> V；"
        "画面里有什么 -> V；"
        # 客户端固定开头的环境描述：不是用户问视觉问题，是上下文消息，应走 C
        "请根据以下环境主动回应：穿浅色短袖T恤 -> C；"
        "请根据以下环境主动回应：身穿浅绿T恤 -> C；"
        "你往前走一下 -> R；"
        "往后退一步 -> R；"
        "开始创建地图 -> R；"
        "开始建图 -> R；"
        "开始巡逻 -> R；"
        "回桩充电 -> R；"
        "就是现在，跳舞 -> R；"
        "搞段舞蹈 -> R（口语化固定舞蹈动作）"
        "；"
        "和我握个手 -> R；"
        "和我打个招呼 -> R；"
        "喝个彩 -> R；"
        "唱首歌 -> S；"
        "唱首爱你 -> S；"
        "你会唱什么歌 -> S；"
        "你会唱爱你吗 -> S；"
        "看看宠物在哪 -> R；"
        "检测一下手势 -> R；"
        "环境识别一下 -> R；"
        "看看周围有什么 -> R；"
        "取消当前任务 -> R；"
        "呼叫视频电话 -> R；"
        "把音乐关掉 -> R（停止当前机器人音乐任务）"
        "；"
        "不用跳舞 -> C（否定尚未开始的动作）"
        "；"
        "跳一段街舞 -> C（舞种参数不受支持）"
        "；"
        "来段拉丁舞 -> C（舞种参数不受支持）"
        "；"
        "往前走五米 -> C（距离参数不受支持）"
        "；"
        "右转一百八十度 -> C（角度参数不受支持）"
        "；"
        "给妈妈打视频 -> C（联系人参数不受支持）"
        "；"
        "先别发起视频通话 -> C（否定动作，不能调用工具）"
        "；"
        "别给我唱歌 -> C（否定尚未开始的动作）"
        "；"
        "单说'过去'且没有方向或上下文 -> C（不能猜测机器人动作）"
        "；"
        "单说'返'且没有上下文 -> C（不能猜测为回桩）"
        "；"
        "看看身后的门有没有关 -> V（观察具体物体状态）"
        "；"
        "把空调打开 -> C（当前没有家电控制工具）"
        "；"
        "关空调 -> C（当前没有家电控制工具）"
        "；"
        "把灯打开 -> C（当前没有灯光控制工具）"
        "；"
        "调亮一点 -> C（当前没有灯光控制工具）"
        "；"
        "明早八点提醒我开会 -> T；"
        "提醒我喝水 -> C（缺少触发时间，应先澄清）"
        "；"
        "明天提醒我 -> C（缺少提醒内容，应先澄清）"
        "；"
        "删除我刚才的提醒 -> T；"
        "把刚才的提醒取消 -> T；"
        "今天星期几 -> U；"
        "What time is it in Tokyo? -> U（跨时区当前时间仍由 Utils 查询）"
        "；"
        "帮我算下 100 加 200 -> C（普通计算不需要工具）"
        "；"
        "青岛现在下雨吗 -> W；"
        "明天的会议几点开始 -> W（会议时间不在本地任务表里，要联网查）"
        "；"
        "青岛有哪些好玩的地方 -> W（实时旅游信息）"
        "；"
        "有什么好玩的 -> C（无具体对象，不是搜索）"
        "；"
        "搜一下 -> C（无具体对象，不是搜索）"
        "；"
        "百度一下 -> C（无具体对象，不是搜索）"
        "；"
        "如果青岛下雨就提醒我带伞 -> X；"
        "what's the time and weather -> X；"
        "查 weather and time please -> X；"
        "看看穿得是否合适，如果不合适就提醒换衣服 -> X；"
        "查一下今天的天气，再看看我这身衣服合不合适 -> X；"
        "先向前走再向右转 -> X；"
        "先检测有没有宠物，有的话再播放轻音乐 -> X；"
        "先往前走再右转，然后查北京和青岛天气 -> X；"
        "查广州天气再查深圳天气 -> X（同一搜索工具需要调用两次）"
        "；"
        "帮我看看 -> C（无具体对象，不是视觉）"
        "；"
        "帮我看看这个 -> V（有具体对象，描述画面）"
        "；"
        "你看一下 -> C（无具体对象，不是视觉）"
        "；"
        "陪我走一段 -> R（让机器人跟随行走）"
        "；"
        "别动 -> R（让机器人停下当前动作）"
        "；"
        "别跳了 -> R（让机器人停止跳舞）"
        "；"
        "嗨，你好呀 -> C（用户在跟助手语音打招呼）"
        "；"
        "和我打个招呼 -> R（让机器人主动挥手招呼）"
        "；"
        "你能 give me a hug 吗 -> C（能力问句，不是真让机器人抱）"
        "；"
        "取消 -> R（取消机器人当前动作）"
        "；"
        "它在哪 -> C（无上下文代词，普通 chat）"
        "；"
        "先到这里吧，回头再聊 -> E（明确告别）"
        "；"
        "我们下次再聊 -> E（明确告别）"
        "；"
        "上一轮只问是否移动，用户回答先算了 -> C（拒绝尚未执行的动作）"
        "；"
        "历史说明机器人正在巡逻，用户回答接着来 -> R（继续当前机器人动作）"
        "；"
        "cya -> E（英文口语告别）"
        "；"
        "我不聊了，你退出吧 -> E。"
    )

    history: list[dict[str, str]] = []
    for message in conversation_messages or []:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        history.append({"role": role, "content": content})

    # 当前用户消息由 text 作为最后一条普通 user 消息传入，避免重复或带标签包装。
    if history and history[-1]["role"] == "user":
        history.pop()
    history = _trim_tool_router_classifier_history(history)

    if not history:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text or ""},
        ]

    system_prompt += (
        "输入中包含最近对话。只分类最后一条用户消息；"
        "最新消息意图明确时按最新消息分类，历史不得覆盖；"
        "最新消息存在省略、指代、确认、继续、修改或取消时，结合历史还原完整意图。"
    )
    older_history = history[:-2]
    recent_history = history[-2:]
    if older_history:
        system_prompt += "\n较早对话背景：\n" + "\n".join(
            f"{'用户' if message['role'] == 'user' else '助手'}：{message['content']}"
            for message in older_history
        )

    messages = [
        {"role": "system", "content": system_prompt},
        *recent_history,
        {"role": "user", "content": text or ""},
    ]

    total_chars = sum(len(msg.get("content", "") or "") for msg in messages)
    prompt_char_limit = _tool_router_classifier_prompt_char_limit()
    if prompt_char_limit > 0 and total_chars > prompt_char_limit:
        logger.debug(
            "工具 Router 分类器构建 prompt 超过预估字符上限，降级使用更少历史重建：chars=%s limit=%s",
            total_chars,
            prompt_char_limit,
        )
        compact_history = _build_tool_router_classifier_messages_for_fallback(conversation_messages)
        if compact_history != conversation_messages:
            return _build_tool_router_classifier_messages(text, compact_history)

    return messages


def _collect_tool_router_classifier_text(
    llm_client,
    text: str,
    model_name: str,
    max_tokens: int,
    conversation_messages: list[dict] | None = None,
    stop_event: threading.Event | None = None,
    active_response: dict | None = None,
    active_response_lock=None,
) -> str:
    classifier_text = ""
    active_response = active_response if active_response is not None else {"response": None}
    active_response_lock = active_response_lock if active_response_lock is not None else threading.Lock()
    chunks = None

    def remember_response(response) -> None:
        with active_response_lock:
            active_response["response"] = response

    try:
        chunks = call_llm_with_optional_controls(
            llm_client,
            messages=_build_tool_router_classifier_messages(text, conversation_messages),
            tools=None,
            temperature=0.0,
            max_tokens=max_tokens,
            model_name=model_name,
            stop_event=stop_event,
            on_response=remember_response,
        )
        for chunk in chunks:
            if stop_event is not None and stop_event.is_set():
                break
            if chunk.get("type") != "text":
                continue
            classifier_text += chunk.get("content", "")
            if parse_llm_router_decision(classifier_text):
                break
    finally:
        close_chunks = getattr(chunks, "close", None)
        if callable(close_chunks):
            try:
                close_chunks()
            except Exception as exc:
                logger.debug("关闭 Router 分类器生成器失败: %s", exc)
        _close_router_classifier_response(
            active_response,
            active_response_lock,
            "classifier_finished",
        )
    return classifier_text


def _close_router_classifier_response(
    active_response: dict,
    active_response_lock,
    reason: str,
) -> None:
    with active_response_lock:
        response = active_response.get("response")
        if response is None or active_response.get("closed"):
            return
        active_response["closed"] = True
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
            if reason == "classifier_timeout":
                logger.info("Router 分类器 LLM 流已关闭: reason=%s", reason)
            else:
                logger.debug("Router 分类器 LLM 流已关闭: reason=%s", reason)
        except Exception as exc:
            logger.debug("关闭 Router 分类器 LLM 流失败: reason=%s error=%s", reason, exc)


def _classify_legacy_tool_latency_route(
    llm_client,
    text: str,
    model_name: str,
    tools: list[dict] | None = None,
    conversation_messages: list[dict] | None = None,
    allow_unavailable_tool_category: bool = False,
    _retried: bool = False,
    session_id: str | None = None,
) -> ToolLatencyRoute:
    started_at = time.monotonic()
    timeout_ms = _tool_router_classifier_timeout_ms()
    max_tokens = _tool_router_classifier_max_tokens()
    stop_event = threading.Event()
    active_response: dict = {"response": None}
    active_response_lock = threading.Lock()
    future = _ROUTER_CLASSIFIER_EXECUTOR.submit(
        _collect_tool_router_classifier_text,
        llm_client,
        text,
        model_name,
        max_tokens,
        conversation_messages,
        stop_event,
        active_response,
        active_response_lock,
    )
    try:
        classifier_text = future.result(timeout=timeout_ms / 1000.0)
    except concurrent.futures.TimeoutError:
        logger.warning("工具 Router 分类器超时 %.0fms，回退 legacy", timeout_ms)
        stop_event.set()
        _close_router_classifier_response(
            active_response,
            active_response_lock,
            "classifier_timeout",
        )
        future.cancel()
        return ToolLatencyRoute("legacy")
    except Exception as exc:
        if (
            not _retried
            and conversation_messages
            and _is_router_context_length_error(exc)
        ):
            logger.warning(
                "工具 Router 分类器上下文超限，尝试仅保留最近 %s 条历史重试",
                _tool_router_classifier_fallback_history_messages(),
            )
            return _classify_legacy_tool_latency_route(
                llm_client,
                text,
                model_name,
                tools,
                _build_tool_router_classifier_messages_for_fallback(conversation_messages),
                allow_unavailable_tool_category,
                True,
                session_id,
            )
        logger.warning("工具 Router 分类器失败，回退 legacy: %s", exc)
        return ToolLatencyRoute("legacy")

    elapsed_ms = (time.monotonic() - started_at) * 1000.0
    decision = parse_llm_router_decision(classifier_text)
    decision_label = "legacy"
    if decision:
        kind, category = decision
        decision_label = kind if not category else f"{kind}:{category}"
    logger.info(
        "工具 Router 分类器完成: decision=%s elapsed=%.1fms model=%s max_tokens=%s raw=%r",
        decision_label,
        elapsed_ms,
        model_name,
        max_tokens,
        classifier_text[:20],
    )
    if not decision:
        return ToolLatencyRoute("legacy")
    kind, category = decision
    if kind == "exit":
        return ToolLatencyRoute("exit", source="llm_router")
    if kind == "chat":
        return ToolLatencyRoute("chat", source="llm_router")
    if kind == "tool":
        tool_prefix = router_category_to_tool_prefix(category)
        if (
            tool_prefix
            and not _filter_tools_by_prefix(tools, tool_prefix)
            and not allow_unavailable_tool_category
        ):
            logger.warning(
                "工具 Router 分类器命中类别但当前 Bot 无匹配工具，回退 unavailable: "
                "category=%s prefix=%s",
                category,
                tool_prefix,
            )
            return ToolLatencyRoute(
                "chat",
                source="llm_router",
                category=f"unavailable_{category}",
            )
        return ToolLatencyRoute(
            "tool",
            router_progress_text(category, session_id),
            tool_prefix=tool_prefix,
            require_tool_call=category != "complex",
            source="llm_router",
            category=category,
        )
    return ToolLatencyRoute("legacy")


async def _collect_tool_router_classifier_text_async(
    llm_client,
    text: str,
    model_name: str,
    max_tokens: int,
    conversation_messages: list[dict] | None = None,
) -> str:
    classifier_text = ""
    stream = _iter_llm_client_stream(
        llm_client,
        _build_tool_router_classifier_messages(text, conversation_messages),
        None,
        0.0,
        max_tokens,
        model_name,
    )
    try:
        async for chunk in stream:
            if chunk.get("type") != "text":
                continue
            classifier_text += chunk.get("content", "")
            if parse_llm_router_decision(classifier_text):
                break
    finally:
        close_stream = getattr(stream, "aclose", None)
        if callable(close_stream):
            await close_stream()
    return classifier_text


async def _classify_legacy_tool_latency_route_async(
    llm_client,
    text: str,
    model_name: str,
    tools: list[dict] | None = None,
    conversation_messages: list[dict] | None = None,
    allow_unavailable_tool_category: bool = False,
    _retried: bool = False,
    session_id: str | None = None,
) -> ToolLatencyRoute:
    if not callable(getattr(llm_client, "stream_call_llm", None)):
        return await asyncio.to_thread(
            _classify_legacy_tool_latency_route,
            llm_client,
            text,
            model_name,
            tools,
            conversation_messages,
            allow_unavailable_tool_category,
            _retried,
            session_id,
        )

    started_at = time.monotonic()
    timeout_ms = _tool_router_classifier_timeout_ms()
    max_tokens = _tool_router_classifier_max_tokens()
    try:
        classifier_text = await asyncio.wait_for(
            _collect_tool_router_classifier_text_async(
                llm_client,
                text,
                model_name,
                max_tokens,
                conversation_messages,
            ),
            timeout=timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        logger.warning("工具 Router async 分类器超时 %.0fms，回退 legacy", timeout_ms)
        return ToolLatencyRoute("legacy")
    except Exception as exc:
        if (
            not _retried
            and conversation_messages
            and _is_router_context_length_error(exc)
        ):
            logger.warning(
                "工具 Router async 分类器上下文超限，尝试仅保留最近 %s 条历史重试",
                _tool_router_classifier_fallback_history_messages(),
            )
            return await _classify_legacy_tool_latency_route_async(
                llm_client,
                text,
                model_name,
                tools,
                _build_tool_router_classifier_messages_for_fallback(conversation_messages),
                allow_unavailable_tool_category,
                True,
                session_id,
            )
        logger.warning("工具 Router async 分类器失败，回退 legacy: %s", exc)
        return ToolLatencyRoute("legacy")

    elapsed_ms = (time.monotonic() - started_at) * 1000.0
    decision = parse_llm_router_decision(classifier_text)
    decision_label = "legacy"
    if decision:
        kind, category = decision
        decision_label = kind if not category else f"{kind}:{category}"
    logger.info(
        "工具 Router async 分类器完成: decision=%s elapsed=%.1fms model=%s max_tokens=%s raw=%r",
        decision_label,
        elapsed_ms,
        model_name,
        max_tokens,
        classifier_text[:20],
    )
    if not decision:
        return ToolLatencyRoute("legacy")
    kind, category = decision
    if kind == "exit":
        return ToolLatencyRoute("exit", source="llm_router_async")
    if kind == "chat":
        return ToolLatencyRoute(
            "chat",
            source="llm_router_async",
            category=category,
        )
    if kind == "tool":
        tool_prefix = router_category_to_tool_prefix(category)
        if (
            tool_prefix
            and not _filter_tools_by_prefix(tools, tool_prefix)
            and not allow_unavailable_tool_category
        ):
            # 4B 路由命中了 category（如 robot/websearch），但当前 Bot 没有对应工具可调。
            # 这通常是 MCP 服务连接失败导致工具列表为空。
            # 旧逻辑回退到 legacy，让 9B 在没工具的情况下自由回答，容易输出鸡汤/无关文本。
            # 新逻辑返回 unavailable_<category>，调用方会给出明确的"工具暂不可用"提示。
            logger.warning(
                "工具 Router async 分类器命中类别但当前 Bot 无匹配工具，回退 unavailable: "
                "category=%s prefix=%s",
                category,
                tool_prefix,
            )
            return ToolLatencyRoute(
                "chat",
                source="llm_router_async",
                category=f"unavailable_{category}",
            )
        return ToolLatencyRoute(
            "tool",
            router_progress_text(category, session_id),
            tool_prefix=tool_prefix,
            require_tool_call=category != "complex",
            source="llm_router_async",
            category=category,
        )
    return ToolLatencyRoute("legacy")


def _build_tool_latency_route(
    text: str,
    tools: list[dict] | None,
    *,
    bot_id: str | None = "xiaowen",
    bot_name: str | None = None,
    messages: list[dict] | None = None,
    session_id: str | None = None,
) -> ToolLatencyRoute:
    return _build_tool_latency_route_v2(
        text,
        tools,
        bot_id=bot_id,
        bot_name=bot_name,
        messages=messages,
        session_id=session_id,
    )


def _filter_tools_by_prefix(tools: list[dict] | None, prefix: str) -> list[dict]:
    safe_prefix = prefix.replace(".", "__")
    return [
        tool
        for tool in tools or []
        if (
            tool.get("function", {}).get("name", "").startswith(prefix)
            or tool.get("function", {}).get("name", "").startswith(safe_prefix)
        )
    ]


def _filter_tools_by_name(tools: list[dict] | None, tool_name: str | None) -> list[dict]:
    if not tool_name:
        return []
    safe_name = tool_name.replace(".", "__")
    return [
        tool
        for tool in tools or []
        if _llm_tool_name(tool) in {tool_name, safe_name}
    ]


def _append_required_tool_instruction(
    messages: list[dict],
    *,
    selected_tool_name: str | None,
) -> list[dict]:
    instruction = (
        "本轮系统已经判定必须先调用当前提供的工具。"
        "请先产生 tool_call，不要直接回答已经完成、已经查询或已经设置。"
        "工具完成后再根据真实结果给用户自然回复。"
    )
    if selected_tool_name:
        instruction += f" 当前目标工具是 {selected_tool_name}。"
    patched = list(messages)
    if patched and patched[0].get("role") == "system":
        first_message = dict(patched[0])
        first_message["content"] = f"{first_message.get('content') or ''}\n\n{instruction}"
        patched[0] = first_message
    else:
        patched.insert(0, {"role": "system", "content": instruction})
    return patched


def _tool_accepts_robot_id(tool: dict) -> bool:
    parameters = tool.get("function", {}).get("parameters") or {}
    properties = parameters.get("properties") or {}
    return isinstance(properties, dict) and "robot_id" in properties


def _llm_tool_name(tool: dict) -> str:
    return str(tool.get("function", {}).get("name") or "")


def _hide_session_robot_id_from_tools(tools: list[dict] | None) -> list[dict] | None:
    """robot_id 是服务端权威上下文，不暴露给 LLM 自行填写。"""
    if not tools:
        return tools

    sanitized_tools = copy.deepcopy(tools)
    for tool in sanitized_tools:
        if not _tool_accepts_robot_id(tool):
            continue
        function = tool.get("function", {})
        parameters = function.get("parameters") or {}
        properties = parameters.get("properties") or {}
        properties.pop("robot_id", None)
        required = parameters.get("required")
        if isinstance(required, list) and "robot_id" in required:
            parameters["required"] = [item for item in required if item != "robot_id"]
    return sanitized_tools


def _is_create_alarm_tool(tool_name: str) -> bool:
    return _tool_name_matches(tool_name, ".create_alarm")


def _parse_alarm_datetime(value) -> datetime | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    normalized = (
        text.replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("/", "-")
        .strip()
    )
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    candidates = [normalized]
    if "T" in normalized:
        candidates.append(normalized.replace("T", " "))

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            return _normalize_alarm_datetime(parsed)
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return _normalize_alarm_datetime(parsed)
        except ValueError:
            continue

    return None


def _format_alarm_datetime_for_tool(value) -> str | None:
    parsed = _parse_alarm_datetime(value)
    if not parsed:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_alarm_datetime(value: datetime) -> datetime:
    tzinfo = _runtime_tzinfo()
    if value.tzinfo is None:
        return value.replace(tzinfo=tzinfo)
    return value.astimezone(tzinfo)


def _validate_alarm_time_for_tool(tool_name: str, tool_args: dict) -> str | None:
    if not _is_create_alarm_tool(tool_name):
        return None

    alarm_time = (tool_args or {}).get("alarm_time")
    parsed_alarm_time = _parse_alarm_datetime(alarm_time)
    if not parsed_alarm_time:
        return (
            "工具未执行：create_alarm 的提醒时间格式不正确。"
            f"请使用 YYYY-MM-DD HH:mm:ss 格式，当前输入：{alarm_time!r}。"
        )

    now = _runtime_now()
    if parsed_alarm_time >= now:
        return None

    return (
        "工具未执行：模型生成的提醒时间 "
        f"{alarm_time!r} 早于当前系统时间 {_format_runtime_now(now)}。"
        "请根据用户原始需求重新换算一个未来时间后再次调用 create_alarm；"
        "如果无法确定正确时间，请向用户确认。"
    )


def _is_robot_tool(tool_name: str) -> bool:
    text = tool_name or ""
    server_name = text.split(".", 1)[0].split("__", 1)[0]
    is_robot_server = (
        server_name == "robot"
        or server_name.startswith("robot_")
        or server_name.startswith("robot-")
    )
    return is_robot_server and any(
        _tool_name_matches(text, suffix) for suffix in ROBOT_TOOL_SUFFIXES
    )


def _is_singing_tool(tool_name: str) -> bool:
    text = tool_name or ""
    server_name = text.split(".", 1)[0].split("__", 1)[0]
    return server_name == "singing" or server_name.startswith("singing_") or server_name.startswith("singing-")


class ImprovedLLMServiceServicer(llm_service_pb2_grpc.LLMServiceServicer):
    """改进版 LLM gRPC 服务实现"""

    def __init__(self):
        if not CONFIG_DATABASE_URL:
            raise ValueError(
                "当前 server-config 分支要求配置 CONFIG_DATABASE_URL。"
                "请先执行 scripts/init_config_db.py 初始化数据库，再启动 LLM 服务。"
            )

        # 初始化 LLM 客户端（不指定默认模型，由 Bot 配置决定）
        if is_missing_secret(LLM_API_KEY):
            logger.error("=" * 60)
            logger.error("错误: LLM_API_KEY 未设置或使用了占位值")
            logger.error("请设置环境变量: export LLM_API_KEY=your-actual-key")
            logger.error("=" * 60)
            raise ValueError("LLM_API_KEY 未正确配置，LLM 服务无法启动")

        self.llm_client = ImprovedQwenLLMClient(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
            timeout=LLM_HTTP_TIMEOUT_SEC,
        )
        self.router_llm_client = self.llm_client
        self.router_model_name = None
        self.router_base_url = LLM_BASE_URL
        if LLM_ROUTER_MODEL_NAME:
            router_api_key = LLM_ROUTER_API_KEY or LLM_API_KEY
            router_base_url = LLM_ROUTER_BASE_URL or LLM_BASE_URL
            if is_missing_secret(router_api_key):
                logger.error("=" * 60)
                logger.error("错误: LLM_ROUTER_MODEL_NAME 已配置，但路由模型 API Key 无效")
                logger.error("请设置环境变量: export LLM_ROUTER_API_KEY=your-router-key")
                logger.error("=" * 60)
                raise ValueError("LLM_ROUTER_API_KEY 未正确配置，Router 分类器无法启动")
            self.router_llm_client = ImprovedQwenLLMClient(
                api_key=router_api_key,
                base_url=router_base_url,
                timeout=LLM_ROUTER_HTTP_TIMEOUT_SEC,
            )
            self.router_model_name = LLM_ROUTER_MODEL_NAME
            self.router_base_url = router_base_url
            logger.info(
                "工具 Router 分类器使用独立模型: model=%s base_url=%s",
                self.router_model_name,
                self.router_base_url,
            )
        else:
            logger.info("工具 Router 分类器复用主 LLM: base_url=%s", self.router_base_url)

        self.vision_snapshot_client = None
        if LLM_VISION_ENABLED and LLM_VISION_GATEWAY_TOKEN:
            self.vision_snapshot_client = VisionSnapshotClient(
                base_url=LLM_VISION_GATEWAY_BASE_URL,
                token=LLM_VISION_GATEWAY_TOKEN,
                timeout_sec=LLM_VISION_FETCH_TIMEOUT_SEC,
            )
            logger.info(
                "视觉上下文取图已启用: gateway=%s",
                LLM_VISION_GATEWAY_BASE_URL,
            )
        elif LLM_VISION_ENABLED:
            logger.warning("视觉上下文未启用：LLM_VISION_GATEWAY_TOKEN 未配置")

        # 初始化会话管理器
        self.session_manager = SessionManager(max_history=10)
        self._workflow_route_cache: OrderedDict[
            tuple[str, str, str, str], tuple[float, ToolLatencyRoute]
        ] = OrderedDict()
        logger.info("会话管理器已初始化")
        self.agent_registry = build_default_agent_registry()
        self.agent_tool_invoker = DefaultAgentToolInvoker()
        discovered_agents = [_agent_catalog_payload(agent) for agent in self.agent_registry.list_agents()]
        ConfigRepository(CONFIG_DATABASE_URL).sync_discovered_agents(discovered_agents)
        logger.info(
            "Agent runtime 已初始化: %s",
            ", ".join(agent.id for agent in self.agent_registry.list_agents()),
        )

        self.runtime_state = LLMRuntimeState(
            database_url=CONFIG_DATABASE_URL,
            mcp_enabled=MCP_ENABLED,
        )
        runtime_status = self.runtime_state.get_status()
        logger.info(
            "LLM runtime 已初始化: source=%s, bots=%s, mcp_servers=%s, default_bot=%s",
            runtime_status["source"],
            runtime_status["bot_count"],
            runtime_status["mcp_count"],
            runtime_status["default_bot_id"],
        )

        logger.info("改进版 LLM 服务已初始化")

    def build_workflow_service(self) -> WorkflowRuntimeService:
        """构造独立复杂任务服务；调用方负责受开关保护地注册。"""
        return WorkflowRuntimeService(
            classify=self._workflow_classify,
            planner=self._workflow_plan,
            execute_step=self._workflow_execute_step,
            synthesize=self._workflow_synthesize,
        )

    @staticmethod
    def _workflow_route_key(
        session_id: str,
        text: str,
        bot_id: str,
        robot_id: str,
    ) -> tuple[str, str, str, str]:
        return tuple(
            str(value or "").strip()
            for value in (session_id, text, bot_id, robot_id)
        )

    def _cache_workflow_route(self, request, route: ToolLatencyRoute) -> None:
        if route.category == "complex":
            return
        cache = getattr(self, "_workflow_route_cache", None)
        if cache is None:
            cache = OrderedDict()
            self._workflow_route_cache = cache
        cutoff = time.monotonic() - 10.0
        for key in list(cache):
            if cache[key][0] >= cutoff:
                break
            cache.pop(key, None)
        key = self._workflow_route_key(
            request.session_id,
            request.text,
            request.bot_id,
            request.robot_id,
        )
        cache[key] = (time.monotonic(), route)
        cache.move_to_end(key)
        while len(cache) > 128:
            cache.popitem(last=False)

    def _pop_workflow_route(
        self,
        *,
        session_id: str,
        text: str,
        bot_id: str,
        robot_id: str,
    ) -> ToolLatencyRoute | None:
        cache = getattr(self, "_workflow_route_cache", None)
        if not cache:
            return None
        key = self._workflow_route_key(session_id, text, bot_id, robot_id)
        cached = cache.pop(key, None)
        if cached is None or cached[0] < time.monotonic() - 10.0:
            return None
        return cached[1]

    async def _workflow_classify(self, request) -> WorkflowClassification:
        bot, _, safe_tools, _, _ = await self._workflow_prepare_tools(request.bot_id)
        history = self.session_manager.get_messages_for_llm(request.session_id)
        route = ToolLatencyRoute("legacy")
        if not _is_tool_latency_experiment_enabled():
            route = ToolLatencyRoute("legacy", source="disabled")
        elif _is_tool_router_classifier_enabled():
            router_client = getattr(self, "router_llm_client", self.llm_client)
            router_model = getattr(self, "router_model_name", None) or bot.model
            route = await _classify_legacy_tool_latency_route_async(
                router_client,
                request.text,
                router_model,
                safe_tools,
                [*history, {"role": "user", "content": request.text}],
                allow_unavailable_tool_category=not safe_tools,
                session_id=request.session_id,
            )
            if not safe_tools and route.category == "complex":
                route = ToolLatencyRoute(
                    route.kind,
                    source=route.source,
                    category="unavailable_complex",
                )
        elif not safe_tools:
            route = ToolLatencyRoute("chat", source="no_tools")
        else:
            route = _build_tool_latency_route(
                request.text,
                safe_tools,
                bot_id=bot.bot_id,
                bot_name=bot.name,
                messages=[*history, {"role": "user", "content": request.text}],
                session_id=request.session_id,
            )
        self._cache_workflow_route(request, route)
        return WorkflowClassification(
            route_kind=route.kind,
            category=route.category or "",
            source=route.source or "",
        )

    async def _workflow_prepare_tools(self, bot_id: str):
        bot = self.runtime_state.bot_manager.get_bot_or_default(bot_id or None)
        mcp_manager = self.runtime_state.mcp_manager
        if not MCP_ENABLED or mcp_manager is None:
            return bot, mcp_manager, [], {}, set()
        bot_mcp_servers = list(bot.mcp_servers or [])
        await self._ensure_mcp_servers_connected(mcp_manager, bot_mcp_servers)
        raw_tools = mcp_manager.get_tools_for_llm(bot_mcp_servers)
        safe_tools = _hide_session_robot_id_from_tools(raw_tools) or []
        raw_by_safe_name = {
            _llm_tool_name(safe_tool): raw_tool
            for safe_tool, raw_tool in zip(safe_tools, raw_tools)
            if _llm_tool_name(safe_tool)
        }
        terminal_tool_names = {
            safe_name
            for safe_name in raw_by_safe_name
            if _terminal_exit_response_for_tool(mcp_manager.resolve_tool_name(safe_name))
        }
        return bot, mcp_manager, safe_tools, raw_by_safe_name, terminal_tool_names

    async def _workflow_collect_model_text(
        self,
        messages,
        *,
        model_name: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        parts: list[str] = []
        async for chunk in _iter_llm_client_stream(
            self.llm_client,
            messages,
            None,
            temperature,
            max_tokens,
            model_name,
        ):
            if chunk.get("type") == "text" and chunk.get("content"):
                parts.append(chunk["content"])
        return "".join(parts).strip()

    async def _workflow_plan(self, request):
        bot, _, safe_tools, _, terminal_tool_names = await self._workflow_prepare_tools(
            request.bot_id
        )

        async def generate(messages, *, temperature, max_tokens):
            return await self._workflow_collect_model_text(
                messages,
                model_name=bot.model,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        planner = ComplexWorkflowPlanner(
            generate=generate,
            tool_schemas=tool_schemas_from_openai_tools(safe_tools),
            terminal_tool_names=terminal_tool_names,
        )
        return await planner.plan(
            user_text=request.text,
            conversation_messages=self.session_manager.get_messages_for_llm(request.session_id),
        )

    async def _workflow_execute_step(self, runtime, step) -> StepExecutionOutcome:
        bot, mcp_manager, _, raw_by_safe_name, _ = await self._workflow_prepare_tools(
            runtime.bot_id
        )
        if step.type == "vision_analyze":
            return await self._workflow_analyze_vision(runtime, step, bot)
        if step.type != "tool" or not step.tool_name:
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="unsupported_step_type",
                message="不支持的工作流步骤",
            )

        raw_tool = raw_by_safe_name.get(step.tool_name)
        if raw_tool is None:
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="tool_unavailable",
                message="计划中的工具当前不可用",
            )
        resolved_tool_name = mcp_manager.resolve_tool_name(step.tool_name)
        arguments = dict(step.arguments)
        if _tool_accepts_robot_id(raw_tool):
            if not runtime.robot_id:
                return StepExecutionOutcome(
                    success=False,
                    status="failed",
                    error_code="robot_id_required",
                    message="机器人身份不可用",
                )
            arguments["robot_id"] = runtime.robot_id
        alarm_error = _validate_alarm_time_for_tool(resolved_tool_name, arguments)
        if alarm_error:
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="invalid_alarm_time",
                message=alarm_error,
            )

        is_websearch = _is_websearch_tool_name(resolved_tool_name)
        arguments = _limit_websearch_args(
            resolved_tool_name,
            {"function": raw_tool.get("function", {})},
            arguments,
        )
        try:
            max_attempts = _WEBSEARCH_TOOL_MAX_ATTEMPTS if is_websearch else 1
            result = None
            for attempt in range(1, max_attempts + 1):
                try:
                    call = mcp_manager.call_tool(
                        step.tool_name,
                        arguments,
                        **({"max_retries": 0} if is_websearch else {}),
                    )
                    result = await asyncio.wait_for(
                        call,
                        timeout=_WEBSEARCH_TOOL_TIMEOUT_SEC if is_websearch else 30.0,
                    )
                    break
                except Exception as exc:
                    if not (
                        is_websearch
                        and attempt < max_attempts
                        and _is_transient_websearch_error(exc)
                    ):
                        raise
                    logger.warning(
                        "Workflow WebSearch 瞬态失败，准备重试: tool=%s attempt=%s/%s error=%s",
                        resolved_tool_name,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    await asyncio.sleep(0)
        except asyncio.TimeoutError:
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="tool_timeout",
                message=_build_tool_failure_message(resolved_tool_name),
            )
        except Exception as exc:
            logger.warning("Workflow 工具调用失败: tool=%s error=%s", resolved_tool_name, exc)
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="tool_error",
                message=_build_tool_failure_message(resolved_tool_name),
            )

        result_text = _extract_tool_result_text(result)
        failed = _tool_result_looks_failed(result)
        return StepExecutionOutcome(
            success=not failed,
            status="failed" if failed else "success",
            result={
                "tool_name": resolved_tool_name,
                "text": result_text,
            },
            error_code="tool_failed" if failed else "",
            message=_build_tool_failure_message(resolved_tool_name) if failed else "",
        )

    async def _workflow_analyze_vision(self, runtime, step, bot) -> StepExecutionOutcome:
        if self.vision_snapshot_client is None:
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="vision_not_configured",
                message="视觉服务未配置",
            )
        fetch = await self.vision_snapshot_client.fetch_latest(runtime.session_id)
        if fetch.snapshot is None:
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code=f"vision_{fetch.status}",
                message="最新画面不可用",
            )
        allowed = list(step.allowed_values)
        instruction = (
            "根据图片和用户请求完成视觉判断。只输出一个 JSON 对象："
            '{"value":允许值之一}。允许值：'
            + json.dumps(allowed, ensure_ascii=False, separators=(",", ":"))
        )
        messages = [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": build_visual_user_content(runtime.user_text, fetch.snapshot),
            },
        ]
        raw = await self._workflow_collect_model_text(
            messages,
            model_name=bot.model,
            temperature=0.0,
            max_tokens=80,
        )
        try:
            value = json.loads(raw).get("value")
        except (AttributeError, json.JSONDecodeError):
            value = None
        if value not in allowed:
            return StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="invalid_vision_decision",
                message="视觉判断结果未通过校验",
            )
        return StepExecutionOutcome(
            success=True,
            status="success",
            result={
                step.output_key: value,
                "frame_id": fetch.snapshot.frame_id,
            },
        )

    async def _workflow_synthesize(self, runtime, step):
        bot = self.runtime_state.bot_manager.get_bot_or_default(runtime.bot_id or None)
        result_context = json.dumps(
            runtime.results,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        system_content = (
            f"{bot.system_prompt}\n\n{_build_runtime_context_message()['content']}\n\n"
            "你正在汇总一个已经由程序执行的复杂任务。只能依据给出的真实执行结果回答，"
            "不要声称尚未执行的终止动作已经完成；用简短自然的中文连续回答，不要列出内部步骤。"
        )
        user_content = (
            f"用户原始请求：{runtime.user_text}\n"
            f"已执行结果：{result_context}\n"
            "请生成本轮播报内容。"
        )
        vision_required = step.vision in {"latest_optional", "latest_required"}
        if vision_required and self.vision_snapshot_client is not None:
            fetch = await self.vision_snapshot_client.fetch_latest(runtime.session_id)
            if fetch.snapshot is not None:
                user_content = build_visual_user_content(user_content, fetch.snapshot)
                system_content = f"{system_content}\n\n{VISION_PROMPT}"
            elif step.vision == "latest_required":
                raise RuntimeError("required vision snapshot unavailable")
        elif step.vision == "latest_required":
            raise RuntimeError("required vision service unavailable")

        messages = [
            {"role": "system", "content": system_content},
            *self.session_manager.get_messages_for_llm(runtime.session_id),
            {"role": "user", "content": user_content},
        ]
        response_parts: list[str] = []
        async for chunk in _iter_llm_client_stream(
            self.llm_client,
            messages,
            None,
            bot.temperature,
            bot.max_tokens,
            bot.model,
        ):
            if chunk.get("type") != "text" or not chunk.get("content"):
                continue
            text = chunk["content"]
            response_parts.append(text)
            yield text
        response_text = _strip_standalone_tool_tags("".join(response_parts))
        if response_text:
            self.session_manager.add_message(runtime.session_id, "user", runtime.user_text)
            self.session_manager.add_message(runtime.session_id, "assistant", response_text)

    def get_config_status(self) -> dict:
        status = self.runtime_state.get_status()
        status["complex_workflow_enabled"] = bool(LLM_COMPLEX_WORKFLOW_ENABLED)
        enabled_agent_ids = self.runtime_state.enabled_agent_ids()
        status["agents"] = [
            {
                "id": agent.id,
                "name": agent.name,
                "description": getattr(agent, "description", "") or "",
                "class_name": agent.__class__.__name__,
                "module": agent.__class__.__module__,
                "trigger_examples": list(getattr(agent, "trigger_examples", ()) or ()),
                "allowed_tools": list(getattr(agent, "allowed_tools", ()) or ()),
                "tool_groups": list(getattr(agent, "tool_groups", ()) or ()),
                "active": agent.id in enabled_agent_ids,
            }
            for agent in self.agent_registry.list_agents()
        ]
        return status

    def get_debug_threads(self) -> dict:
        return _build_debug_threads_snapshot()

    async def async_validate_runtime_config(self, version: int | None = None) -> dict:
        return await self.runtime_state.validate(version)

    async def async_reload_runtime_config(self, version: int | None = None) -> dict:
        return await self.runtime_state.reload(version)

    async def async_reconnect_mcp_server(self, server_key: str) -> dict:
        return await self.runtime_state.reconnect_mcp_server(server_key)

    async def ClearSession(self, request, context):
        session_id = (request.session_id or "").strip()
        if not session_id:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("session_id 不能为空")
            return llm_service_pb2.ClearSessionResponse(
                success=False,
                cleared=False,
                message="session_id 不能为空",
            )

        cleared = self.session_manager.clear_session(session_id)
        logger.info("LLM 会话清理完成: session_id=%s, cleared=%s", session_id, cleared)
        return llm_service_pb2.ClearSessionResponse(
            success=True,
            cleared=cleared,
            message="cleared" if cleared else "not_found",
        )

    async def StreamChat(self, request, context):
        """
        改进版流式对话服务

        Args:
            request: ChatRequest
            context: gRPC 上下文

        Yields:
            ChatResponse: 流式文本响应
        """
        try:
            request_started_at = time.monotonic()
            llm_metrics = {
                "llm_mcp_enabled": bool(MCP_ENABLED),
                "llm_stream_mode": "unknown",
            }

            def set_metric(key: str, value) -> None:
                llm_metrics[key] = value

            def final_response(stream_mode: str | None = None) -> llm_service_pb2.ChatResponse:
                if stream_mode:
                    set_metric("llm_stream_mode", stream_mode)
                set_metric("llm_service_total_ms", _elapsed_monotonic_ms(request_started_at))
                logger.info(
                    "LLM 请求指标: mode=%s router=%s/%s router_ms=%s first_text_ms=%s messages=%s max_tokens=%s max_response_chars=%s response_chars=%s truncated=%s mcp_prepare_ms=%s tools=%s tool_total_ms=%s total_ms=%s",
                    llm_metrics.get("llm_stream_mode"),
                    llm_metrics.get("llm_router_kind", "<none>"),
                    llm_metrics.get("llm_router_source", "<none>"),
                    llm_metrics.get("llm_router_ms", 0),
                    llm_metrics.get("llm_first_text_ms", 0),
                    llm_metrics.get("llm_messages_count", 0),
                    llm_metrics.get("llm_max_tokens", 0),
                    llm_metrics.get("llm_max_response_chars", 0),
                    llm_metrics.get("llm_response_chars", 0),
                    llm_metrics.get("llm_response_truncated", False),
                    llm_metrics.get("llm_mcp_prepare_ms", 0),
                    llm_metrics.get("llm_tools_count", 0),
                    llm_metrics.get("llm_tool_total_ms", 0),
                    llm_metrics.get("llm_service_total_ms", 0),
                )
                return llm_service_pb2.ChatResponse(
                    text="",
                    is_final=True,
                    metrics_json=_metrics_json(llm_metrics),
                )

            session_id = request.session_id
            trace_id = (getattr(request, "trace_id", "") or "").strip()
            user_text_for_model = request.text
            user_text, audio_context = _split_audio_context_text(user_text_for_model)
            bot_id = request.bot_id if request.bot_id else None
            robot_id = request.robot_id.strip() if getattr(request, "robot_id", "") else ""
            set_metric("llm_request_text_chars", len(user_text or ""))
            vision_snapshot_client = getattr(self, "vision_snapshot_client", None)
            # vision_intent 仅依赖关键词判断；vision_snapshot_client 是否可用
            # 留给后续拉取阶段检查（None 时走 vision_unavailable 兜底）。
            vision_intent_from_keywords = bool(is_visual_context_intent(user_text))
            # 真正决定 vision_intent 的位置在 router 决定之后（line ~2030 附近），
            # 这样 4b router 输出 V=vision 时也能走 vision path。
            vision_intent = vision_intent_from_keywords
            set_metric("llm_vision_intent", vision_intent)

            # 获取 Bot 配置
            bot_manager = self.runtime_state.bot_manager
            mcp_manager = self.runtime_state.mcp_manager

            bot = bot_manager.get_bot_or_default(bot_id)
            set_metric("llm_bot_id", bot.bot_id)
            set_metric("llm_bot_name", bot.name)
            logger.info(f"使用机器人: {bot.name} (id={bot.bot_id})")

            logger.info(
                "收到流式对话请求: session_id=%s, trace_id=%s, robot_id=%s",
                session_id,
                trace_id or "<none>",
                robot_id or "<none>",
            )
            logger.info(f"用户输入: {user_text}")
            if audio_context:
                logger.info("语音上下文: %s", audio_context)

            # 获取配置（请求中的 config 可覆盖 Bot 默认配置）
            model_name = request.config.model_name if request.config.model_name else bot.model
            temperature = request.config.temperature if request.config.temperature else bot.temperature
            max_tokens = request.config.max_tokens if request.config.max_tokens else bot.max_tokens
            max_response_chars = max(0, int(getattr(bot, "max_response_chars", 0) or 0))
            set_metric("llm_model", model_name)
            set_metric("llm_max_tokens", max_tokens)
            set_metric("llm_max_response_chars", max_response_chars)
            set_metric("llm_response_chars_sent", 0)
            set_metric("llm_response_truncated", False)

            response_chars_sent = 0

            def apply_response_char_budget(text: str) -> str:
                nonlocal response_chars_sent
                if not text:
                    return ""
                if "[EXIT]" in text:
                    response_chars_sent += len(text)
                    set_metric("llm_response_chars_sent", response_chars_sent)
                    return text
                if max_response_chars <= 0:
                    response_chars_sent += len(text)
                    set_metric("llm_response_chars_sent", response_chars_sent)
                    return text

                remaining = max_response_chars - response_chars_sent
                if remaining <= 0:
                    if not llm_metrics.get("llm_response_truncated"):
                        logger.info(
                            "LLM 回复已达到 max_response_chars 上限: bot=%s limit=%s",
                            bot.bot_id,
                            max_response_chars,
                        )
                    set_metric("llm_response_truncated", True)
                    set_metric("llm_response_chars_sent", response_chars_sent)
                    return ""

                if len(text) <= remaining:
                    response_chars_sent += len(text)
                    set_metric("llm_response_chars_sent", response_chars_sent)
                    return text

                limited_text = text[:remaining]
                response_chars_sent += len(limited_text)
                if not llm_metrics.get("llm_response_truncated"):
                    logger.info(
                        "LLM 回复已截断到 max_response_chars 上限: bot=%s limit=%s",
                        bot.bot_id,
                        max_response_chars,
                    )
                set_metric("llm_response_truncated", True)
                set_metric("llm_response_chars_sent", response_chars_sent)
                return limited_text

            def response_char_budget_exhausted() -> bool:
                return max_response_chars > 0 and response_chars_sent >= max_response_chars

            logger.info(
                "LLM 配置: model=%s, temp=%s, max_tokens=%s, max_response_chars=%s",
                model_name,
                temperature,
                max_tokens,
                max_response_chars,
            )

            agent_state = self.session_manager.get_agent_state(session_id)
            allowed_agent_ids = set(bot.agents or [])

            async def llm_normalizer(answer_text: str) -> str | None:
                return await self._normalize_cognitive_answer_with_llm(
                    answer_text,
                    model_name=model_name,
                )

            async def agent_llm_responder(
                messages: list[dict[str, str]],
                temperature: float,
                max_tokens: int,
            ) -> str:
                return await self._generate_agent_text(
                    messages,
                    model_name=model_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

            async def agent_llm_streamer(
                messages: list[dict[str, str]],
                temperature: float,
                max_tokens: int,
            ):
                async for chunk in _iter_llm_client_stream(
                    self.llm_client,
                    messages,
                    None,
                    temperature,
                    max_tokens,
                    model_name,
                ):
                    if chunk.get("type") == "text":
                        yield chunk.get("content", "")

            agent_context = AgentContext(
                session_id=session_id,
                model_name=model_name,
                robot_id=robot_id or None,
                llm_normalizer=llm_normalizer,
                llm_responder=agent_llm_responder,
                llm_streamer=agent_llm_streamer,
                tool_invoker=self.agent_tool_invoker,
            )

            if agent_state:
                active_agent_id = agent_state.get("agent_id")
                active_agent = self.agent_registry.get(active_agent_id)
                if not active_agent or active_agent_id not in allowed_agent_ids:
                    logger.warning(
                        "会话 %s 存在未知或当前 Bot 未绑定的 Agent 状态，已清理: %s",
                        session_id,
                        active_agent_id,
                    )
                    self.session_manager.clear_agent_state(session_id)
                else:
                    stream_handler = getattr(active_agent, "handle_stream", None)
                    if callable(stream_handler):
                        result = None
                        streamed_text = ""
                        async for event in stream_handler(user_text, agent_state, agent_context):
                            event_text = getattr(event, "text", "") or ""
                            if event_text:
                                limited_text = apply_response_char_budget(event_text)
                                if limited_text:
                                    streamed_text += limited_text
                                    yield llm_service_pb2.ChatResponse(text=limited_text, is_final=False)
                            event_result = getattr(event, "result", None)
                            if event_result is not None:
                                result = event_result
                        if result is None:
                            logger.warning("会话 %s Agent 流式处理未返回最终状态: agent=%s", session_id, active_agent.id)
                            result = AgentResult(
                                text=streamed_text,
                                state=None,
                                finished=True,
                                metadata={"persist_history": False},
                            )
                        if result.finished or result.state is None:
                            self.session_manager.clear_agent_state(session_id)
                        else:
                            self.session_manager.set_agent_state(session_id, result.state)

                        if result.metadata.get("persist_history", True):
                            self.session_manager.add_message(session_id, "user", user_text)
                            if streamed_text:
                                self.session_manager.add_message(session_id, "assistant", streamed_text)
                        logger.info(
                            "会话 %s 由 Agent 流式接管: agent=%s, active=%s",
                            session_id,
                            active_agent.id,
                            not result.finished,
                        )
                        set_metric("llm_agent_id", active_agent.id)
                        set_metric("llm_response_chars", len(streamed_text))
                        yield final_response("active_agent_stream")
                        return

                    result = await active_agent.handle(
                        user_text,
                        agent_state,
                        agent_context,
                    )
                    limited_text = apply_response_char_budget(result.text or "")
                    if result.finished or result.state is None:
                        self.session_manager.clear_agent_state(session_id)
                    else:
                        self.session_manager.set_agent_state(session_id, result.state)

                    if result.metadata.get("persist_history", True):
                        self.session_manager.add_message(session_id, "user", user_text)
                        if limited_text:
                            self.session_manager.add_message(session_id, "assistant", limited_text)
                    logger.info(
                        "会话 %s 由 Agent 接管: agent=%s, active=%s",
                        session_id,
                        active_agent.id,
                        not result.finished,
                    )
                    set_metric("llm_agent_id", active_agent.id)
                    set_metric("llm_response_chars", len(limited_text))
                    if limited_text:
                        yield llm_service_pb2.ChatResponse(text=limited_text, is_final=False)
                    yield final_response("active_agent")
                    return

            entry_agent = self.agent_registry.find_entry_agent(
                user_text,
                agent_context,
                allowed_agent_ids=allowed_agent_ids,
            )
            if entry_agent:
                stream_starter = getattr(entry_agent, "start_stream", None)
                if callable(stream_starter):
                    result = None
                    streamed_text = ""
                    async for event in stream_starter(user_text, agent_context):
                        event_text = getattr(event, "text", "") or ""
                        if event_text:
                            limited_text = apply_response_char_budget(event_text)
                            if limited_text:
                                streamed_text += limited_text
                                yield llm_service_pb2.ChatResponse(text=limited_text, is_final=False)
                        event_result = getattr(event, "result", None)
                        if event_result is not None:
                            result = event_result
                    if result is None:
                        logger.warning("会话 %s Agent 流式启动未返回最终状态: agent=%s", session_id, entry_agent.id)
                        result = AgentResult(
                            text=streamed_text,
                            state=None,
                            finished=True,
                            metadata={"persist_history": False},
                        )
                    if result.finished or result.state is None:
                        self.session_manager.clear_agent_state(session_id)
                    else:
                        self.session_manager.set_agent_state(session_id, result.state)

                    if result.metadata.get("persist_history", True):
                        self.session_manager.add_message(session_id, "user", user_text)
                        if streamed_text:
                            self.session_manager.add_message(session_id, "assistant", streamed_text)
                    logger.info("会话 %s 流式触发 Agent: %s", session_id, entry_agent.id)
                    set_metric("llm_agent_id", entry_agent.id)
                    set_metric("llm_response_chars", len(streamed_text))
                    yield final_response("entry_agent_stream")
                    return

                result = await entry_agent.start(user_text, agent_context)
                limited_text = apply_response_char_budget(result.text or "")
                if result.finished or result.state is None:
                    self.session_manager.clear_agent_state(session_id)
                else:
                    self.session_manager.set_agent_state(session_id, result.state)

                if result.metadata.get("persist_history", True):
                    self.session_manager.add_message(session_id, "user", user_text)
                    if limited_text:
                        self.session_manager.add_message(session_id, "assistant", limited_text)
                logger.info("会话 %s 触发 Agent: %s", session_id, entry_agent.id)
                set_metric("llm_agent_id", entry_agent.id)
                set_metric("llm_response_chars", len(limited_text))
                if limited_text:
                    yield llm_service_pb2.ChatResponse(text=limited_text, is_final=False)
                yield final_response("entry_agent")
                return

            exit_response = build_whole_session_exit_response(user_text, session_id)
            if exit_response:
                logger.info("硬保护命中整会话退出: session_id=%s", session_id)
                set_metric("llm_response_chars", len(exit_response))
                yield llm_service_pb2.ChatResponse(text=exit_response, is_final=False)
                yield final_response("exit_guard")
                return

            # Router 需要最近五个完整历史轮次；先取快照，避免加入本轮后被历史上限截断半轮。
            router_conversation_messages = self.session_manager.get_messages_for_llm(session_id)

            # 添加用户消息到会话历史
            self.session_manager.add_message(session_id, "user", user_text)

            # 获取会话历史（用于 LLM API）
            messages = self.session_manager.get_messages_for_llm(session_id)
            if audio_context:
                messages = _replace_latest_user_message(messages, user_text, user_text_for_model)

            # 注入 Bot 的 system prompt 和本轮运行时上下文（不写入会话历史）
            runtime_context = _build_runtime_context_message()["content"]
            system_content = (
                f"{bot.system_prompt}\n\n{runtime_context}"
                if bot.system_prompt
                else runtime_context
            )
            vision_snapshot = None
            # vision snapshot 的拉取与 system/messages 修补延后到 router 决定之后；
            # 这样 4b router 输出 V=vision 时也会补一次 vision_intent。
            messages = [{"role": "system", "content": system_content}] + messages

            logger.info(f"会话历史消息数: {len(messages)}")
            set_metric("llm_messages_count", len(messages))

            # 准备工具列表（根据 Bot 配置过滤）
            tools = None
            session_robot_id_tool_names: set[str] = set()
            # vision 路径跳过 mcp tools 准备：tools 留给 chat_with_tools 用，
            # vision path 只发文本和图片，不调工具。
            if (
                vision_snapshot is None
                and not vision_intent_from_keywords
                and MCP_ENABLED
                and mcp_manager
            ):
                mcp_prepare_started_at = time.monotonic()
                # 获取 Bot 配置的 MCP Server 列表
                bot_mcp_servers = list(bot.mcp_servers or [])
                set_metric("llm_mcp_servers_count", len(bot_mcp_servers))

                # Warm up 会在启动 / Apply 后提前连接；这里保留请求时兜底。
                mcp_connect_started_at = time.monotonic()
                await self._ensure_mcp_servers_connected(mcp_manager, bot_mcp_servers)
                set_metric("llm_mcp_connect_ms", _elapsed_monotonic_ms(mcp_connect_started_at))

                # 获取工具（按 Bot 配置过滤），robot_id 由 Gateway 会话上下文权威注入，不交给 LLM 填。
                mcp_tools_started_at = time.monotonic()
                raw_tools = mcp_manager.get_tools_for_llm(bot_mcp_servers)
                session_robot_id_tool_names = {
                    mcp_manager.resolve_tool_name(_llm_tool_name(tool))
                    for tool in raw_tools
                    if _tool_accepts_robot_id(tool) and _llm_tool_name(tool)
                }
                tools = _hide_session_robot_id_from_tools(raw_tools)
                set_metric("llm_mcp_tool_build_ms", _elapsed_monotonic_ms(mcp_tools_started_at))
                set_metric("llm_tools_count", len(tools) if tools else 0)
                set_metric("llm_mcp_prepare_ms", _elapsed_monotonic_ms(mcp_prepare_started_at))
                logger.info(f"获取到 {len(tools)} 个 MCP 工具 (Bot: {bot.bot_id})")
            else:
                set_metric("llm_mcp_servers_count", 0)
                set_metric("llm_tools_count", 0)

            preclassified_route = self._pop_workflow_route(
                session_id=session_id,
                text=user_text,
                bot_id=bot.bot_id,
                robot_id=robot_id,
            )
            latency_route = ToolLatencyRoute("legacy")
            if vision_snapshot is not None or vision_intent_from_keywords:
                # 关键词命中视觉意图或 vision snapshot 已就绪：直接走 vision_context，
                # 跳过 4b router，避免对视觉类问题多走一次小模型分类。
                latency_route = ToolLatencyRoute(
                    "chat",
                    source="vision_context",
                    category="vision",
                )
                set_metric("llm_router_kind", latency_route.kind)
                set_metric("llm_router_source", latency_route.source)
                set_metric("llm_router_category", latency_route.category)
                set_metric("llm_router_classifier_used", False)
            elif _is_tool_latency_experiment_enabled():
                router_started_at = time.monotonic()
                router_classifier_used = False
                if preclassified_route is not None:
                    latency_route = preclassified_route
                    router_classifier_used = "llm_router" in (latency_route.source or "")
                    logger.info(
                        "复用 Workflow 预分类结果: kind=%s category=%s source=%s",
                        latency_route.kind,
                        latency_route.category or "<none>",
                        latency_route.source or "<none>",
                    )
                elif not tools:
                    required_category = detect_required_tool_category(user_text)
                    if required_category:
                        latency_route = ToolLatencyRoute(
                            "tool",
                            source="deterministic_unavailable",
                            category=required_category,
                        )
                    else:
                        latency_route = ToolLatencyRoute("chat", source="no_tools")
                else:
                    latency_route = _build_tool_latency_route(
                        user_text,
                        tools,
                        bot_id=bot.bot_id,
                        bot_name=bot.name,
                        messages=messages,
                        session_id=session_id,
                    )
                if (
                    preclassified_route is None
                    and latency_route.kind == "legacy"
                    and tools
                    and _is_tool_router_classifier_enabled()
                    and is_xiaowen_profile(bot.bot_id, bot.name)
                ):
                    router_classifier_used = True
                    router_llm_client = getattr(self, "router_llm_client", self.llm_client)
                    router_model_name = getattr(self, "router_model_name", None) or model_name
                    set_metric("llm_router_classifier_model", router_model_name)
                    latency_route = await _classify_legacy_tool_latency_route_async(
                        router_llm_client,
                        user_text,
                        router_model_name,
                        tools,
                        [
                            *router_conversation_messages,
                            {"role": "user", "content": user_text},
                        ],
                        session_id=session_id,
                    )
                if (
                    latency_route.kind == "tool"
                    and latency_route.category != "complex"
                    and not latency_route.selected_tool_name
                    and tools
                ):
                    selected_tool_name = None
                    selected_tool_prefix = latency_route.tool_prefix
                    selected_tool_progress = latency_route.progress_text
                    if latency_route.category == "task":
                        selected_tool_name = _infer_task_tool_name_from_text(user_text, tools)
                        if selected_tool_name:
                            selected_tool_prefix = selected_tool_prefix or "robots_task_service."

                    if not selected_tool_name:
                        deterministic_route = _build_tool_latency_route(
                            user_text,
                            tools,
                            bot_id=bot.bot_id,
                            bot_name=bot.name,
                            messages=messages,
                            session_id=session_id,
                        )
                        if (
                            deterministic_route.kind == "tool"
                            and deterministic_route.selected_tool_name
                            and deterministic_route.category == latency_route.category
                        ):
                            selected_tool_name = deterministic_route.selected_tool_name
                            selected_tool_prefix = deterministic_route.tool_prefix or selected_tool_prefix
                            selected_tool_progress = deterministic_route.progress_text or selected_tool_progress

                    if (
                        selected_tool_name
                    ):
                        latency_route = replace(
                            latency_route,
                            selected_tool_name=selected_tool_name,
                            tool_prefix=selected_tool_prefix,
                            progress_text=selected_tool_progress,
                        )
                        logger.info(
                            "Router 类别结果使用确定性规则补齐单工具: %s",
                            selected_tool_name,
                        )
                    else:
                        logger.warning(
                            "Router 类别结果无法补齐单工具: kind=%s category=%s source=%s",
                            latency_route.kind,
                            latency_route.category or "<none>",
                            latency_route.source or "<none>",
                        )
                if latency_route.model_text:
                    current_model_user_text = user_text_for_model if audio_context else user_text
                    routed_model_text = latency_route.model_text
                    if audio_context:
                        routed_model_text = (
                            f"{_AUDIO_CONTEXT_PREFIX}{audio_context}]\n"
                            f"{_AUDIO_CONTEXT_USER_MARKER}{latency_route.model_text}"
                        )
                    messages = _replace_latest_user_message(
                        messages,
                        current_model_user_text,
                        routed_model_text,
                    )
                logger.info(
                    "工具首响实验路由: kind=%s, progress=%s",
                    latency_route.kind,
                    bool(latency_route.progress_text),
                )
                set_metric("llm_router_ms", _elapsed_monotonic_ms(router_started_at))
                set_metric("llm_router_kind", latency_route.kind)
                set_metric("llm_router_source", latency_route.source or "deterministic")
                set_metric("llm_router_category", latency_route.category)
                set_metric("llm_router_classifier_used", router_classifier_used)
            else:
                set_metric("llm_router_kind", latency_route.kind)
                set_metric("llm_router_source", "disabled")

            # 决定最终 vision_intent：关键词命中 OR 4b router 输出 V=vision。
            final_vision_intent = vision_intent_from_keywords or latency_route.category == "vision"
            if (
                final_vision_intent
                and vision_snapshot is None
                and vision_snapshot_client is None
            ):
                # 命中视觉意图但 vision 服务没配置：用 vision_unavailable 兜底。
                fallback_text = "我现在没有拿到最新画面，你再试一次好吗？"
                self.session_manager.add_message(session_id, "assistant", fallback_text)
                set_metric("llm_response_chars", len(fallback_text))
                set_metric("llm_vision_fetch_status", "not_configured")
                logger.info(
                    "视觉上下文不可用 (vision 未配置): session_id=%s source=%s",
                    session_id,
                    "router" if latency_route.category == "vision" else "keyword",
                )
                yield llm_service_pb2.ChatResponse(text=fallback_text, is_final=False)
                yield final_response("vision_unavailable")
                return
            if (
                final_vision_intent
                and vision_snapshot is None
                and vision_snapshot_client is not None
            ):
                vision_intent = True
                set_metric("llm_vision_intent", True)
                vision_fetch_started_at = time.monotonic()
                vision_result = await vision_snapshot_client.fetch_latest(session_id)
                set_metric(
                    "llm_vision_fetch_ms",
                    _elapsed_monotonic_ms(vision_fetch_started_at),
                )
                set_metric("llm_vision_fetch_status", vision_result.status)
                vision_snapshot = vision_result.snapshot
                if vision_snapshot is None:
                    fallback_text = "我现在没有拿到最新画面，你再试一次好吗？"
                    self.session_manager.add_message(session_id, "assistant", fallback_text)
                    set_metric("llm_response_chars", len(fallback_text))
                    logger.info(
                        "视觉上下文不可用: session_id=%s status=%s http_status=%s source=%s",
                        session_id,
                        vision_result.status,
                        vision_result.http_status,
                        "router" if latency_route.category == "vision" else "keyword",
                    )
                    yield llm_service_pb2.ChatResponse(text=fallback_text, is_final=False)
                    yield final_response("vision_unavailable")
                    return

                set_metric("llm_vision_image_bytes", len(vision_snapshot.jpeg))
                set_metric("llm_vision_image_age_ms", vision_snapshot.age_ms)
                set_metric("llm_vision_frame_id", vision_snapshot.frame_id)
                if messages and messages[0].get("role") == "system":
                    messages[0] = {
                        "role": "system",
                        "content": f"{messages[0].get('content') or ''}\n\n{VISION_PROMPT}",
                    }
                model_user_text = user_text_for_model if audio_context else user_text
                messages = _replace_latest_user_message(
                    messages,
                    model_user_text,
                    build_visual_user_content(model_user_text, vision_snapshot),
                )
                logger.info(
                    "视觉上下文已附加: session_id=%s frame_id=%s bytes=%s age_ms=%s source=%s",
                    session_id,
                    vision_snapshot.frame_id or "<none>",
                    len(vision_snapshot.jpeg),
                    vision_snapshot.age_ms,
                    "router" if latency_route.category == "vision" else "keyword",
                )
                # 已获取 vision snapshot，覆盖 latency_route 为 vision_context。
                latency_route = ToolLatencyRoute(
                    "chat",
                    source="vision_context",
                    category="vision",
                )
                set_metric("llm_router_kind", latency_route.kind)
                set_metric("llm_router_source", latency_route.source)
                set_metric("llm_router_category", latency_route.category)

            assistant_response = ""

            if latency_route.kind == "exit":
                logger.info("工具 Router 语义命中整会话退出: session_id=%s", session_id)
                exit_text = build_fixed_exit_response(user_text, session_id)
                set_metric("llm_response_chars", len(exit_text or ""))
                yield llm_service_pb2.ChatResponse(
                    text=exit_text,
                    is_final=False,
                )
                yield final_response("router_exit")
                return

            if latency_route.kind == "tool" and not tools:
                unavailable_text = _unavailable_tool_category_response(latency_route.category)
                self.session_manager.add_message(session_id, "assistant", unavailable_text)
                set_metric("llm_response_chars", len(unavailable_text))
                logger.info(
                    "工具 Router 命中但当前 Bot 无可用工具: category=%s",
                    latency_route.category or "<none>",
                )
                yield llm_service_pb2.ChatResponse(text=unavailable_text, is_final=False)
                yield final_response("tool_unavailable")
                return

            if (
                latency_route.kind == "chat"
                and (latency_route.category or "").startswith("unavailable_")
            ):
                # 4B 路由命中了 category（如 robot/websearch）但当前 Bot 无对应工具，
                # llm_router_async 返回 chat + unavailable_<category> 走友好提示。
                unavailable_text = _unavailable_tool_category_response(latency_route.category)
                self.session_manager.add_message(session_id, "assistant", unavailable_text)
                set_metric("llm_response_chars", len(unavailable_text))
                logger.info(
                    "工具 Router 命中类别但当前 Bot 无匹配工具: category=%s",
                    latency_route.category or "<none>",
                )
                yield llm_service_pb2.ChatResponse(text=unavailable_text, is_final=False)
                yield final_response("router_unavailable")
                return

            if latency_route.kind == "chat":
                llm_stream_started_at = time.monotonic()
                first_text_recorded = False
                async for chunk in _iter_llm_client_stream(
                    self.llm_client,
                    messages,
                    None,
                    temperature,
                    max_tokens,
                    model_name,
                ):
                    if chunk.get("type") != "text":
                        continue
                    chunk_content = chunk.get("content", "")
                    limited_text = apply_response_char_budget(chunk_content)
                    if not limited_text:
                        if response_char_budget_exhausted():
                            break
                        continue
                    if limited_text and not first_text_recorded:
                        first_text_recorded = True
                        set_metric("llm_first_text_ms", _elapsed_monotonic_ms(llm_stream_started_at))
                    assistant_response += limited_text
                    yield llm_service_pb2.ChatResponse(
                        text=limited_text,
                        is_final=False,
                    )
                    if response_char_budget_exhausted():
                        break

                assistant_response_for_history = _strip_standalone_tool_tags(assistant_response)
                if assistant_response_for_history:
                    self.session_manager.add_message(session_id, "assistant", assistant_response_for_history)

                set_metric("llm_response_chars", len(assistant_response_for_history))
                yield final_response("chat_bypass_tools")
                logger.info("工具首响实验: 普通聊天已绕过工具编排")
                return

            defer_singing_progress = (
                latency_route.kind == "tool"
                and latency_route.category == "singing"
                and bool(latency_route.progress_text)
            )
            singing_progress_emitted = False
            if (
                latency_route.kind == "tool"
                and latency_route.progress_text
                and not defer_singing_progress
            ):
                set_metric("llm_first_text_ms", _elapsed_monotonic_ms(request_started_at))
                yield llm_service_pb2.ChatResponse(
                    text=latency_route.progress_text,
                    is_final=False,
                )

            require_tool_call = False
            round_tool_choice = None
            first_round_tools = tools
            followup_tools_for_route = tools
            selected_tools: list[dict] = []
            if tools and latency_route.kind == "tool":
                if latency_route.selected_tool_name:
                    selected_tools = _filter_tools_by_name(tools, latency_route.selected_tool_name)
                    if selected_tools:
                        first_round_tools = selected_tools
                    set_metric("llm_selected_tool_name", latency_route.selected_tool_name)
                    set_metric("llm_first_round_tools_count", len(first_round_tools) if first_round_tools else 0)
                    logger.info(
                        "本轮命中确定性工具策略: source=%s category=%s selected_tool=%s tools=%s",
                        latency_route.source or "deterministic",
                        latency_route.category or "<none>",
                        latency_route.selected_tool_name,
                        len(first_round_tools) if first_round_tools else 0,
                    )
                elif latency_route.tool_prefix:
                    first_round_tools = _filter_tools_by_prefix(tools, latency_route.tool_prefix)
                    set_metric("llm_first_round_tools_count", len(first_round_tools) if first_round_tools else 0)
                    logger.info(
                        "本轮命中工具类别路由: source=%s category=%s prefix=%s tools=%s",
                        latency_route.source or "deterministic",
                        latency_route.category or "<none>",
                        latency_route.tool_prefix,
                        len(first_round_tools),
                    )
                require_tool_call = latency_route.require_tool_call
                if require_tool_call and first_round_tools:
                    # 不传 forced_function / required 强制 tool_choice:
                    # 9B (qwen3-5-9b) 在 streaming 模式下，tool_choice=forced_function
                    # 或 "required" 时 vLLM 端点有 bug，会把 tool_call args 写到
                    # content 字段而不是 tool_calls 字段，导致后续 tool_calls=[] 被
                    # _no_tool_call_fallback_message 拦截返回兜底。
                    # 正确做法: 让 9B 自己选工具 (streaming + 无 tool_choice 时正常)，
                    # selected_tool_name 已通过 first_round_tools 收窄到单一工具，9B
                    # 没得选。如果 9B 选错，由 d0236fc7 加的
                    # _build_robot_fallback_tool_call / _build_task_fallback_tool_call
                    # 兜底。实测 9B 单轮 98% / 多轮 75% 选对。
                    set_metric("llm_tool_choice_mode", "none")
                    messages = _append_required_tool_instruction(
                        messages,
                        selected_tool_name=latency_route.selected_tool_name,
                    )
                if latency_route.category != "complex" and first_round_tools:
                    followup_tools_for_route = [] if len(first_round_tools) == 1 else first_round_tools
                set_metric("llm_followup_tools_count", len(followup_tools_for_route) if followup_tools_for_route else 0)
                if followup_tools_for_route is not tools:
                    logger.info(
                        "本轮后续工具范围已收窄: followup_tools=%s",
                        len(followup_tools_for_route),
                    )

            async def tool_executor(tool_name: str, tool_args: dict) -> str:
                """执行 MCP 工具（带超时控制）"""
                resolved_tool_name = mcp_manager.resolve_tool_name(tool_name)
                final_tool_args = dict(tool_args or {})
                # _meta 只接受服务端权威上下文。先移除模型可能生成的同名参数，
                # 再按工具边界注入 Robot 审计或 Singing 当前音色上下文。
                final_tool_args.pop("_meta", None)
                final_tool_args = _limit_websearch_args(
                    resolved_tool_name,
                    _find_llm_tool_schema(tools, tool_name) or _find_llm_tool_schema(tools, resolved_tool_name),
                    final_tool_args,
                )
                if _is_create_alarm_tool(resolved_tool_name):
                    formatted_time = _format_alarm_datetime_for_tool(
                        final_tool_args.get("alarm_time"),
                    )
                    if formatted_time:
                        final_tool_args["alarm_time"] = formatted_time
                set_metric(
                    "llm_tool_call_count",
                    int(llm_metrics.get("llm_tool_call_count") or 0) + 1,
                )
                if resolved_tool_name in session_robot_id_tool_names:
                    if not robot_id:
                        logger.error("工具缺少会话 robot_id，拒绝执行: %s", resolved_tool_name)
                        return "我这边暂时无法确认当前机器人身份，所以没有执行这个操作。"
                    llm_robot_id = final_tool_args.get("robot_id")
                    if llm_robot_id and str(llm_robot_id).strip() != robot_id:
                        logger.warning(
                            "覆盖 LLM 生成的 robot_id: tool=%s llm_robot_id=%s session_robot_id=%s",
                            resolved_tool_name,
                            llm_robot_id,
                            robot_id,
                        )
                    final_tool_args["robot_id"] = robot_id
                    if _is_robot_tool(resolved_tool_name):
                        audit_meta = {}
                        if session_id:
                            audit_meta["session_id"] = session_id
                        if trace_id:
                            audit_meta["trace_id"] = trace_id
                        if audit_meta:
                            final_tool_args["_meta"] = audit_meta
                elif _is_singing_tool(resolved_tool_name):
                    singing_meta = {
                        "tts_profile_id": bot.tts_profile_id,
                    }
                    if session_id:
                        singing_meta["session_id"] = session_id
                    if trace_id:
                        singing_meta["trace_id"] = trace_id
                    final_tool_args["_meta"] = singing_meta

                alarm_time_error = _validate_alarm_time_for_tool(resolved_tool_name, final_tool_args)
                if alarm_time_error:
                    logger.warning(
                        "拦截 create_alarm 时间参数: tool=%s args=%s",
                        resolved_tool_name,
                        final_tool_args,
                    )
                    return alarm_time_error

                logger.info("执行工具: %s, 参数: %s", resolved_tool_name, final_tool_args)
                tool_started_at = time.monotonic()
                is_websearch = _is_websearch_tool_name(resolved_tool_name)
                max_attempts = _WEBSEARCH_TOOL_MAX_ATTEMPTS if is_websearch else 1
                timeout_sec = _WEBSEARCH_TOOL_TIMEOUT_SEC if is_websearch else 30.0

                try:
                    result = None
                    for attempt in range(1, max_attempts + 1):
                        try:
                            call = (
                                mcp_manager.call_tool(
                                    tool_name,
                                    final_tool_args,
                                    max_retries=0,
                                )
                                if is_websearch
                                else mcp_manager.call_tool(tool_name, final_tool_args)
                            )
                            result = await asyncio.wait_for(call, timeout=timeout_sec)
                            break
                        except Exception as exc:
                            should_retry = (
                                is_websearch
                                and attempt < max_attempts
                                and _is_transient_websearch_error(exc)
                            )
                            if not should_retry:
                                raise
                            logger.warning(
                                "WebSearch 工具调用瞬态失败，准备重试一次: tool=%s "
                                "attempt=%s/%s timeout=%.1fs error=%s",
                                resolved_tool_name,
                                attempt,
                                max_attempts,
                                timeout_sec,
                                exc,
                            )
                            await asyncio.sleep(0)

                    tool_elapsed_ms = (time.monotonic() - tool_started_at) * 1000.0
                    set_metric(
                        "llm_tool_total_ms",
                        float(llm_metrics.get("llm_tool_total_ms") or 0.0) + tool_elapsed_ms,
                    )
                    set_metric(
                        "llm_tool_max_ms",
                        max(float(llm_metrics.get("llm_tool_max_ms") or 0.0), tool_elapsed_ms),
                    )
                    self.session_manager.add_tool_call(
                        session_id,
                        resolved_tool_name,
                        final_tool_args,
                        result,
                    )
                    logger.info("工具调用成功: %s elapsed=%.1fms", resolved_tool_name, tool_elapsed_ms)
                    return result
                except asyncio.TimeoutError:
                    tool_elapsed_ms = (time.monotonic() - tool_started_at) * 1000.0
                    set_metric(
                        "llm_tool_total_ms",
                        float(llm_metrics.get("llm_tool_total_ms") or 0.0) + tool_elapsed_ms,
                    )
                    set_metric(
                        "llm_tool_max_ms",
                        max(float(llm_metrics.get("llm_tool_max_ms") or 0.0), tool_elapsed_ms),
                    )
                    logger.error(
                        "工具调用超时: %s elapsed=%.1fms (单次超时 %.1f 秒)",
                        resolved_tool_name,
                        tool_elapsed_ms,
                        timeout_sec,
                    )
                    return _build_tool_failure_message(resolved_tool_name)
                except Exception as e:
                    tool_elapsed_ms = (time.monotonic() - tool_started_at) * 1000.0
                    set_metric(
                        "llm_tool_total_ms",
                        float(llm_metrics.get("llm_tool_total_ms") or 0.0) + tool_elapsed_ms,
                    )
                    set_metric(
                        "llm_tool_max_ms",
                        max(float(llm_metrics.get("llm_tool_max_ms") or 0.0), tool_elapsed_ms),
                    )
                    logger.error("工具调用失败: %s elapsed=%.1fms - %s", resolved_tool_name, tool_elapsed_ms, e)
                    return _build_tool_failure_message(resolved_tool_name)

            direct_tool_args = None
            if (
                latency_route.kind == "tool"
                and require_tool_call
                and latency_route.selected_tool_name
                and first_round_tools
                and len(first_round_tools) == 1
            ):
                resolved_candidate_name = mcp_manager.resolve_tool_name(latency_route.selected_tool_name)
                if _is_robot_tool(resolved_candidate_name):
                    direct_tool_args = infer_deterministic_robot_tool_args(
                        user_text,
                        latency_route.selected_tool_name,
                    )
                elif _is_singing_tool(resolved_candidate_name):
                    direct_tool_args = infer_deterministic_singing_tool_args(
                        user_text,
                        latency_route.selected_tool_name,
                    )

            if direct_tool_args is not None:
                resolved_direct_tool_name = mcp_manager.resolve_tool_name(latency_route.selected_tool_name)
                set_metric("llm_direct_tool_call", True)
                set_metric("llm_direct_tool_name", latency_route.selected_tool_name)
                logger.info(
                    "确定性工具直调: tool=%s args=%s",
                    latency_route.selected_tool_name,
                    direct_tool_args,
                )
                direct_tool_result = await tool_executor(
                    latency_route.selected_tool_name,
                    direct_tool_args,
                )
                direct_tool_text = "" if direct_tool_result is None else str(direct_tool_result)
                direct_speech_text = (
                    _authoritative_tool_result_response(
                        resolved_direct_tool_name,
                        direct_tool_result,
                    )
                    if _is_singing_tool(resolved_direct_tool_name)
                    else _direct_robot_result_to_speech(
                        resolved_direct_tool_name,
                        direct_tool_text,
                    )
                )
                if direct_speech_text:
                    limited_direct_speech_text = apply_response_char_budget(direct_speech_text)
                    if limited_direct_speech_text:
                        if defer_singing_progress:
                            set_metric("llm_first_text_ms", _elapsed_monotonic_ms(request_started_at))
                        assistant_response += limited_direct_speech_text
                        yield llm_service_pb2.ChatResponse(
                            text=limited_direct_speech_text,
                            is_final=False,
                        )
                else:
                    terminal_response = _terminal_exit_response_for_tool(
                        resolved_direct_tool_name,
                        direct_tool_result,
                    )
                    if terminal_response:
                        if defer_singing_progress and "[SINGING_PLAYBACK:" in terminal_response:
                            set_metric("llm_first_text_ms", _elapsed_monotonic_ms(request_started_at))
                            yield llm_service_pb2.ChatResponse(
                                text=latency_route.progress_text,
                                is_final=False,
                            )
                            singing_progress_emitted = True
                        yield llm_service_pb2.ChatResponse(
                            text=(
                                terminal_response
                                if "[SINGING_PLAYBACK:" in terminal_response
                                else "[EXIT]"
                            ),
                            is_final=False,
                        )

                assistant_response_for_history = _strip_standalone_tool_tags(
                    assistant_response or latency_route.progress_text or ""
                )
                if assistant_response_for_history:
                    self.session_manager.add_message(session_id, "assistant", assistant_response_for_history)

                set_metric(
                    "llm_response_chars",
                    len(assistant_response_for_history),
                )
                yield final_response(
                    "deterministic_singing_tool_direct"
                    if _is_singing_tool(resolved_direct_tool_name)
                    else "deterministic_robot_tool_direct"
                )
                logger.info("改进版流式对话请求处理完成")
                return

            llm_stream_started_at = time.monotonic()
            first_text_recorded = False
            async for chunk in self.llm_client.chat_with_tools(
                messages=messages,
                tools=first_round_tools,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_executor=tool_executor if tools else None,
                model_name=model_name,
                followup_tools=followup_tools_for_route,
                tool_choice=round_tool_choice,
                require_tool_call=require_tool_call,
                max_tool_rounds=LLM_MAX_TOOL_ROUNDS,
                emit_tool_wait_message=latency_route.kind != "tool",
                router_kind=latency_route.kind,
                router_source=latency_route.source or ("deterministic" if latency_route.kind == "tool" else "legacy"),
                router_category=latency_route.category,
                quick_reply_session_id=session_id,
            ):
                chunk_type = chunk.get("type")
                chunk_content = chunk.get("content")

                if chunk_type == "text":
                    # 返回文本片段
                    limited_text = apply_response_char_budget(chunk_content)
                    if not limited_text:
                        if response_char_budget_exhausted():
                            break
                        continue
                    if limited_text and not first_text_recorded:
                        first_text_recorded = True
                        set_metric("llm_first_text_ms", _elapsed_monotonic_ms(llm_stream_started_at))
                    assistant_response += limited_text
                    yield llm_service_pb2.ChatResponse(
                        text=limited_text,
                        is_final=False
                    )
                    if response_char_budget_exhausted():
                        break

                elif chunk_type == "tool_call":
                    # 工具调用信息（仅记录日志）
                    tool_name = chunk_content.get("name")
                    logger.info(f"LLM 调用工具: {tool_name}")

                elif chunk_type == "progress_text":
                    # 过程播报只给 TTS 播放，不写入会话历史
                    yield llm_service_pb2.ChatResponse(
                        text=chunk_content,
                        is_final=False,
                    )

                elif chunk_type == "tool_result":
                    # 工具结果（仅记录日志）
                    tool_name = chunk_content.get("tool_name")
                    logger.info(f"工具 {tool_name} 执行完成")

                elif chunk_type == "terminal_text":
                    # 权威工具结果或本地接管控制标记；不再交回模型改写。
                    terminal_text = str(chunk_content or "")
                    if terminal_text:
                        if (
                            defer_singing_progress
                            and not singing_progress_emitted
                            and "[SINGING_PLAYBACK:" in terminal_text
                        ):
                            set_metric("llm_first_text_ms", _elapsed_monotonic_ms(request_started_at))
                            yield llm_service_pb2.ChatResponse(
                                text=latency_route.progress_text,
                                is_final=False,
                            )
                            singing_progress_emitted = True
                        elif defer_singing_progress and not singing_progress_emitted:
                            set_metric("llm_first_text_ms", _elapsed_monotonic_ms(request_started_at))
                            singing_progress_emitted = True
                        yield llm_service_pb2.ChatResponse(
                            text=terminal_text,
                            is_final=False,
                        )
                        history_text = terminal_text.replace("[EXIT]", "")
                        history_text = re.sub(
                            r"\[SINGING_PLAYBACK:[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+\]",
                            "",
                            history_text,
                        )
                        assistant_response += history_text

                elif chunk_type == "error":
                    # 错误信息
                    logger.error("LLM 工具处理异常: %s", chunk_content)
                    error_text = "抱歉，我刚刚没处理好，你再试一次好吗？"
                    yield llm_service_pb2.ChatResponse(
                        text=error_text,
                        is_final=False
                    )

            # 保存 AI 回复到会话历史
            assistant_response_for_history = _strip_standalone_tool_tags(assistant_response)
            if assistant_response_for_history:
                self.session_manager.add_message(session_id, "assistant", assistant_response_for_history)

            # 发送最后标记
            set_metric("llm_response_chars", len(assistant_response_for_history))
            yield final_response("tool_or_legacy")

            logger.info("改进版流式对话请求处理完成")

        except Exception as e:
            logger.error(f"流式对话错误: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))

    async def _normalize_cognitive_answer_with_llm(
        self,
        text: str,
        *,
        model_name: str,
    ) -> str | None:
        """Use the configured LLM only when simple answer rules are uncertain."""
        messages = [
            {
                "role": "system",
                "content": (
                    "你只负责把用户对认知筛查问题的回答归一成一个选项。"
                    "只能输出：经常、偶尔、从不、不确定。不要解释。"
                ),
            },
            {
                "role": "user",
                "content": f"用户回答：{text}",
            },
        ]
        try:
            chunks = self.llm_client._call_llm(
                messages=messages,
                tools=None,
                temperature=0.0,
                max_tokens=8,
                model_name=model_name,
            )
            response = "".join(
                chunk.get("content", "")
                for chunk in chunks
                if chunk.get("type") == "text"
            ).strip()
        except Exception as exc:
            logger.warning("认知检测答案 LLM 归一失败: %s", exc)
            return None

        normalized = normalize_answer_by_rule(response)
        if normalized:
            return normalized
        if response in {"经常", "偶尔", "从不"}:
            return response
        return None

    async def _generate_agent_text(
        self,
        messages: list[dict[str, str]],
        *,
        model_name: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        chunks = self.llm_client._call_llm(
            messages=messages,
            tools=None,
            temperature=temperature,
            max_tokens=max_tokens,
            model_name=model_name,
        )
        return "".join(
            chunk.get("content", "")
            for chunk in chunks
            if chunk.get("type") == "text"
        ).strip()

    @staticmethod
    async def _ensure_mcp_servers_connected(mcp_manager, server_names: list[str]) -> None:
        for server_name in server_names:
            if server_name not in mcp_manager.servers:
                logger.warning(
                    "Bot 配置的 MCP Server %s 不存在，跳过。当前可用: %s",
                    server_name,
                    sorted(mcp_manager.servers.keys()),
                )
                continue
            try:
                await asyncio.wait_for(
                    mcp_manager.connect_server(server_name),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.error(f"连接 MCP Server {server_name} 超时")
                mcp_manager.mark_connection_error(server_name, "连接超时（超过 10 秒）")
            except Exception as e:
                logger.error(f"连接 MCP Server {server_name} 失败: {e}")
                mcp_manager.mark_connection_error(server_name, str(e) or e.__class__.__name__)


def _build_debug_threads_snapshot(stack_limit_chars: int = 12000) -> dict:
    frames = sys._current_frames()
    threads = []
    for thread in sorted(threading.enumerate(), key=lambda item: item.name):
        frame = frames.get(thread.ident)
        stack = "".join(traceback.format_stack(frame)) if frame is not None else ""
        if len(stack) > stack_limit_chars:
            stack = stack[-stack_limit_chars:]
        threads.append({
            "name": thread.name,
            "ident": thread.ident,
            "native_id": getattr(thread, "native_id", None),
            "daemon": thread.daemon,
            "alive": thread.is_alive(),
            "stack": stack,
        })
    return {
        "success": True,
        "pid": os.getpid(),
        "thread_count": len(threads),
        "threads": threads,
        "llm_streams": get_llm_stream_diagnostics(),
    }


def _install_signal_stack_dump() -> None:
    if not hasattr(signal, "SIGUSR1"):
        return
    try:
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
        logger.info("LLM 线程栈转储已注册: kill -USR1 %s", os.getpid())
    except Exception as exc:
        logger.warning("注册 SIGUSR1 线程栈转储失败: %s", exc)


async def serve(port=50053, host: str = "127.0.0.1"):
    """
    启动改进版 LLM gRPC 异步服务器

    Args:
        port: 监听端口
    """
    # 创建异步 gRPC 服务器
    server = grpc.aio.server(
        options=[
            ('grpc.max_send_message_length', 50 * 1024 * 1024),  # 50MB
            ('grpc.max_receive_message_length', 50 * 1024 * 1024),  # 50MB
            ('grpc.keepalive_time_ms', 30000),  # 30秒
            ('grpc.keepalive_timeout_ms', 10000),  # 10秒
        ]
    )

    servicer = ImprovedLLMServiceServicer()
    mcp_warmup = await servicer.runtime_state.wait_for_mcp_warmup()
    if not mcp_warmup.get("success", False):
        logger.warning("LLM 启动前 MCP warm up 未全部成功: %s", mcp_warmup)
    llm_service_pb2_grpc.add_LLMServiceServicer_to_server(servicer, server)
    if LLM_COMPLEX_WORKFLOW_ENABLED:
        workflow_service_pb2_grpc.add_WorkflowServiceServicer_to_server(
            servicer.build_workflow_service(),
            server,
        )
        logger.info("独立复杂任务 WorkflowService 已启用")
    else:
        logger.info("独立复杂任务 WorkflowService 未启用（默认关闭）")

    server.add_insecure_port(f'{host}:{port}')
    await server.start()

    loop = asyncio.get_running_loop()

    class _AdminActions:
        def get_config_status(self) -> dict:
            return servicer.get_config_status()

        def get_debug_threads(self) -> dict:
            return servicer.get_debug_threads()

        def validate_runtime_config(self, version: int | None = None) -> dict:
            future = asyncio.run_coroutine_threadsafe(servicer.async_validate_runtime_config(version), loop)
            return future.result(timeout=20.0)

        def reload_runtime_config(self, version: int | None = None) -> dict:
            future = asyncio.run_coroutine_threadsafe(servicer.async_reload_runtime_config(version), loop)
            return future.result(timeout=20.0)

        def reconnect_mcp_server(self, server_key: str) -> dict:
            future = asyncio.run_coroutine_threadsafe(servicer.async_reconnect_mcp_server(server_key), loop)
            return future.result(timeout=20.0)

    admin_server = ConfigAdminHTTPServer(LLM_ADMIN_BIND_HOST, LLM_ADMIN_PORT, _AdminActions())
    admin_server.start()
    _install_signal_stack_dump()

    logger.info("改进版 LLM gRPC 异步服务已启动")
    logger.info(f"监听地址: {host}:{port}")
    logger.info(f"内部管理接口: http://{LLM_ADMIN_BIND_HOST}:{LLM_ADMIN_PORT}")
    logger.info("准备接收客户端请求...")
    try:
        await server.wait_for_termination()
    except asyncio.CancelledError:
        logger.info("收到取消信号，开始关闭 LLM 服务")
    finally:
        admin_server.stop()
        await servicer.runtime_state.shutdown()
        try:
            await asyncio.shield(server.stop(0))
        except asyncio.CancelledError:
            pass


if __name__ == '__main__':
    """主入口"""
    port = LLM_GRPC_SERVER_PORT
    logger.info(f"启动改进版 LLM gRPC 服务器，监听地址: {LLM_GRPC_BIND_HOST}:{port}")
    try:
        asyncio.run(serve(port, LLM_GRPC_BIND_HOST))
    except KeyboardInterrupt:
        logger.info("LLM 服务已停止")
