#!/usr/bin/env python3
"""独立 Gold 验收使用的 Router/Tool 请求与结果校验核心。"""

from __future__ import annotations

import copy
import ipaddress
import json
import sys
import time
from urllib.parse import urlparse
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from llm.tool_router import parse_llm_router_decision
from llm.llm_grpc_server import _build_tool_router_classifier_messages
import config

# ─── 配置 ──────────────────────────────────────────
ROUTER_BASE_URL = config.LLM_ROUTER_BASE_URL or config.LLM_BASE_URL
ROUTER_API_KEY = config.LLM_ROUTER_API_KEY or config.LLM_API_KEY
ROUTER_MODEL = config.LLM_ROUTER_MODEL_NAME or config.LLM_MODEL_NAME

GEN_BASE_URL = config.LLM_BASE_URL
GEN_API_KEY = config.LLM_API_KEY
GEN_MODEL = config.LLM_MODEL_NAME

def _should_bypass_proxy(base_url: str) -> bool:
    """内网和 localhost 评测必须直连，不能被系统代理转发。"""
    hostname = urlparse(base_url).hostname
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_private or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _openai_client(base_url: str, api_key: str, *, max_retries: int = 2) -> OpenAI:
    kwargs: dict[str, Any] = {
        "base_url": base_url,
        "api_key": api_key,
        "timeout": 30.0,
        "max_retries": max_retries,
    }
    if _should_bypass_proxy(base_url):
        kwargs["http_client"] = httpx.Client(trust_env=False)
    return OpenAI(**kwargs)


# ─── 9B 工具 schema (复用 case generator) ───────────
from mcp_servers.robot_sse_server import TOOLS_LIST as ROBOT_TOOL_CONTRACTS
from mcp_servers.utils_sse_server import TOOLS_LIST as UTILS_TOOL_CONTRACTS


def _to_openai_tool(prefix: str, contract: dict[str, Any]) -> dict[str, Any]:
    parameters = copy.deepcopy(contract["inputSchema"])
    # robot_id 由运行时按当前会话注入，不应由模型生成，也不属于 Gold 参数。
    parameters.get("properties", {}).pop("robot_id", None)
    if "required" in parameters:
        parameters["required"] = [item for item in parameters["required"] if item != "robot_id"]
    return {
        "type": "function",
        "function": {
            "name": f"{prefix}.{contract['name']}",
            "description": contract.get("description", ""),
            "parameters": parameters,
        },
    }


EVAL_TOOL_SCHEMAS = [
    *[_to_openai_tool("robot_remote", item) for item in ROBOT_TOOL_CONTRACTS],
    *[_to_openai_tool("utils_remote", item) for item in UTILS_TOOL_CONTRACTS],
]
# ─── 4B 路由测试 ─────────────────────────────────
@dataclass
class RouterResult:
    file: str
    text: str
    expected: str
    actual: str
    raw: str
    elapsed_ms: float
    ok: bool
    case_id: str = ""
    scenario: str = ""
    error: str = ""


def normalize_4b_decision(raw: str) -> str:
    """把 4B 输出的单字符 code 转为 expected 形式 (1 / 2:xxx / exit)。"""
    decision = parse_llm_router_decision(raw)
    if not decision:
        return "invalid"
    kind, category = decision
    if kind == "exit":
        return "exit"
    if kind == "chat":
        return "2:vision" if category == "vision" else "1"
    if kind == "tool":
        return f"2:{category}" if category else "2:complex"
    return "invalid"


def run_4b_router(client: OpenAI, file: str, case: dict) -> RouterResult:
    text = case.get("text", "")
    expected = case.get("expected", "1")
    history = case.get("history", [])
    try:
        # 支持多轮: 把 history 透传给 4B 分类器
        if history:
            messages = _build_tool_router_classifier_messages(text, history)
        else:
            messages = _build_tool_router_classifier_messages(text, None)
        started = time.monotonic()
        resp = client.chat.completions.create(
            model=ROUTER_MODEL,
            messages=messages,
            max_tokens=4,
            temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        elapsed = (time.monotonic() - started) * 1000
        raw = (resp.choices[0].message.content or "").strip()
        actual = normalize_4b_decision(raw)
        return RouterResult(
            file=file,
            text=text,
            expected=expected,
            actual=actual,
            raw=raw,
            elapsed_ms=elapsed,
            ok=(actual == expected),
            case_id=case.get("id", ""),
            scenario=case.get("scenario", ""),
        )
    except Exception as exc:
        return RouterResult(
            file=file,
            text=text,
            expected=expected,
            actual="error",
            raw="",
            elapsed_ms=0,
            ok=False,
            case_id=case.get("id", ""),
            scenario=case.get("scenario", ""),
            error=f"{type(exc).__name__}: {exc}",
        )


# ─── 9B 工具测试 ─────────────────────────────────
@dataclass
class ToolResult:
    file: str
    text: str
    expected_tool: str
    expected_args: dict
    actual_tool: str
    actual_args: dict
    actual_text: str
    elapsed_ms: float
    ok: bool
    case_id: str = ""
    tool_ok: bool = False
    args_ok: bool = False
    validation_errors: list[str] = field(default_factory=list)
    candidate_tools: list[str] = field(default_factory=list)
    scenario: str = ""
    error: str = ""


def _canonical_tool_name(value: str) -> str:
    return (value or "").replace("__", ".").rsplit(".", 1)[-1]


def _validate_tool_prediction(
    *,
    expected_tool: str,
    expected_args: dict,
    actual_tool: str,
    actual_args: dict,
    has_tool_calls: bool,
) -> tuple[bool, bool, list[str]]:
    if expected_tool == "any":
        return False, False, ["invalid_gold: expected_tool=any is forbidden"]
    if expected_tool in {"*", "no_tool"}:
        if has_tool_calls:
            return False, False, [f"unexpected_tool: {_canonical_tool_name(actual_tool)}"]
        return True, True, []
    if not has_tool_calls:
        return False, False, ["missing_tool_call"]
    expected_name = _canonical_tool_name(expected_tool)
    actual_name = _canonical_tool_name(actual_tool)
    if expected_name != actual_name:
        return False, False, [f"tool_mismatch: expected={expected_name} actual={actual_name}"]
    if actual_args != expected_args:
        return True, False, [
            "args_mismatch: "
            f"expected={json.dumps(expected_args, ensure_ascii=False, sort_keys=True)} "
            f"actual={json.dumps(actual_args, ensure_ascii=False, sort_keys=True)}"
        ]
    return True, True, []


def run_9b_tool(client: OpenAI, file: str, case: dict) -> ToolResult:
    text = case.get("text", "")
    expected_tool = case.get("expected_tool", "")
    expected_args = case.get("expected_args", {})
    history = case.get("history", [])
    try:
        tools = EVAL_TOOL_SCHEMAS
        system_prompt = (
            "你是一个机器人语音助手，控制一台物理机器人。\n"
            "用户用中文下达指令，请根据指令从工具列表中选最匹配的工具调用。\n"
            "- 移动/转向/停止：选 move_robot\n"
            "- 跳舞/表演：选 dance\n"
            "- 巡逻/巡检：选 start_patrol\n"
            "- 充电/回桩：选 return_to_charge\n"
            "- 视频电话：选 call_video\n"
            "- 唱歌：选 sing\n"
            "- 放音乐：选 play_music\n"
            "- 检测手势/宠物/环境：选对应 detect_* 或 understand_environment\n"
            "- 建图：选 create_map\n"
            "- 取消当前任务：选 cancel_task\n"
            "- 提醒/待办：选 create_reminder 或 list_reminders\n"
            "- 联网搜索：选 websearch.search\n"
            "- 时间：选 utils_remote.current_time\n"
            "闲聊/能力问句/故事：不调用任何工具，直接自然回答。"
        )
        # 构造 messages, 支持多轮
        messages = [{"role": "system", "content": system_prompt}]
        for h in history:
            messages.append(h)
        messages.append({"role": "user", "content": text})
        started = time.monotonic()
        resp = client.chat.completions.create(
            model=GEN_MODEL,
            messages=messages,
            tools=tools,
            max_tokens=200,
            temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        elapsed = (time.monotonic() - started) * 1000
        message = resp.choices[0].message
        actual_text = (message.content or "").strip()
        actual_tool = ""
        actual_args = {}
        tool_calls = getattr(message, "tool_calls", None) or []
        if tool_calls:
            tc = message.tool_calls[0]
            actual_tool = tc.function.name
            try:
                actual_args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else tc.function.arguments
            except (json.JSONDecodeError, TypeError):
                actual_args = {}
        tool_ok, args_ok, validation_errors = _validate_tool_prediction(
            expected_tool=expected_tool,
            expected_args=expected_args,
            actual_tool=actual_tool,
            actual_args=actual_args,
            has_tool_calls=bool(tool_calls),
        )
        if actual_text.startswith("{"):
            validation_errors.append("tool_protocol_leakage_in_text")
        ok = tool_ok and args_ok and not validation_errors
        candidate_tools = [item["function"]["name"] for item in tools]

        return ToolResult(
            file=file,
            text=text,
            expected_tool=expected_tool,
            expected_args=expected_args,
            actual_tool=_canonical_tool_name(actual_tool) if actual_tool else "NONE",
            actual_args=actual_args,
            actual_text=actual_text,
            elapsed_ms=elapsed,
            ok=ok,
            case_id=case.get("id", ""),
            tool_ok=tool_ok,
            args_ok=args_ok,
            validation_errors=validation_errors,
            candidate_tools=candidate_tools,
            scenario=case.get("scenario", ""),
        )
    except Exception as exc:
        return ToolResult(
            file=file,
            text=text,
            expected_tool=expected_tool,
            expected_args=expected_args,
            actual_tool="ERROR",
            actual_args={},
            actual_text="",
            elapsed_ms=0,
            ok=False,
            case_id=case.get("id", ""),
            validation_errors=[f"request_error: {type(exc).__name__}"],
            candidate_tools=[item["function"]["name"] for item in EVAL_TOOL_SCHEMAS],
            scenario=case.get("scenario", ""),
            error=f"{type(exc).__name__}: {exc}",
        )


def summarize_4b_results(results: list[RouterResult]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    by_category: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    for r in results:
        cat = r.expected
        by_category[cat]["total"] += 1
        if r.ok:
            by_category[cat]["passed"] += 1
        else:
            by_category[cat]["failed"] += 1
    failures = [
        {
            "case": {
                "id": r.case_id,
                "text": r.text,
                "expected": r.expected,
                "file": r.file,
                "scenario": r.scenario,
            },
            "actual": r.actual,
            "raw": r.raw,
            "elapsed_ms": r.elapsed_ms,
            "error": r.error,
        }
        for r in results if not r.ok
    ]
    return {
        "name": "4b_router",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": ROUTER_MODEL,
        "total": total,
        "passed": passed,
        "failed": failed,
        "accuracy": passed / total if total > 0 else 0,
        "by_category": dict(by_category),
        "failures": failures,
        "results": [asdict(r) for r in results],  # 保留全部结果, 用于延迟分析
    }


def summarize_9b_results(results: list[ToolResult]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    by_category: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    for r in results:
        cat = r.expected_tool
        by_category[cat]["total"] += 1
        if r.ok:
            by_category[cat]["passed"] += 1
        else:
            by_category[cat]["failed"] += 1
    failures = [
        {
            "case": {
                "id": r.case_id,
                "text": r.text,
                "expected_tool": r.expected_tool,
                "file": r.file,
                "scenario": r.scenario,
            },
            "actual_tool": r.actual_tool,
            "actual_text": r.actual_text,
            "actual_args": r.actual_args,
            "elapsed_ms": r.elapsed_ms,
            "error": r.error,
        }
        for r in results if not r.ok
    ]
    return {
        "name": "9b_tool",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": GEN_MODEL,
        "total": total,
        "passed": passed,
        "failed": failed,
        "accuracy": passed / total if total > 0 else 0,
        "by_category": dict(by_category),
        "failures": failures,
        "results": [asdict(r) for r in results],
    }
