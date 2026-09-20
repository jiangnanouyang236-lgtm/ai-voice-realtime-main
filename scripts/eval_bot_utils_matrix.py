#!/usr/bin/env python3
"""Validate Utils routing and final-answer factual fidelity for every enabled Bot.

The LLM service must already be running from the checkout under test. This script
is read-only: it loads Runtime Snapshot and invokes the local LLM gRPC endpoint.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sys
import time
from typing import Any

import grpc

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from llm import llm_service_pb2, llm_service_pb2_grpc
from mcp_servers import utils_api
from server_config.repository import ConfigRepository


def expected_time_facts() -> dict[str, Any]:
    now = datetime.now(timezone(timedelta(hours=8)))
    holiday = utils_api.is_holiday_today()
    return {
        "date_padded": now.strftime("%Y年%m月%d日"),
        "date_unpadded": f"{now.year}年{now.month}月{now.day}日",
        "weekday": utils_api.WEEKDAY_CN[now.weekday()],
        "holiday": holiday,
    }


def validate_time_fidelity(response: str, facts: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not any(
        date_text in response
        for date_text in (facts["date_padded"], facts["date_unpadded"])
    ):
        errors.append("missing_or_wrong_date")
    if facts["weekday"] not in response:
        errors.append("missing_or_wrong_weekday")
    if any(marker in response for marker in ('{"tool_name"', '{"name"', "工具调用中")):
        errors.append("tool_protocol_leakage")

    holiday = str(facts["holiday"])
    if holiday.startswith("是"):
        expected_label = holiday.split("：", 1)[-1]
        if expected_label not in response and "法定节假日" not in response:
            errors.append("missing_or_wrong_holiday")
        if "不是法定节假日" in response or "并非法定节假日" in response:
            errors.append("contradictory_holiday")
    elif holiday.startswith("否"):
        if "工作日" not in response:
            errors.append("missing_or_wrong_workday")
        if "是法定节假日" in response:
            errors.append("contradictory_workday")
    return errors


def run_matrix(port: int, timeout: float) -> dict[str, Any]:
    snapshot = ConfigRepository(config.CONFIG_DATABASE_URL).load_runtime_snapshot()
    stub = llm_service_pb2_grpc.LLMServiceStub(
        grpc.insecure_channel(f"127.0.0.1:{port}")
    )
    results: list[dict[str, Any]] = []
    for index, (bot_id, bot) in enumerate(sorted(snapshot.bots.items())):
        if not bot.enabled:
            continue
        started = time.monotonic()
        chunks: list[str] = []
        metrics: dict[str, Any] = {}
        error = ""
        facts = expected_time_facts()
        trace_id = f"bot-utils-matrix-{index}-{bot_id.replace(' ', '_')}"
        try:
            request = llm_service_pb2.ChatRequest(
                text="请通过时间工具告诉我现在的完整日期、时间和星期。",
                session_id=f"bot-utils-matrix-{index}",
                bot_id=bot_id,
                trace_id=trace_id,
            )
            for response in stub.StreamChat(request, timeout=timeout):
                if response.text:
                    chunks.append(response.text)
                if response.metrics_json:
                    metrics = json.loads(response.metrics_json)
        except grpc.RpcError as exc:
            error = f"{exc.code().name}: {exc.details()}"

        response_text = "".join(chunks)
        has_utils = "utils_remote" in bot.mcp_servers
        tool_calls = int(metrics.get("llm_tool_call_count", 0))
        selected_tool = str(metrics.get("llm_selected_tool_name", ""))
        fidelity_errors = (
            validate_time_fidelity(response_text, facts) if has_utils and not error else []
        )
        grounding_errors: list[str] = []
        if has_utils:
            route_ok = (
                tool_calls == 1
                and selected_tool == "utils_remote__get_now_context"
                and metrics.get("llm_router_kind") == "tool"
            )
        else:
            route_ok = tool_calls == 0 and "utils_remote" not in selected_tool
            if "没有可用的时间查询能力" not in response_text:
                grounding_errors.append("ungrounded_realtime_answer_without_utils")
        behavior_ok = route_ok and not fidelity_errors and not grounding_errors and not error
        results.append(
            {
                "bot_id": bot_id,
                "trace_id": trace_id,
                "is_default": bot.is_default,
                "mcp_servers": list(bot.mcp_servers),
                "has_utils": has_utils,
                "route_ok": route_ok,
                "fidelity_ok": not fidelity_errors if has_utils else None,
                "fidelity_errors": fidelity_errors,
                "grounding_errors": grounding_errors,
                "behavior_ok": behavior_ok,
                "error": error,
                "response": response_text,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "metrics": {
                    "router_kind": metrics.get("llm_router_kind"),
                    "router_source": metrics.get("llm_router_source"),
                    "selected_tool": selected_tool,
                    "tool_call_count": tool_calls,
                    "tools_count": metrics.get("llm_tools_count"),
                    "stream_mode": metrics.get("llm_stream_mode"),
                },
            }
        )

    configured_utils = [item for item in results if item["has_utils"]]
    route_kinds = {item["metrics"]["router_kind"] for item in configured_utils}
    report = {
        "schema_version": "bot-utils-route-matrix/v2",
        "config_version": snapshot.config_version,
        "runtime_source": snapshot.source,
        "total": len(results),
        "passed": sum(1 for item in results if item["behavior_ok"]),
        "utils_bot_total": len(configured_utils),
        "utils_fidelity_passed": sum(
            1 for item in configured_utils if item["fidelity_ok"]
        ),
        "shared_route_policy": len(route_kinds) <= 1 and route_kinds == {"tool"},
        "results": results,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="全部启用 Bot 的 Utils 严格矩阵")
    parser.add_argument("--port", type=int, default=50053)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    report = run_matrix(args.port, args.timeout)
    output = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(output)
    if args.output:
        output_path = Path(args.output)
        resolved = output_path.resolve() if output_path.is_absolute() else (ROOT / output_path).resolve()
        if not any(
            resolved.is_relative_to((ROOT / directory).resolve())
            for directory in ("reports", "tmp")
        ):
            print("ERROR: output 必须位于 reports/ 或 tmp/")
            return 2
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(output + "\n", encoding="utf-8")
    return 0 if report["passed"] == report["total"] and report["shared_route_policy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
