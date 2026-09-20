"""
改进版 LLM 客户端

支持：
1. 工具调用后继续处理结果
2. 会话上下文管理
3. 多轮对话
"""

import asyncio
import concurrent.futures
import itertools
import inspect
import logging
import json
import re
import copy
import time
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Generator, Callable
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, DefaultHttpxClient, OpenAI
from config import (
    LLM_SYNC_STREAM_EXECUTOR_MAX_WORKERS,
    LLM_SYNC_STREAM_QUEUE_MAXSIZE,
)
from llm.tool_router import infer_deterministic_robot_tool_args
from singing.protocol import playback_control_from_tool_result
from voice_quick_replies import (
    SINGING_PROGRESS_TEXT,
    pick_quick_reply,
    pick_robot_action_reply,
)

logger = logging.getLogger(__name__)


VISUAL_DETECTION_TOOL_SUFFIXES = (
    ".detect_gesture",
    ".detect_pet",
    ".understand_environment",
)
TERMINAL_TOOL_EXIT_RESPONSES = (
    ((".patrol",), "好的，我开始巡逻。[EXIT]"),
    ((".recharge_robot", ".return_to_charge"), "好的，我去充电了。[EXIT]"),
    ((".create_map",), "好的，我开始创建地图。[EXIT]"),
    ((".dance",), "好呀，我开始跳舞。[EXIT]"),
    ((".call_video",), "好的，我开始呼叫视频电话。[EXIT]"),
    (VISUAL_DETECTION_TOOL_SUFFIXES, "好的，我开始看一下。[EXIT]"),
)

QWEN3_NO_THINKING_EXTRA_BODY = {
    "chat_template_kwargs": {
        "enable_thinking": False,
    },
}
QWEN3_NO_THINK_HINT = "/no_think"
_LLM_STREAM_END = object()
_LLM_STREAM_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=LLM_SYNC_STREAM_EXECUTOR_MAX_WORKERS,
    thread_name_prefix="llm-stream",
)
_LLM_STREAM_ID_COUNTER = itertools.count(1)
_ACTIVE_LLM_STREAMS_LOCK = threading.Lock()
_ACTIVE_LLM_STREAMS: Dict[int, Dict[str, Any]] = {}


def _monotonic_ms() -> float:
    return round(time.monotonic() * 1000.0, 1)


def get_llm_stream_diagnostics() -> Dict[str, Any]:
    """Return lightweight diagnostics for sync OpenAI stream bridge workers."""
    now = time.monotonic()
    with _ACTIVE_LLM_STREAMS_LOCK:
        active = []
        for stream_id, info in sorted(_ACTIVE_LLM_STREAMS.items()):
            started_at = info.get("started_at", now)
            item = dict(info)
            item["stream_id"] = stream_id
            item["age_ms"] = round((now - started_at) * 1000.0, 1)
            item.pop("started_at", None)
            active.append(item)
    return {
        "executor_max_workers": LLM_SYNC_STREAM_EXECUTOR_MAX_WORKERS,
        "queue_maxsize": LLM_SYNC_STREAM_QUEUE_MAXSIZE,
        "active_stream_count": len(active),
        "active_streams": active,
    }


def _register_active_llm_stream(
    *,
    client,
    messages: List[Dict[str, str]],
    tools: Optional[List[Dict]],
    model_name: Optional[str],
    tool_choice: Optional[Any],
    mode: str,
) -> int:
    stream_id = next(_LLM_STREAM_ID_COUNTER)
    effective_model = model_name or getattr(client, "model_name", "")
    prompt_chars = sum(len(str(message.get("content") or "")) for message in messages)
    with _ACTIVE_LLM_STREAMS_LOCK:
        _ACTIVE_LLM_STREAMS[stream_id] = {
            "mode": mode,
            "model": effective_model,
            "base_url": getattr(client, "base_url", ""),
            "prompt_chars": prompt_chars,
            "tools": len(tools or []),
            "tool_choice": str(tool_choice) if tool_choice is not None else "",
            "started_at": time.monotonic(),
            "created_ms": _monotonic_ms(),
            "thread_name": "",
            "thread_ident": None,
            "consumer_stopped": False,
            "producer_done": False,
            "response_seen": False,
            "stop_event_set": False,
        }
    return stream_id


def _update_active_llm_stream(stream_id: int, **fields: Any) -> None:
    with _ACTIVE_LLM_STREAMS_LOCK:
        info = _ACTIVE_LLM_STREAMS.get(stream_id)
        if info is not None:
            info.update(fields)


def _unregister_active_llm_stream(stream_id: int) -> None:
    with _ACTIVE_LLM_STREAMS_LOCK:
        _ACTIVE_LLM_STREAMS.pop(stream_id, None)


def _call_accepts_parameter(callable_obj, parameter_name: str) -> bool:
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    if parameter_name in parameters:
        return True
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


def call_llm_with_optional_controls(
    llm_client,
    *,
    messages: List[Dict[str, str]],
    tools: Optional[List[Dict]] = None,
    temperature: float = 0.7,
    max_tokens: int = 2000,
    model_name: Optional[str] = None,
    tool_choice: Optional[Any] = None,
    stop_event: Optional[threading.Event] = None,
    on_response: Optional[Callable[[Any], None]] = None,
):
    """Call an LLM client while passing cancellation hooks only when supported."""
    kwargs = {
        "messages": messages,
        "tools": tools,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "model_name": model_name,
        "tool_choice": tool_choice,
    }
    call_llm = llm_client._call_llm
    if stop_event is not None and _call_accepts_parameter(call_llm, "stop_event"):
        kwargs["stop_event"] = stop_event
    if on_response is not None and _call_accepts_parameter(call_llm, "on_response"):
        kwargs["on_response"] = on_response
    return call_llm(**kwargs)


def _chat_extra_body_for_model(model_name: str | None) -> Optional[Dict[str, Any]]:
    normalized = (model_name or "").lower()
    if "qwen3" not in normalized:
        return None
    return copy.deepcopy(QWEN3_NO_THINKING_EXTRA_BODY)


def _is_qwen3_model(model_name: str | None) -> bool:
    return "qwen3" in (model_name or "").lower()


def _chat_messages_for_model(
    messages: List[Dict[str, str]],
    model_name: str | None,
) -> List[Dict[str, str]]:
    if not _is_qwen3_model(model_name):
        return messages

    patched = copy.deepcopy(messages)
    if any(QWEN3_NO_THINK_HINT in str(message.get("content") or "") for message in patched):
        return patched

    for message in patched:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content') or ''}\n{QWEN3_NO_THINK_HINT}".strip()
            return patched

    for message in reversed(patched):
        if message.get("role") == "user":
            message["content"] = f"{message.get('content') or ''}{QWEN3_NO_THINK_HINT}"
            return patched

    patched.append({"role": "system", "content": QWEN3_NO_THINK_HINT})
    return patched


def _tool_name_matches(tool_name: str, suffix: str) -> bool:
    """兼容内部真实名 server.tool 与 LLM 安全名 server__tool。"""
    return (tool_name or "").endswith(suffix) or (tool_name or "").endswith(suffix.replace(".", "__"))


def _terminal_exit_response_for_tool(tool_name: str, tool_result: Any = None) -> Optional[str]:
    """长任务/本地接管类工具成功后，语音侧用固定短句结束本轮对话。"""
    if _tool_name_matches(tool_name, ".play_song"):
        if tool_result is None:
            return "[SINGING_TOOL_TERMINAL]"
        return playback_control_from_tool_result(_extract_tool_result_text(tool_result))
    for suffixes, response in TERMINAL_TOOL_EXIT_RESPONSES:
        if any(_tool_name_matches(tool_name, suffix) for suffix in suffixes):
            return response
    return None


def _authoritative_tool_result_response(tool_name: str, tool_result: Any) -> Optional[str]:
    """对权威只读工具直接返回原始事实，避免二次 LLM 改写日期或数值。"""
    singing_response = _singing_tool_result_response(tool_name, tool_result)
    if singing_response is not None:
        return singing_response
    authoritative_suffixes = (
        ".get_now_context",
        ".get_current_time",
        ".get_current_date",
        ".get_weekday",
        ".get_datetime_full",
        ".format_timestamp",
    )
    if not any(_tool_name_matches(tool_name, suffix) for suffix in authoritative_suffixes):
        return None
    if _tool_result_looks_failed(tool_result):
        return None
    text = _extract_tool_result_text(tool_result).strip()
    return text or None


def _singing_tool_result_response(tool_name: str, tool_result: Any) -> Optional[str]:
    if not any(
        _tool_name_matches(tool_name, suffix)
        for suffix in (".play_song", ".list_songs")
    ):
        return None
    try:
        payload = json.loads(_extract_tool_result_text(tool_result).strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    if kind == "singing_playback":
        return None
    if kind == "singing_unavailable":
        return str(payload.get("message") or "这首歌我还没学会，换一首好吗？")
    if kind == "singing_clarification":
        titles = [
            str(item.get("title") or "")
            for item in payload.get("candidates") or []
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
        if len(titles) >= 2:
            return f"你是想听《{titles[0]}》还是《{titles[1]}》？"
        return "我没有听清歌名，你可以再说一次吗？"
    if kind != "singing_catalog":
        return None
    songs = [item for item in payload.get("songs") or [] if isinstance(item, dict)]
    if not songs:
        return str(payload.get("message") or "这首歌我还没学会，换一首好吗？")
    titles = [f"《{str(item.get('title') or '')}》" for item in songs]
    query = str(payload.get("query") or "").strip()
    total = int(payload.get("total_available") or len(songs))
    if query and total == 1:
        return f"会呀，我会唱{titles[0]}。"
    joined = "、".join(titles)
    if bool(payload.get("has_more")) or total > len(songs):
        return f"我现在会唱 {total} 首歌，比如{joined}。你想听哪一首？"
    return f"我现在会唱{joined}。你想听哪一首？"


def _tool_result_error_flag(tool_result: Any) -> Optional[bool]:
    """优先使用 MCP 结构化错误标记，避免把 isError=False 误判为失败。"""
    for attr in ("isError", "is_error"):
        if hasattr(tool_result, attr):
            value = getattr(tool_result, attr)
            if isinstance(value, bool):
                return value

    if isinstance(tool_result, dict):
        for key in ("isError", "is_error", "error"):
            value = tool_result.get(key)
            if isinstance(value, bool):
                return value
    return None


def _extract_tool_result_text(tool_result: Any) -> str:
    """提取适合做失败兜底判断和日志摘要的文本，避免直接 stringify 整个 MCP 对象。"""
    if tool_result is None:
        return ""
    if isinstance(tool_result, str):
        return tool_result

    content = getattr(tool_result, "content", None)
    if content is None and isinstance(tool_result, dict):
        content = tool_result.get("content")

    parts = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
            else:
                text = getattr(item, "text", None)
            if text:
                parts.append(str(text))

    if parts:
        return "\n".join(parts)
    return str(tool_result)


def _summarize_tool_result(tool_result: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", _extract_tool_result_text(tool_result)).strip()
    if not text:
        text = type(tool_result).__name__
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _tool_result_looks_failed(tool_result: Any) -> bool:
    """兼容当前工具执行器返回自然语言失败文案的形态。"""
    error_flag = _tool_result_error_flag(tool_result)
    if error_flag is not None:
        return error_flag

    text = _extract_tool_result_text(tool_result).lower()
    failure_markers = (
        "暂时没能",
        "失败",
        "不存在",
        "exception",
        "traceback",
    )
    return any(marker in text for marker in failure_markers)


def _build_tool_wait_message(
    tool_name: str,
    tool_args: Dict[str, Any],
    session_id: str | None = None,
) -> str:
    """为工具调用生成一条自然、简短的等待提示。"""
    name = tool_name or ""

    if _tool_name_matches(name, ".move_robot"):
        action = (tool_args or {}).get("action", "forward")
        mapping = {
            "forward": "move_forward",
            "backward": "move_backward",
            "turn_left": "turn_left",
            "turn_right": "turn_right",
            "stop": "stop",
        }
        return pick_robot_action_reply(mapping.get(action, "generic"), session_id)

    if _tool_name_matches(name, ".create_map"):
        return pick_robot_action_reply("generic", session_id)
    if _tool_name_matches(name, ".patrol"):
        return pick_robot_action_reply("generic", session_id)
    if _tool_name_matches(name, ".follow"):
        return pick_robot_action_reply("generic", session_id)
    if _tool_name_matches(name, ".recharge_robot") or _tool_name_matches(name, ".return_to_charge"):
        return pick_robot_action_reply("recharge", session_id)
    if _tool_name_matches(name, ".dance"):
        return pick_robot_action_reply("generic", session_id)
    if _tool_name_matches(name, ".play_song"):
        return SINGING_PROGRESS_TEXT
    if _tool_name_matches(name, ".detect_gesture"):
        return pick_robot_action_reply("generic", session_id)
    if _tool_name_matches(name, ".detect_pet"):
        return pick_robot_action_reply("generic", session_id)
    if _tool_name_matches(name, ".understand_environment"):
        return pick_robot_action_reply("generic", session_id)
    if _tool_name_matches(name, ".cancel_robot_task"):
        return pick_robot_action_reply("stop", session_id)
    if "task" in name.lower() or "alarm" in name.lower() or "remind" in name.lower():
        return pick_quick_reply("tool.task", session_id)
    if _tool_name_matches(name, ".get_weather"):
        return pick_quick_reply("tool.weather", session_id)
    if _tool_name_matches(name, ".get_now_context"):
        return pick_quick_reply("tool.utils", session_id)
    if _tool_name_matches(name, ".format_timestamp"):
        return pick_quick_reply("tool.utils", session_id)
    if "websearch" in name.lower():
        return pick_quick_reply("tool.websearch", session_id)

    return pick_quick_reply("tool.generic", session_id)


def _tool_needs_wait_message(tool_name: str) -> bool:
    """判断工具是否需要在执行前给用户一个阶段性反馈。"""
    name = (tool_name or "").lower()
    if "websearch" in name:
        return True
    if "weather" in name:
        return True
    if "task" in name or "alarm" in name or "remind" in name:
        return True
    if "robot" in name or "singing" in name:
        return True
    return False


def _build_tool_batch_wait_message(
    parsed_tool_calls: List[Dict[str, Any]],
    session_id: str | None = None,
) -> Optional[str]:
    """为同一轮工具批次生成一句等待语，避免多个工具逐个机械播报。"""
    announced_calls = [
        call for call in parsed_tool_calls
        if _tool_needs_wait_message(call.get("name", ""))
    ]
    if not announced_calls:
        return None

    tool_names = [call.get("name", "") for call in announced_calls]
    websearch_calls = [
        call for call in announced_calls
        if "websearch" in (call.get("name") or "").lower()
    ]
    if websearch_calls:
        return pick_quick_reply("tool.websearch", session_id)

    if any("task" in name.lower() or "alarm" in name.lower() or "remind" in name.lower() for name in tool_names):
        return pick_quick_reply("tool.task", session_id)

    robot_calls = [
        call for call in announced_calls
        if "robot" in (call.get("name") or "").lower()
    ]
    if robot_calls:
        if len(robot_calls) == 1:
            return _build_tool_wait_message(
                robot_calls[0].get("name", ""),
                robot_calls[0].get("args", {}),
                session_id,
            )
        return pick_robot_action_reply("generic", session_id)

    return _build_tool_wait_message(
        announced_calls[0].get("name", ""),
        announced_calls[0].get("args", {}),
        session_id,
    )


_NO_TOOL_CALL_FALLBACK_BY_CATEGORY: Dict[str, str] = {
    "vision": "我这边没拿到最新画面，你再说一次好吗？",
    "robot": "我这边暂时没能控制机器人，你再试一次好吗？",
    "websearch": "我这边暂时没能发起查询，请稍后再试。",
    "task": "我这边暂时没能设置提醒，你再试一次好吗？",
    "utils": "我这边暂时没拿到时间信息，你再说一次好吗？",
    "complex": "我这边没能完成这个请求，你再换一种方式问问看？",
}

_TASK_QUERY_KEYWORDS = (
    "有哪些",
    "有哪些提醒",
    "有哪",
    "待办",
    "提醒事项",
    "列出",
    "列表",
    "查下",
    "查看",
    "怎么回事",
)

_TASK_CREATE_KEYWORDS = (
    "添加",
    "创建",
    "设置",
    "新增",
    "记一条",
    "记一下",
    "提醒我",
    "我要",
    "去",
    "下午",
    "今天",
    "明天",
    "后天",
)

_TASK_TRIGGER_TIME_KEYWORDS = (
    "后",
    "后面",
    "后天",
    "明天",
    "今天",
)

_TASK_DEFAULT_TRIGGER_MINUTES = 5

_TASK_TIME_FIELD_CANDIDATES = (
    "trigger_time",
    "alarm_time",
    "time",
    "alarm_at",
    "due_time",
)

_TASK_CONTENT_FIELD_CANDIDATES = (
    "content",
    "title",
    "text",
    "name",
    "description",
    "detail",
)


_CN_NUM_MAP = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "两": 2,
    "廿": 20,
}


def _tool_name_to_reminder_type(tool_name: str) -> str:
    tool_name = (tool_name or "").lower().replace("__", ".")
    if any(
        suffix in tool_name
        for suffix in (
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
    ):
        return "create"
    if any(
        suffix in tool_name
        for suffix in (
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
    ):
        return "list"
    if ".alarm" in tool_name and ".create" in tool_name:
        return "create"
    if ".alarm" in tool_name and ".get" in tool_name:
        return "list"
    return ""


def _task_tool_supported_field(tool: Dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    function = tool.get("function") or {}
    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") or {}
    if not isinstance(properties, dict):
        return None
    for candidate in candidates:
        if candidate in properties:
            return candidate
    return None


def _build_task_time_text(user_text: str) -> str | None:
    return _parse_task_trigger_time(user_text)


def _build_task_default_content(user_text: str, extracted: str) -> str:
    if extracted:
        return extracted
    normalized = re.sub(r"\s+", "", user_text or "").lower()
    for token in _TASK_CREATE_KEYWORDS:
        normalized = normalized.replace(token, "")
    normalized = normalized.strip("。！？!?.,，；;：:~～…的了吧啊呀")
    return normalized if normalized else "提醒"


def _contains_task_tools(tools: Optional[List[Dict]]) -> bool:
    for tool in tools or []:
        tool_name = str((tool.get("function") or {}).get("name") or "")
        if _tool_name_to_reminder_type(tool_name):
            return True
    return False


def _normalize_router_category(category: Optional[str]) -> str:
    return (category or "").strip().lower()


def _build_task_default_trigger_time() -> str:
    now = datetime.now().astimezone()
    return (now + timedelta(minutes=_TASK_DEFAULT_TRIGGER_MINUTES)).replace(second=0, microsecond=0).isoformat()


def _contains_task_trigger_time(text: str) -> bool:
    for keyword in _TASK_TRIGGER_TIME_KEYWORDS:
        if keyword in text:
            return True
    return False


def _contains_any_task_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _cn_num_to_int(raw: str) -> int | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        try:
            return int(raw)
        except ValueError:
            return None
    if raw == "半":
        return 30
    if raw == "十":
        return 10
    if len(raw) == 1:
        return _CN_NUM_MAP.get(raw)
    if len(raw) == 2 and raw[0] == "十" and raw[1] in _CN_NUM_MAP:
        return 10 + _CN_NUM_MAP[raw[1]]
    if len(raw) == 2 and raw[1] == "十" and raw[0] in _CN_NUM_MAP:
        return _CN_NUM_MAP[raw[0]] * 10
    if len(raw) == 3 and raw[1] == "十" and raw[0] in _CN_NUM_MAP and raw[2] in _CN_NUM_MAP:
        return _CN_NUM_MAP[raw[0]] * 10 + _CN_NUM_MAP[raw[2]]
    if raw == "廿" or raw == "二十":
        return 20
    return None


def _parse_task_trigger_time(text: str) -> str | None:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return None
    now = datetime.now().astimezone()

    relative_minutes = re.search(r"(半|[0-9]+|[零一二三四五六七八九十廿两]+)\s*分钟后", normalized)
    if relative_minutes:
        raw_value = relative_minutes.group(1)
        minute_delta = 30 if raw_value == "半" else _cn_num_to_int(raw_value)
        if minute_delta is not None:
            return (now + timedelta(minutes=minute_delta)).isoformat()

    relative_hours = re.search(r"(半|[0-9]+|[零一二三四五六七八九十廿两]+)\s*小时后", normalized)
    if relative_hours:
        raw_value = relative_hours.group(1)
        hour_delta = 0.5 if raw_value == "半" else _cn_num_to_int(raw_value)
        if hour_delta is not None:
            return (now + timedelta(hours=hour_delta)).isoformat()

    day_offset = 0
    if "后天" in normalized:
        day_offset = 2
    elif "明天" in normalized:
        day_offset = 1

    abs_time = re.search(
        r"(上午|中午|下午|晚上|傍晚)?"
        r"([0-9一二三四五六七八九十廿两]{1,4})"
        r"点(?:(\d{1,2})|(?P<half>半)|:[ ]*(\d{1,2}))?"
        r"(?:分)?",
        normalized,
    )
    if abs_time:
        ampm = abs_time.group(1) or ""
        hour = _cn_num_to_int(abs_time.group(2))
        minute = (
            abs_time.group(3)
            if abs_time.group(3)
            else abs_time.group(5)
        )
        if hour is None:
            return None
        if minute in (None, ""):
            minute_value = 0
        elif minute == "半":
            minute_value = 30
        else:
            minute_value = int(minute)
        if "下午" in ampm or "晚上" in ampm or "傍晚" in ampm:
            if hour < 12:
                hour += 12
        if hour == 24:
            hour = 0
        target = (
            now
            + timedelta(days=day_offset)
        ).replace(hour=hour, minute=minute_value, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return target.isoformat()

    if not _contains_task_trigger_time(normalized):
        return None

    return None


def _extract_reminder_content(text: str) -> str:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return ""
    cleaned = normalized
    for token in (
        "请你",
        "请",
        "麻烦",
        "帮我",
        "给我",
        "帮忙",
        "你帮我",
        "设置",
        "设定",
        "创建",
        "添加",
        "一个",
        "个",
        "一下",
        "提醒我",
        "提醒",
        "闹钟",
        "定时",
        "任务",
        "去",
    ):
        cleaned = cleaned.replace(token, "")
    cleaned = re.sub(
        r"([0-9一二三四五六七八九十廿两半]{1,4}点(?:(?:[0-9]{1,2}|半)|:[ ]*[0-9]{1,2})?(?:分)?|"
        r"[0-9]+(?:小时|小时后|分钟|分钟后))",
        "",
        cleaned,
    )
    cleaned = cleaned.strip("。！？!?.,，；;：:~～…的了吧啊呀")
    return cleaned


def _build_task_fallback_tool_call(
    tools: Optional[List[Dict]],
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """当 task 场景下模型未产出 tool_call 时，做高置信单步直调。"""
    if not tools:
        return None

    user_text = _latest_user_text(messages)
    if not user_text:
        return None

    normalized = user_text.lower()
    create_tool = None
    list_tool = None
    for tool in tools:
        function = tool.get("function") or {}
        tool_name = str(function.get("name") or "")
        reminder_type = _tool_name_to_reminder_type(tool_name)
        if reminder_type == "create" and create_tool is None:
            create_tool = (tool_name, tool)
        elif reminder_type == "list" and list_tool is None:
            list_tool = (tool_name, tool)

    if not (create_tool or list_tool):
        return None

    if _contains_any_task_keyword(normalized, _TASK_QUERY_KEYWORDS) and list_tool:
        return {
            "type": "tool_call",
            "content": {
                "name": list_tool[0],
                "arguments": {},
            },
        }

    if not create_tool:
        return None

    content = _build_task_default_content(user_text, _extract_reminder_content(user_text))
    trigger_time = _build_task_time_text(user_text)
    if not trigger_time:
        if not _contains_any_task_keyword(normalized, _TASK_CREATE_KEYWORDS):
            return None
        trigger_time = _build_task_default_trigger_time()

    create_tool_name, create_tool_schema = create_tool
    content_field = _task_tool_supported_field(create_tool_schema, _TASK_CONTENT_FIELD_CANDIDATES)
    time_field = _task_tool_supported_field(create_tool_schema, _TASK_TIME_FIELD_CANDIDATES)

    if not content_field:
        content_field = "content"
    if not time_field:
        time_field = "trigger_time"

    arguments = {
        content_field: content,
        time_field: trigger_time,
    }

    required = ((create_tool_schema.get("function") or {}).get("parameters") or {}).get("required")
    if isinstance(required, list):
        for required_field in required:
            if required_field in arguments:
                continue
            if required_field in _TASK_CONTENT_FIELD_CANDIDATES:
                arguments[required_field] = "提醒"
            elif required_field in _TASK_TIME_FIELD_CANDIDATES:
                arguments[required_field] = trigger_time
            elif "time" in required_field or "date" in required_field or "alarm" in required_field:
                arguments[required_field] = trigger_time
            else:
                arguments[required_field] = content

    if _contains_any_task_keyword(normalized, _TASK_CREATE_KEYWORDS) and arguments:
        return {
            "type": "tool_call",
            "content": {
                "name": create_tool_name,
                "arguments": arguments,
            },
        }

    # 如果能明确识别为查询意图，兜底查询；否则不给出低质量创建参数。
    if list_tool and (
        "查询" in normalized
        or "列出" in normalized
        or "有哪些" in normalized
        or "待办" in normalized
    ):
        return {
            "type": "tool_call",
            "content": {
                "name": list_tool[0],
                "arguments": {},
            },
        }

    if list_tool and _contains_any_task_keyword(normalized, _TASK_QUERY_KEYWORDS):
        return {
            "type": "tool_call",
            "content": {
                "name": list_tool[0],
                "arguments": {},
            },
        }

    return None


def _no_tool_call_fallback_message(
    router_category: Optional[str],
    force_websearch_tool: bool,
) -> str:
    """当本轮策略要求必须调用工具但模型没生成 tool_call 时的兜底文本。

    根据 router_category 选最贴近用户场景的措辞；优先使用 force_websearch_tool 标识。
    """
    if force_websearch_tool:
        return _NO_TOOL_CALL_FALLBACK_BY_CATEGORY["websearch"]
    if router_category and router_category in _NO_TOOL_CALL_FALLBACK_BY_CATEGORY:
        return _NO_TOOL_CALL_FALLBACK_BY_CATEGORY[router_category]
    return "我这边没能完成这个请求，你再试一次好吗？"


def _collect_tool_calls(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [chunk for chunk in chunks if chunk.get("type") == "tool_call"]


def _iter_non_tool_chunks(chunks: List[Dict[str, Any]]):
    """过滤掉工具调用元信息，只保留应展示给用户的 chunk。"""
    for chunk in chunks:
        if chunk.get("type") != "tool_call":
            yield chunk


def _parse_tool_call(tool_call_chunk: Dict[str, Any]) -> Dict[str, Any]:
    tool_info = tool_call_chunk.get("content") or {}
    tool_name = tool_info.get("name")
    tool_args_str = tool_info.get("arguments")

    try:
        tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
    except json.JSONDecodeError:
        logger.error(f"工具参数 JSON 解析失败: {tool_args_str}")
        tool_args = {}

    return {
        "name": tool_name,
        "args": tool_args or {},
    }


def _latest_user_text(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            text = " ".join(
                str(item.get("text") or "").strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
        else:
            text = str(content or "").strip()
        if "\n用户说：" in text:
            text = text.rsplit("\n用户说：", 1)[-1].strip()
        if text:
            return text
    return ""


def _build_websearch_fallback_tool_call(
    tools: Optional[List[Dict]],
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """模型未产出 tool_call 时，仅对唯一且可安全填参的搜索工具进行兜底直调。"""
    candidates = []
    query_arg_names = ("query", "search_query", "q", "keyword", "keywords")
    for tool in tools or []:
        function = tool.get("function") or {}
        name = str(function.get("name") or "")
        if "websearch" not in name.lower():
            continue
        properties = ((function.get("parameters") or {}).get("properties") or {})
        query_arg = next((key for key in query_arg_names if key in properties), None)
        if query_arg:
            candidates.append((name, query_arg))

    query = _latest_user_text(messages)
    if len(candidates) != 1 or not query:
        return None

    tool_name, query_arg = candidates[0]
    return {
        "type": "tool_call",
        "content": {
            "name": tool_name,
            "arguments": {query_arg: query},
        },
    }


def _build_robot_fallback_tool_call(
    tools: Optional[List[Dict]],
    messages: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """模型未产出 robot 的 tool_call 时，基于单轮文本做可控的兜底直调。"""
    if not tools:
        return None

    user_text = _latest_user_text(messages)
    if not user_text:
        return None

    inferred_calls = []
    for tool in tools:
        function = tool.get("function") or {}
        tool_name = str(function.get("name") or "")
        if "robot" not in tool_name.lower():
            continue
        tool_args = infer_deterministic_robot_tool_args(user_text, tool_name)
        if tool_args is None:
            continue
        inferred_calls.append((tool_name, tool_args))

    if not inferred_calls:
        return None
    if len(inferred_calls) == 1:
        tool_name, tool_args = inferred_calls[0]
    elif len(tools) == 1:
        tool_name, tool_args = inferred_calls[0]
    else:
        specific_calls = [item for item in inferred_calls if item[1]]
        if len(specific_calls) == 1:
            tool_name, tool_args = specific_calls[0]
        else:
            move_calls = [
                item
                for item in inferred_calls
                if ".move_robot" in item[0]
            ]
            if not move_calls:
                return None
            tool_name, tool_args = move_calls[0]

    return {
        "type": "tool_call",
        "content": {
            "name": tool_name,
            "arguments": tool_args,
        },
    }


def _summarize_tool_calls(parsed_tool_calls: List[Dict[str, Any]]) -> str:
    """生成适合日志展示的工具调用摘要，避免整段工具结果污染日志。"""
    summaries = []
    for tool_call in parsed_tool_calls:
        tool_name = tool_call.get("name") or "<unknown>"
        tool_args = tool_call.get("args") or {}
        try:
            args_text = json.dumps(tool_args, ensure_ascii=False)
        except TypeError:
            args_text = str(tool_args)
        if len(args_text) > 300:
            args_text = args_text[:300] + "..."
        summaries.append(f"{tool_name} args={args_text}")
    return "; ".join(summaries) if summaries else "<none>"


class ImprovedQwenLLMClient:
    """
    改进版 Qwen LLM 客户端

    主要改进：
    - 支持工具调用后继续处理
    - 支持会话历史
    - 完整的对话流程
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = "qwen3-5-9b",
        base_url: str = None,
        client: Any = None,
        http_client: Any = None,
        async_client: Any = None,
        async_http_client: Any = None,
        timeout: float = 60.0,
    ):
        """
        初始化 LLM 客户端

        Args:
            api_key: API 密钥
            model_name: 模型名称
            base_url: API 基础 URL
        """
        self.api_key = api_key
        self.model_name = model_name
        self.base_url = base_url or "http://127.0.0.1:15101/v1"
        self.client = client or OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=http_client or DefaultHttpxClient(
                timeout=timeout,
                trust_env=False,
            ),
        )
        self.async_client = async_client or AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=async_http_client or DefaultAsyncHttpxClient(
                timeout=timeout,
                trust_env=False,
            ),
        )

        logger.info(f"改进版 Qwen LLM 客户端已初始化: model={self.model_name}")

    def _stream_chat_completions(self, params: Dict[str, Any]):
        return self.client.chat.completions.create(**params)

    async def _async_stream_chat_completions(self, params: Dict[str, Any]):
        return await self.async_client.chat.completions.create(**params)

    async def chat_with_tools(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        tool_executor: Optional[callable] = None,
        model_name: Optional[str] = None,
        followup_tools: Optional[List[Dict]] = None,
        tool_choice: Optional[Any] = None,
        require_tool_call: bool = False,
        max_tool_rounds: int = 5,
        emit_tool_wait_message: bool = True,
        router_kind: Optional[str] = None,
        router_source: Optional[str] = None,
        router_category: Optional[str] = None,
        quick_reply_session_id: Optional[str] = None,
    ):
        """
        支持工具调用的完整对话流程

        Args:
            messages: 对话历史 [{"role": "user", "content": "..."}]
            tools: 可用工具列表
            temperature: 温度参数
            max_tokens: 最大 token 数
            tool_executor: 工具执行函数 async def(tool_name, tool_args) -> str
            model_name: 模型名称（可选，默认使用客户端初始化时的模型）
            followup_tools: 后续工具轮使用的完整工具列表
            tool_choice: 工具选择策略，透传给 OpenAI-compatible API
            require_tool_call: 是否要求本轮必须产生工具调用
            max_tool_rounds: 最大工具决策轮数，防止无限循环
            emit_tool_wait_message: 是否在工具执行前生成等待播报
            router_kind: 路由类型，仅用于日志观测
            router_source: 路由来源，仅用于日志观测
            router_category: 路由工具类别，仅用于日志观测
            quick_reply_session_id: 快速回复防重复使用的会话标识

        Yields:
            Dict: {"type": "text", "content": "..."} 或
                  {"type": "progress_text", "content": "..."} 或
                  {"type": "tool_call", "content": {...}}
        """
        logger.info(
            "开始对话: messages=%s, tools=%s, router_kind=%s, router_source=%s, router_category=%s",
            len(messages),
            len(tools) if tools else 0,
            router_kind or "<none>",
            router_source or "<none>",
            router_category or "<none>",
        )

        # 使用传入的 model_name 或默认值
        effective_model = model_name or self.model_name
        effective_max_tool_rounds = max(1, max_tool_rounds)
        working_messages = list(messages)
        all_tools = followup_tools if followup_tools is not None else tools

        async def _handle_tool_calls(
            collected_tool_calls: List[Dict[str, Any]],
            round_index: int,
        ):
            tool_results = []
            parsed_tool_calls = [
                _parse_tool_call(tool_call_chunk)
                for tool_call_chunk in collected_tool_calls
            ]

            wait_message = (
                _build_tool_batch_wait_message(parsed_tool_calls, quick_reply_session_id)
                if emit_tool_wait_message
                else None
            )
            if wait_message:
                yield {
                    "type": "progress_text",
                    "content": wait_message,
                }

            for tool_call in parsed_tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                logger.info(f"执行工具: {tool_name}")
                tool_started_at = time.monotonic()

                try:
                    tool_result = await tool_executor(tool_name, tool_args)
                    tool_elapsed_ms = (time.monotonic() - tool_started_at) * 1000.0
                    tool_results.append((tool_name, tool_result))
                    logger.info("工具执行完成: tool=%s elapsed=%.1fms", tool_name, tool_elapsed_ms)
                    yield {
                        "type": "tool_result",
                        "content": {
                            "tool_name": tool_name,
                            "result": tool_result
                        }
                    }
                    terminal_response = _terminal_exit_response_for_tool(tool_name, tool_result)
                    tool_failed = _tool_result_looks_failed(tool_result)
                    logger.info(
                        "工具结果判定: tool=%s, terminal=%s, failed=%s, summary=%s",
                        tool_name,
                        bool(terminal_response),
                        tool_failed,
                        _summarize_tool_result(tool_result),
                    )
                    authoritative_response = _authoritative_tool_result_response(
                        tool_name,
                        tool_result,
                    )
                    if (
                        authoritative_response
                        and router_category in {"utils", "singing"}
                        and len(parsed_tool_calls) == 1
                    ):
                        logger.info("权威只读工具直接返回原始结果: %s", tool_name)
                        yield {
                            "type": "terminal_text",
                            "content": authoritative_response,
                        }
                        return
                    if terminal_response and not tool_failed:
                        logger.info("终止型工具已触发，结束当前语音对话: %s", tool_name)
                        yield {
                            "type": "terminal_text",
                            "content": terminal_response,
                        }
                        return
                    if terminal_response and tool_failed:
                        logger.warning("终止型工具结果疑似失败，不触发退出: %s", tool_name)

                except Exception as e:
                    tool_elapsed_ms = (time.monotonic() - tool_started_at) * 1000.0
                    logger.error("工具执行失败: tool=%s elapsed=%.1fms error=%s", tool_name, tool_elapsed_ms, e)
                    yield {
                        "type": "error",
                        "content": f"工具执行失败: {str(e)}"
                    }

            if tool_results:
                result_blocks = [
                    f"[系统：工具 {tool_name} 返回结果]\n{tool_result}"
                    for tool_name, tool_result in tool_results
                ]
                working_messages.append({
                    "role": "user",
                    "content": (
                        "\n\n".join(result_blocks)
                        + "\n\n请根据这些工具结果继续处理用户请求。"
                        "如果用户请求中还有未完成的条件动作或后续任务，请继续调用合适的工具；"
                        "对于多城市、多条件、多步骤任务，请在完成所有必要工具调用后再统一总结；"
                        "不要在提醒、机器人动作等条件任务尚未真实调用工具前声称已经完成。"
                        "如果已经完成全部必要工具调用，请直接给出最终口语化回答。"
                    ),
                })

        for round_index in range(1, effective_max_tool_rounds + 1):
            normalized_router_category = _normalize_router_category(router_category)
            round_tools = tools if round_index == 1 else all_tools
            round_tool_choice = tool_choice if round_index == 1 else None
            round_require_tool_call = require_tool_call if round_index == 1 else False
            force_websearch_tool = (
                round_index == 1
                and round_require_tool_call
                and normalized_router_category == "websearch"
                and bool(round_tools)
            )
            force_robot_tool = (
                round_index == 1
                and round_require_tool_call
                and normalized_router_category == "robot"
                and bool(round_tools)
            )
            force_task_tool = (
                round_index == 1
                and round_require_tool_call
                and (
                    normalized_router_category == "task"
                    or _contains_task_tools(round_tools)
                )
                and bool(round_tools)
            )
            round_reason = "用户请求初始决策" if round_index == 1 else "基于上一轮工具结果继续决策"

            logger.info(
                "工具编排轮次 %s/%s: 开始 LLM 决策 (%s), tools=%s, tool_choice=%s",
                round_index,
                effective_max_tool_rounds,
                round_reason,
                len(round_tools) if round_tools else 0,
                round_tool_choice or "auto",
            )

            round_chunks = []
            llm_decision_started_at = time.monotonic()
            async for chunk in self.stream_call_llm(
                working_messages,
                round_tools,
                temperature,
                max_tokens,
                effective_model,
                tool_choice=round_tool_choice,
            ):
                round_chunks.append(chunk)

            tool_calls = _collect_tool_calls(round_chunks)
            llm_decision_elapsed_ms = (time.monotonic() - llm_decision_started_at) * 1000.0
            parsed_tool_calls = [
                _parse_tool_call(tool_call_chunk)
                for tool_call_chunk in tool_calls
            ]
            logger.info(
                "工具编排轮次 %s/%s: LLM 决策完成，elapsed=%.1fms, chunks=%s, tool_calls=%s, tools=[%s]",
                round_index,
                effective_max_tool_rounds,
                llm_decision_elapsed_ms,
                len(round_chunks),
                len(tool_calls),
                _summarize_tool_calls(parsed_tool_calls),
            )

            if force_websearch_tool and not tool_calls:
                logger.warning(
                    "WebSearch 首次决策未返回 tool_call，使用低温度强制重试一次"
                )
                retry_started_at = time.monotonic()
                retry_chunks = []
                async for chunk in self.stream_call_llm(
                    working_messages,
                    round_tools,
                    0.0,
                    max_tokens,
                    effective_model,
                    tool_choice=round_tool_choice,
                ):
                    retry_chunks.append(chunk)
                round_chunks = retry_chunks
                tool_calls = _collect_tool_calls(round_chunks)
                parsed_tool_calls = [
                    _parse_tool_call(tool_call_chunk)
                    for tool_call_chunk in tool_calls
                ]
                logger.info(
                    "WebSearch 强制重试完成: elapsed=%.1fms, chunks=%s, tool_calls=%s, tools=[%s]",
                    (time.monotonic() - retry_started_at) * 1000.0,
                    len(round_chunks),
                    len(tool_calls),
                    _summarize_tool_calls(parsed_tool_calls),
                )
                if not tool_calls:
                    fallback_tool_call = _build_websearch_fallback_tool_call(
                        round_tools,
                        working_messages,
                    )
                    if fallback_tool_call:
                        tool_calls = [fallback_tool_call]
                        round_chunks = [fallback_tool_call]
                        parsed_tool_calls = [_parse_tool_call(fallback_tool_call)]
                        logger.warning(
                            "WebSearch 模型重试仍未返回 tool_call，"
                            "已使用用户原问题构造唯一搜索工具调用: tools=[%s]",
                            _summarize_tool_calls(parsed_tool_calls),
                        )

            if force_robot_tool and not tool_calls:
                logger.warning(
                    "Robot 首次决策未返回 tool_call，尝试低温度强制重试一次"
                )
                retry_started_at = time.monotonic()
                retry_chunks = []
                async for chunk in self.stream_call_llm(
                    working_messages,
                    round_tools,
                    0.0,
                    max_tokens,
                    effective_model,
                    tool_choice=round_tool_choice,
                ):
                    retry_chunks.append(chunk)
                round_chunks = retry_chunks
                tool_calls = _collect_tool_calls(round_chunks)
                parsed_tool_calls = [
                    _parse_tool_call(tool_call_chunk)
                    for tool_call_chunk in tool_calls
                ]
                logger.info(
                    "Robot 强制重试完成: elapsed=%.1fms, chunks=%s, tool_calls=%s, tools=[%s]",
                    (time.monotonic() - retry_started_at) * 1000.0,
                    len(round_chunks),
                    len(tool_calls),
                    _summarize_tool_calls(parsed_tool_calls),
                )
                if not tool_calls:
                    fallback_tool_call = _build_robot_fallback_tool_call(
                        round_tools,
                        working_messages,
                    )
                    if fallback_tool_call:
                        tool_calls = [fallback_tool_call]
                        round_chunks = [fallback_tool_call]
                        parsed_tool_calls = [_parse_tool_call(fallback_tool_call)]
                        logger.warning(
                            "Robot 模型重试仍未返回 tool_call，"
                            "已使用本地规则构造兜底工具调用: tools=[%s]",
                            _summarize_tool_calls(parsed_tool_calls),
                        )

            if force_task_tool and not tool_calls:
                logger.warning(
                    "Task 首次决策未返回 tool_call，尝试低温度强制重试一次"
                )
                retry_started_at = time.monotonic()
                retry_chunks = []
                async for chunk in self.stream_call_llm(
                    working_messages,
                    round_tools,
                    0.0,
                    max_tokens,
                    effective_model,
                    tool_choice=round_tool_choice,
                ):
                    retry_chunks.append(chunk)
                round_chunks = retry_chunks
                tool_calls = _collect_tool_calls(round_chunks)
                parsed_tool_calls = [
                    _parse_tool_call(tool_call_chunk)
                    for tool_call_chunk in tool_calls
                ]
                logger.info(
                    "Task 强制重试完成: elapsed=%.1fms, chunks=%s, tool_calls=%s, tools=[%s]",
                    (time.monotonic() - retry_started_at) * 1000.0,
                    len(round_chunks),
                    len(tool_calls),
                    _summarize_tool_calls(parsed_tool_calls),
                )
                if not tool_calls:
                    fallback_tool_call = _build_task_fallback_tool_call(
                        round_tools,
                        working_messages,
                    )
                    if fallback_tool_call:
                        tool_calls = [fallback_tool_call]
                        round_chunks = [fallback_tool_call]
                        parsed_tool_calls = [_parse_tool_call(fallback_tool_call)]
                        logger.warning(
                            "Task 模型重试仍未返回 tool_call，"
                            "已使用本地规则构造兜底任务工具调用: tools=[%s]",
                            _summarize_tool_calls(parsed_tool_calls),
                        )

            if tool_calls and tool_executor:
                logger.info(
                    "工具编排轮次 %s/%s: 执行本轮工具调用",
                    round_index,
                    effective_max_tool_rounds,
                )
                should_stop = False
                async for chunk in _handle_tool_calls(tool_calls, round_index):
                    if chunk.get("type") == "terminal_text":
                        yield {
                            "type": "text",
                            "content": chunk.get("content", ""),
                        }
                        should_stop = True
                    else:
                        yield chunk
                if should_stop:
                    return
                continue

            if round_require_tool_call:
                logger.warning("本轮策略要求工具调用，但模型未返回 tool_call，已拦截直接文本回复")
                yield {
                    "type": "text",
                    "content": _no_tool_call_fallback_message(
                        router_category=router_category,
                        force_websearch_tool=force_websearch_tool,
                    ),
                }
                return

            for chunk in _iter_non_tool_chunks(round_chunks):
                yield chunk
            logger.info(
                "工具编排轮次 %s/%s: 无需继续调用工具，进入最终回复",
                round_index,
                effective_max_tool_rounds,
            )
            return

        logger.warning("达到最大工具调用轮数 %s，开始生成最终回复", effective_max_tool_rounds)
        final_messages = list(working_messages)
        final_messages.append({
            "role": "user",
            "content": "请基于以上已经完成的工具结果，直接给出最终口语化回答，不要再调用工具。",
        })
        async for final_chunk in self.stream_call_llm(
            final_messages,
            None,
            temperature,
            max_tokens,
            effective_model,
        ):
            yield final_chunk

    async def _async_call_llm(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        model_name: Optional[str] = None,
        tool_choice: Optional[Any] = None,
    ):
        """Call the OpenAI-compatible provider with a native async stream."""
        response = None
        try:
            effective_model = model_name or self.model_name
            request_messages = _chat_messages_for_model(messages, effective_model)
            params = {
                "model": effective_model,
                "messages": request_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
            extra_body = _chat_extra_body_for_model(effective_model)
            if extra_body:
                params["extra_body"] = extra_body
            if tools:
                params["tools"] = tools
            if tool_choice is not None:
                params["tool_choice"] = tool_choice

            prompt_chars = sum(len(str(message.get("content") or "")) for message in request_messages)
            logger.info(
                "LLM async 请求参数: model=%s messages=%s prompt_chars=%s tools=%s temp=%s max_tokens=%s no_thinking=%s",
                effective_model,
                len(request_messages),
                prompt_chars,
                len(tools or []),
                temperature,
                max_tokens,
                bool(extra_body),
            )
            request_started_at = time.monotonic()
            response = await self._async_stream_chat_completions(params)

            tool_calls_accumulator = {}
            raw_chunks = 0
            content_chunks = 0
            reasoning_chunks = 0
            first_content_logged = False
            async for chunk in response:
                raw_chunks += 1
                delta = chunk.choices[0].delta
                reasoning_content = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                has_content = bool(getattr(delta, "content", None))
                has_tool_calls = bool(getattr(delta, "tool_calls", None))
                has_reasoning = bool(reasoning_content)
                if raw_chunks == 1:
                    logger.info(
                        "LLM async 首个原始流 chunk: model=%s elapsed=%.1fms content=%s reasoning=%s tool_calls=%s",
                        effective_model,
                        (time.monotonic() - request_started_at) * 1000.0,
                        has_content,
                        has_reasoning,
                        has_tool_calls,
                    )
                if has_reasoning:
                    reasoning_chunks += 1

                if has_content:
                    content_chunks += 1
                    if not first_content_logged:
                        first_content_logged = True
                        logger.info(
                            "LLM async 首个文本 chunk: model=%s elapsed=%.1fms chars=%s",
                            effective_model,
                            (time.monotonic() - request_started_at) * 1000.0,
                            len(delta.content),
                        )
                    yield {
                        "type": "text",
                        "content": delta.content,
                    }

                if has_tool_calls:
                    for tool_call in delta.tool_calls:
                        index = tool_call.index
                        if index not in tool_calls_accumulator:
                            tool_calls_accumulator[index] = {
                                "name": "",
                                "arguments": "",
                            }
                        if tool_call.function.name:
                            tool_calls_accumulator[index]["name"] = tool_call.function.name
                        if tool_call.function.arguments:
                            tool_calls_accumulator[index]["arguments"] += tool_call.function.arguments

            if tool_calls_accumulator:
                for tool_call_data in tool_calls_accumulator.values():
                    yield {
                        "type": "tool_call",
                        "content": tool_call_data,
                    }
            if _is_qwen3_model(effective_model) and reasoning_chunks:
                logger.warning(
                    "Qwen3 no-thinking async 请求仍收到 reasoning_content: model=%s raw_chunks=%s reasoning_chunks=%s content_chunks=%s",
                    effective_model,
                    raw_chunks,
                    reasoning_chunks,
                    content_chunks,
                )
            logger.info(
                "LLM async 流结束: model=%s raw_chunks=%s content_chunks=%s reasoning_chunks=%s",
                effective_model,
                raw_chunks,
                content_chunks,
                reasoning_chunks,
            )

        except asyncio.CancelledError:
            logger.info("LLM async provider stream 已取消")
            raise
        except Exception as e:
            logger.error("LLM async 调用失败: %s", e)
            yield {
                "type": "error",
                "content": str(e),
            }
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                    except Exception as exc:
                        logger.debug("关闭 LLM async provider stream 失败: %s", exc)

    async def stream_call_llm(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        model_name: Optional[str] = None,
        tool_choice: Optional[Any] = None,
    ):
        """Stream provider chunks without blocking the gRPC event loop."""
        if getattr(self, "async_client", None) is not None:
            stream_id = _register_active_llm_stream(
                client=self,
                messages=messages,
                tools=tools,
                model_name=model_name,
                tool_choice=tool_choice,
                mode="async",
            )
            _update_active_llm_stream(stream_id, thread_name="asyncio", thread_ident=None)
            try:
                async for chunk in self._async_call_llm(
                    messages,
                    tools,
                    temperature,
                    max_tokens,
                    model_name,
                    tool_choice=tool_choice,
                ):
                    _update_active_llm_stream(
                        stream_id,
                        response_seen=True,
                        response_seen_ms=_monotonic_ms(),
                    )
                    yield chunk
            finally:
                _update_active_llm_stream(
                    stream_id,
                    consumer_stopped=True,
                    stop_event_set=True,
                    producer_done=True,
                    consumer_stopped_ms=_monotonic_ms(),
                )
                _unregister_active_llm_stream(stream_id)
            return

        # Fallback for tests/custom clients that only implement the synchronous provider.
        loop = asyncio.get_running_loop()
        output_queue: asyncio.Queue = asyncio.Queue(maxsize=LLM_SYNC_STREAM_QUEUE_MAXSIZE)
        stop_event = threading.Event()
        response_lock = threading.Lock()
        response_holder: Dict[str, Any] = {}
        stream_id = next(_LLM_STREAM_ID_COUNTER)
        effective_model = model_name or self.model_name
        prompt_chars = sum(len(str(message.get("content") or "")) for message in messages)
        with _ACTIVE_LLM_STREAMS_LOCK:
            _ACTIVE_LLM_STREAMS[stream_id] = {
                "model": effective_model,
                "base_url": getattr(self, "base_url", ""),
                "prompt_chars": prompt_chars,
                "tools": len(tools or []),
                "tool_choice": str(tool_choice) if tool_choice is not None else "",
                "started_at": time.monotonic(),
                "created_ms": _monotonic_ms(),
                "thread_name": "",
                "thread_ident": None,
                "consumer_stopped": False,
                "producer_done": False,
                "response_seen": False,
                "stop_event_set": False,
            }

        def update_stream_info(**fields: Any) -> None:
            with _ACTIVE_LLM_STREAMS_LOCK:
                info = _ACTIVE_LLM_STREAMS.get(stream_id)
                if info is not None:
                    info.update(fields)

        def remember_response(response) -> None:
            with response_lock:
                response_holder["response"] = response
            update_stream_info(response_seen=True, response_seen_ms=_monotonic_ms())

        def close_active_response(reason: str) -> None:
            with response_lock:
                response = response_holder.get("response")
                if response is None or response_holder.get("closed"):
                    return
                response_holder["closed"] = True
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                    logger.info("LLM 流已主动关闭: reason=%s", reason)
                except Exception as exc:
                    logger.debug("主动关闭 LLM 流失败: reason=%s error=%s", reason, exc)

        def put_from_thread(item) -> bool:
            while not stop_event.is_set():
                future = asyncio.run_coroutine_threadsafe(output_queue.put(item), loop)
                try:
                    future.result(timeout=0.1)
                    return True
                except concurrent.futures.TimeoutError:
                    future.cancel()
                except Exception:
                    return False
            return False

        def produce_stream() -> None:
            current_thread = threading.current_thread()
            update_stream_info(
                thread_name=current_thread.name,
                thread_ident=current_thread.ident,
            )
            try:
                for chunk in call_llm_with_optional_controls(
                    self,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model_name=model_name,
                    tool_choice=tool_choice,
                    stop_event=stop_event,
                    on_response=remember_response,
                ):
                    if stop_event.is_set() or not put_from_thread(chunk):
                        return
            except Exception as exc:
                if stop_event.is_set():
                    logger.info("LLM 流在取消后结束: %s", exc)
                else:
                    put_from_thread(exc)
            finally:
                put_from_thread(_LLM_STREAM_END)
                update_stream_info(producer_done=True, producer_done_ms=_monotonic_ms())
                with _ACTIVE_LLM_STREAMS_LOCK:
                    _ACTIVE_LLM_STREAMS.pop(stream_id, None)

        producer_future = loop.run_in_executor(_LLM_STREAM_EXECUTOR, produce_stream)
        try:
            while True:
                item = await output_queue.get()
                if item is _LLM_STREAM_END:
                    break
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop_event.set()
            update_stream_info(
                consumer_stopped=True,
                stop_event_set=True,
                consumer_stopped_ms=_monotonic_ms(),
            )
            close_active_response("async_consumer_stopped")
            if not producer_future.done():
                producer_future.cancel()

    def _call_llm(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        model_name: Optional[str] = None,
        tool_choice: Optional[Any] = None,
        stop_event: Optional[threading.Event] = None,
        on_response: Optional[Callable[[Any], None]] = None,
    ) -> Generator[Dict, None, None]:
        """
        调用 LLM API

        Args:
            messages: 消息历史
            tools: 工具列表
            temperature: 温度
            max_tokens: 最大 token
            model_name: 模型名称（可选，默认使用客户端初始化时的模型）
            tool_choice: 工具选择策略（可选）

        Yields:
            Dict: 响应块
        """
        response = None
        try:
            if stop_event is not None and stop_event.is_set():
                logger.info("LLM 调用在发起前已被取消")
                return

            # 构建请求参数
            effective_model = model_name or self.model_name
            request_messages = _chat_messages_for_model(messages, effective_model)
            params = {
                "model": effective_model,
                "messages": request_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True
            }
            extra_body = _chat_extra_body_for_model(effective_model)
            if extra_body:
                params["extra_body"] = extra_body

            # 如果有工具，添加到请求
            if tools:
                params["tools"] = tools
            if tool_choice is not None:
                params["tool_choice"] = tool_choice

            # 调用 API
            prompt_chars = sum(len(str(message.get("content") or "")) for message in request_messages)
            logger.info(
                "LLM 请求参数: model=%s messages=%s prompt_chars=%s tools=%s temp=%s max_tokens=%s no_thinking=%s",
                effective_model,
                len(request_messages),
                prompt_chars,
                len(tools or []),
                temperature,
                max_tokens,
                bool(extra_body),
            )
            request_started_at = time.monotonic()
            response = self._stream_chat_completions(params)
            if on_response is not None:
                on_response(response)

            # 处理流式响应
            tool_calls_accumulator = {}
            raw_chunks = 0
            content_chunks = 0
            reasoning_chunks = 0
            first_content_logged = False
            for chunk in response:
                if stop_event is not None and stop_event.is_set():
                    logger.info("LLM 流收到取消标志，停止读取 provider stream")
                    break
                raw_chunks += 1
                delta = chunk.choices[0].delta
                reasoning_content = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
                has_content = bool(getattr(delta, "content", None))
                has_tool_calls = bool(getattr(delta, "tool_calls", None))
                has_reasoning = bool(reasoning_content)
                if raw_chunks == 1:
                    logger.info(
                        "LLM 首个原始流 chunk: model=%s elapsed=%.1fms content=%s reasoning=%s tool_calls=%s",
                        effective_model,
                        (time.monotonic() - request_started_at) * 1000.0,
                        has_content,
                        has_reasoning,
                        has_tool_calls,
                    )
                if has_reasoning:
                    reasoning_chunks += 1

                # 文本内容
                if has_content:
                    content_chunks += 1
                    if not first_content_logged:
                        first_content_logged = True
                        logger.info(
                            "LLM 首个文本 chunk: model=%s elapsed=%.1fms chars=%s",
                            effective_model,
                            (time.monotonic() - request_started_at) * 1000.0,
                            len(delta.content),
                        )
                    yield {
                        "type": "text",
                        "content": delta.content
                    }

                # 工具调用（累积）
                if has_tool_calls:
                    for tool_call in delta.tool_calls:
                        index = tool_call.index

                        # 初始化累积器
                        if index not in tool_calls_accumulator:
                            tool_calls_accumulator[index] = {
                                "name": "",
                                "arguments": ""
                            }

                        # 累积各个字段
                        if tool_call.function.name:
                            tool_calls_accumulator[index]["name"] = tool_call.function.name
                        if tool_call.function.arguments:
                            tool_calls_accumulator[index]["arguments"] += tool_call.function.arguments

            # 返回完整的工具调用
            if tool_calls_accumulator:
                for tool_call_data in tool_calls_accumulator.values():
                    yield {
                        "type": "tool_call",
                        "content": tool_call_data
                    }
            if _is_qwen3_model(effective_model) and reasoning_chunks:
                logger.warning(
                    "Qwen3 no-thinking 请求仍收到 reasoning_content: model=%s raw_chunks=%s reasoning_chunks=%s content_chunks=%s",
                    effective_model,
                    raw_chunks,
                    reasoning_chunks,
                    content_chunks,
                )
            logger.info(
                "LLM 流结束: model=%s raw_chunks=%s content_chunks=%s reasoning_chunks=%s",
                effective_model,
                raw_chunks,
                content_chunks,
                reasoning_chunks,
            )

        except Exception as e:
            if stop_event is not None and stop_event.is_set():
                logger.info("LLM provider stream 在取消后结束: %s", e)
                return
            logger.error(f"LLM 调用失败: {e}")
            yield {
                "type": "error",
                "content": str(e)
            }
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as exc:
                        logger.debug("关闭 LLM provider stream 失败: %s", exc)
