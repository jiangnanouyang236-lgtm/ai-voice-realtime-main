#!/usr/bin/env python3
"""Collect Gateway runtime trace metrics into a baseline JSON report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


NUMERIC_METRICS = (
    "queue_size",
    "audio_chunks",
    "audio_bytes",
    "audio_duration_ms",
    "asr_queue_wait_ms",
    "asr_inference_ms",
    "llm_first_token_ms",
    "llm_first_text_ms",
    "llm_total_ms",
    "llm_service_total_ms",
    "llm_request_text_chars",
    "llm_messages_count",
    "llm_tools_count",
    "llm_mcp_servers_count",
    "llm_mcp_prepare_ms",
    "llm_mcp_connect_ms",
    "llm_mcp_tool_build_ms",
    "llm_router_ms",
    "llm_first_round_tools_count",
    "llm_tool_call_count",
    "llm_tool_total_ms",
    "llm_tool_max_ms",
    "llm_response_chars",
    "tts_connect_ms",
    "tts_first_commit_ms",
    "tts_first_audio_ms",
    "tts_internal_first_pcm_ms",
    "tts_first_text_to_first_pcm_ms",
    "tts_gateway_after_server_pcm_ms",
    "tts_request_to_grpc_yield_ms",
    "ws_send_ms",
    "ws_slow_send_ms",
    "ws_send_timeout_ms",
    "ws_slow_send_strikes",
    "client_playback_chunks",
    "client_playback_samples",
    "client_playback_underruns",
    "client_playback_zero_fill_samples",
    "client_playback_max_buffered_samples",
    "client_first_audio_to_playback_start_ms",
    "client_playback_start_to_complete_ms",
)

BOOLEAN_METRICS = (
    "cancelled",
    "timeout",
    "ws_backpressure",
    "client_playback_completed",
    "client_playback_interrupted",
    "llm_mcp_enabled",
    "llm_router_classifier_used",
)


def fetch_json(url: str, *, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Gateway response is not a JSON object")
    return payload


def traces_url(base_url: str, *, limit: int, offset: int) -> str:
    query = urlencode({"limit": limit, "offset": offset})
    return f"{base_url.rstrip('/')}/internal/runtime/traces?{query}"


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 1)


def summarize_numeric(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "avg": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "avg": round(sum(values) / len(values), 1),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": round(max(values), 1),
    }


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize_rounds(items: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    metric_values: dict[str, list[float]] = {metric: [] for metric in NUMERIC_METRICS}
    boolean_counts: dict[str, int] = {metric: 0 for metric in BOOLEAN_METRICS}

    for item in items:
        status = item.get("status") or "<unknown>"
        status_counts[str(status)] = status_counts.get(str(status), 0) + 1

        metrics = item.get("metrics") or {}
        if not isinstance(metrics, dict):
            continue
        for metric in NUMERIC_METRICS:
            number = _as_number(metrics.get(metric))
            if number is not None:
                metric_values[metric].append(number)
        for metric in BOOLEAN_METRICS:
            if bool(metrics.get(metric)):
                boolean_counts[metric] += 1

    return {
        "round_count": len(items),
        "status_counts": status_counts,
        "numeric_metrics": {
            metric: summarize_numeric(values)
            for metric, values in metric_values.items()
            if values
        },
        "boolean_metric_true_counts": {
            metric: count for metric, count in boolean_counts.items() if count
        },
    }


def compact_round(item: dict[str, Any]) -> dict[str, Any]:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    return {
        "trace_id": item.get("trace_id"),
        "session_id": item.get("session_id"),
        "round_seq": item.get("round_seq"),
        "robot_id": item.get("robot_id"),
        "bot_id": item.get("bot_id"),
        "bot_name": item.get("bot_name"),
        "status": item.get("status"),
        "last_stage": item.get("last_stage"),
        "duration_ms": item.get("duration_ms"),
        "metrics": metrics,
    }


def build_report(
    payload: dict[str, Any],
    *,
    base_url: str,
    label: str,
    require_client_playback_report: bool,
) -> dict[str, Any]:
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise ValueError("Gateway trace payload items is not a list")

    compact_items = [compact_round(item) for item in items if isinstance(item, dict)]
    if require_client_playback_report:
        compact_items = [
            item
            for item in compact_items
            if item["metrics"].get("client_playback_completed")
            or item["metrics"].get("client_playback_interrupted")
        ]

    return {
        "schema_version": "gateway-trace-baseline/v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "label": label,
        "base_url": base_url.rstrip("/"),
        "source_stats": payload.get("stats"),
        "source_pagination": payload.get("pagination"),
        "filters": {
            "require_client_playback_report": require_client_playback_report,
        },
        "summary": summarize_rounds(compact_items),
        "rounds": compact_items,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect Gateway /internal/runtime/traces into a baseline JSON report.",
    )
    parser.add_argument("--base-url", required=True, help="Gateway base URL, e.g. http://127.0.0.1:9860")
    parser.add_argument("--label", required=True, help="Environment label for this baseline run")
    parser.add_argument("--limit", type=int, default=100, help="Trace page size, 1-500")
    parser.add_argument("--offset", type=int, default=0, help="Trace page offset")
    parser.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--require-client-playback-report",
        action="store_true",
        help="Only include rounds with client playback completion/interruption reports",
    )
    parser.add_argument("--out", help="Write report JSON to this path instead of stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    limit = max(1, min(int(args.limit), 500))
    offset = max(0, int(args.offset))
    payload = fetch_json(
        traces_url(args.base_url, limit=limit, offset=offset),
        timeout=max(0.1, float(args.timeout)),
    )
    report = build_report(
        payload,
        base_url=args.base_url,
        label=args.label,
        require_client_playback_report=bool(args.require_client_playback_report),
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{encoded}\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
