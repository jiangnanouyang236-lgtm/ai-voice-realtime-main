"""
In-memory Gateway trace recorder.

This recorder is intentionally best-effort and non-persistent. The voice path
must never wait on storage or network I/O just to keep observability data.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import threading
import time
from typing import Any


_METRIC_KEYS = {
    "queue_size",
    "audio_chunks",
    "audio_bytes",
    "audio_duration_ms",
    "tts_empty_audio_chunks",
    "asr_queue_wait_ms",
    "asr_inference_ms",
    "llm_first_token_ms",
    "llm_first_text_ms",
    "llm_total_ms",
    "llm_service_total_ms",
    "llm_request_text_chars",
    "llm_messages_count",
    "llm_tools_count",
    "llm_mcp_enabled",
    "llm_mcp_servers_count",
    "llm_mcp_prepare_ms",
    "llm_mcp_connect_ms",
    "llm_mcp_tool_build_ms",
    "llm_router_ms",
    "llm_router_kind",
    "llm_router_source",
    "llm_router_category",
    "llm_router_classifier_used",
    "llm_stream_mode",
    "llm_first_round_tools_count",
    "llm_selected_tool_name",
    "llm_tool_choice_mode",
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
    "ws_backpressure",
    "ws_slow_send_ms",
    "ws_send_timeout_ms",
    "ws_slow_send_strikes",
    "cancelled",
    "timeout",
    "client_playback_completed",
    "client_playback_interrupted",
    "client_playback_chunks",
    "client_playback_samples",
    "client_playback_underruns",
    "client_playback_zero_fill_samples",
    "client_playback_max_buffered_samples",
    "client_first_audio_to_playback_start_ms",
    "client_playback_start_to_complete_ms",
}

_DURATION_METRIC_BY_STAGE = {
    "llm_first_token": "llm_first_token_ms",
    "llm_done": "llm_total_ms",
    "tts_connected": "tts_connect_ms",
    "tts_first_commit": "tts_first_commit_ms",
    "tts_first_audio": "tts_first_audio_ms",
}

_DIAGNOSIS_METRICS = (
    ("asr_queue_wait_ms", "ASR queue wait", 500.0),
    ("asr_inference_ms", "ASR inference", 1500.0),
    ("llm_router_ms", "LLM router", 800.0),
    ("llm_mcp_prepare_ms", "MCP prepare", 1000.0),
    ("llm_mcp_connect_ms", "MCP connect", 1500.0),
    ("llm_mcp_tool_build_ms", "MCP tool build", 1000.0),
    ("llm_first_token_ms", "LLM first token", 3000.0),
    ("llm_tool_total_ms", "LLM tool calls", 5000.0),
    ("llm_total_ms", "LLM total", 10000.0),
    ("tts_connect_ms", "TTS connect", 1000.0),
    ("tts_first_commit_ms", "TTS first commit", 1000.0),
    ("tts_first_audio_ms", "TTS first audio", 3000.0),
    ("ws_slow_send_ms", "Gateway WS send", 150.0),
    ("client_first_audio_to_playback_start_ms", "Client first audio to playback", 500.0),
    ("client_playback_start_to_complete_ms", "Client playback", 15000.0),
)


class TraceRecorder:
    def __init__(
        self,
        *,
        enabled: bool = True,
        max_events: int = 10_000,
        max_rounds: int = 1_000,
        text_max_chars: int = 300,
        error_max_chars: int = 1_000,
    ) -> None:
        self.enabled = enabled
        self.max_events = max(1, int(max_events))
        self.max_rounds = max(1, int(max_rounds))
        self.text_max_chars = max(20, int(text_max_chars))
        self.error_max_chars = max(50, int(error_max_chars))
        self._lock = threading.Lock()
        self._rounds: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._event_count = 0

    def emit(
        self,
        trace_id: str | None,
        *,
        session_id: str,
        round_seq: int | None = None,
        stage: str,
        status: str = "ok",
        robot_id: str | None = None,
        bot_id: str | None = None,
        bot_name: str | None = None,
        duration_ms: float | None = None,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if not self.enabled or not trace_id:
            return

        raw_summary = summary or {}
        now = time.time()
        event = {
            "trace_id": trace_id,
            "session_id": session_id,
            "round_seq": round_seq,
            "robot_id": robot_id,
            "bot_id": bot_id,
            "bot_name": bot_name,
            "stage": stage,
            "status": status,
            "ts": _iso_now(),
            "ts_epoch": now,
            "duration_ms": _round_float(duration_ms),
            "summary": self._sanitize_summary(raw_summary),
            "error": self._truncate(error, self.error_max_chars) if error else None,
        }

        try:
            with self._lock:
                item = self._rounds.get(trace_id)
                if item is None:
                    item = {
                        "trace_id": trace_id,
                        "session_id": session_id,
                        "round_seq": round_seq,
                        "robot_id": robot_id,
                        "bot_id": bot_id,
                        "bot_name": bot_name,
                        "started_at": event["ts"],
                        "started_epoch": now,
                        "last_event_at": event["ts"],
                        "last_event_epoch": now,
                        "last_stage": stage,
                        "status": status,
                        "duration_ms": 0.0,
                        "event_count": 0,
                        "metrics": {},
                        "events": [],
                    }
                    self._rounds[trace_id] = item
                    previous_event_epoch = None
                else:
                    self._rounds.move_to_end(trace_id)
                    previous_event_epoch = item.get("last_event_epoch")

                started_epoch = float(item.get("started_epoch") or now)
                event["since_round_start_ms"] = _round_float((now - started_epoch) * 1000)
                event["since_previous_event_ms"] = (
                    _round_float((now - float(previous_event_epoch)) * 1000)
                    if previous_event_epoch is not None
                    else None
                )

                if robot_id:
                    item["robot_id"] = robot_id
                if bot_id:
                    item["bot_id"] = bot_id
                if bot_name:
                    item["bot_name"] = bot_name

                item["last_event_at"] = event["ts"]
                item["last_event_epoch"] = now
                item["last_stage"] = stage
                item["event_count"] = int(item.get("event_count") or 0) + 1
                item["duration_ms"] = _round_float(
                    (now - float(item.get("started_epoch") or now)) * 1000
                )
                if status in {"error", "timeout"} or item.get("status") not in {"error", "timeout"}:
                    item["status"] = status

                self._update_metrics(
                    item,
                    stage=stage,
                    status=status,
                    duration_ms=duration_ms,
                    summary=raw_summary,
                )
                item["events"].append(event)
                self._event_count += 1
                self._trim_locked()
        except Exception:
            # Tracing must be invisible to the voice path.
            return

    def list_rounds(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        page_limit = max(1, min(int(limit or 100), self.max_rounds))
        page_offset = max(0, int(offset or 0))
        if not self.enabled:
            return {
                "success": True,
                "enabled": False,
                "items": [],
                "stats": self.stats(),
                "pagination": {
                    "limit": page_limit,
                    "offset": page_offset,
                    "total": 0,
                    "has_more": False,
                },
            }
        with self._lock:
            ordered_rounds = list(reversed(list(self._rounds.values())))
            total = len(ordered_rounds)
            items = [
                self._round_summary(item)
                for item in ordered_rounds[page_offset : page_offset + page_limit]
            ]
            stats = self._stats_locked()
        return {
            "success": True,
            "enabled": True,
            "items": items,
            "stats": stats,
            "pagination": {
                "limit": page_limit,
                "offset": page_offset,
                "total": total,
                "has_more": page_offset + page_limit < total,
            },
        }

    def get_round(self, trace_id: str) -> dict[str, Any]:
        with self._lock:
            item = self._rounds.get(trace_id)
            if item is None:
                return {
                    "success": False,
                    "enabled": self.enabled,
                    "trace": None,
                    "message": "trace 不存在或已被内存淘汰",
                    "stats": self._stats_locked(),
                }
            return {
                "success": True,
                "enabled": self.enabled,
                "trace": {
                    **self._round_summary(item),
                    "events": [dict(event) for event in item.get("events", [])],
                },
                "stats": self._stats_locked(),
            }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return self._stats_locked()

    def _sanitize_summary(self, value: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str):
                sanitized[key] = self._truncate(item, self.text_max_chars)
            elif isinstance(item, (int, float, bool)) or item is None:
                sanitized[key] = item
            else:
                sanitized[key] = self._truncate(str(item), self.text_max_chars)
        return sanitized

    def _truncate(self, value: str, max_chars: int) -> str:
        text = str(value)
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    def _trim_locked(self) -> None:
        while self._event_count > self.max_events or len(self._rounds) > self.max_rounds:
            _, item = self._rounds.popitem(last=False)
            self._event_count -= len(item.get("events", []))
        self._event_count = max(0, self._event_count)

    def _round_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "trace_id": item.get("trace_id"),
            "session_id": item.get("session_id"),
            "round_seq": item.get("round_seq"),
            "robot_id": item.get("robot_id"),
            "bot_id": item.get("bot_id"),
            "bot_name": item.get("bot_name"),
            "started_at": item.get("started_at"),
            "last_event_at": item.get("last_event_at"),
            "last_stage": item.get("last_stage"),
            "status": item.get("status"),
            "duration_ms": item.get("duration_ms"),
            "event_count": item.get("event_count", 0),
            "metrics": dict(item.get("metrics") or {}),
            "diagnosis": _diagnose_round(item),
        }

    def _stats_locked(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "round_count": len(self._rounds),
            "event_count": self._event_count,
            "max_rounds": self.max_rounds,
            "max_events": self.max_events,
            "text_max_chars": self.text_max_chars,
            "error_max_chars": self.error_max_chars,
        }

    def _update_metrics(
        self,
        item: dict[str, Any],
        *,
        stage: str,
        status: str,
        duration_ms: float | None,
        summary: dict[str, Any],
    ) -> None:
        metrics = item.setdefault("metrics", {})

        duration_metric = _DURATION_METRIC_BY_STAGE.get(stage)
        if duration_metric and duration_ms is not None:
            _set_metric(metrics, duration_metric, duration_ms)

        if status == "timeout" or stage.endswith("_timeout"):
            _set_metric(metrics, "timeout", True)
        if stage.endswith("_interrupted"):
            _set_metric(metrics, "cancelled", True)

        for key, value in _iter_metric_values(summary):
            _set_metric(metrics, key, value)


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _round_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


def _iter_metric_values(value: Any):
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in _METRIC_KEYS and item is not None:
            yield key, item
        if isinstance(item, dict):
            yield from _iter_metric_values(item)


def _normalize_metric_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _round_float(value)
    return value


def _set_metric(metrics: dict[str, Any], key: str, value: Any) -> None:
    normalized = _normalize_metric_value(value)
    if key in {"cancelled", "timeout"}:
        metrics[key] = bool(metrics.get(key)) or bool(normalized)
        return
    if key in {"audio_bytes", "audio_duration_ms"} and key in metrics:
        return
    metrics[key] = normalized


def _diagnose_round(item: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(item.get("metrics") or {})
    breakdown: list[dict[str, Any]] = []
    for metric, label, slow_threshold_ms in _DIAGNOSIS_METRICS:
        duration_ms = _number_metric(metrics.get(metric))
        if duration_ms is None:
            continue
        breakdown.append(
            {
                "metric": metric,
                "label": label,
                "duration_ms": _round_float(duration_ms),
                "slow_threshold_ms": slow_threshold_ms,
                "slow": duration_ms >= slow_threshold_ms,
            }
        )

    breakdown.sort(
        key=lambda item: float(item.get("duration_ms") or 0),
        reverse=True,
    )
    bottleneck = breakdown[0] if breakdown else None
    signals = _diagnosis_signals(metrics)
    status = str(item.get("status") or "unknown")

    if bool(metrics.get("timeout")) or status == "timeout":
        severity = "timeout"
    elif bool(metrics.get("cancelled")):
        severity = "cancelled"
    elif bottleneck and bool(bottleneck.get("slow")):
        severity = "slow"
    elif status == "error":
        severity = "error"
    else:
        severity = "ok" if breakdown else "unknown"

    return {
        "severity": severity,
        "bottleneck_metric": bottleneck.get("metric") if bottleneck else None,
        "bottleneck_label": bottleneck.get("label") if bottleneck else None,
        "bottleneck_ms": bottleneck.get("duration_ms") if bottleneck else None,
        "signals": signals,
        "breakdown": breakdown[:8],
    }


def _diagnosis_signals(metrics: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    if bool(metrics.get("timeout")):
        signals.append("timeout")
    if bool(metrics.get("cancelled")):
        signals.append("cancelled")
    if bool(metrics.get("ws_backpressure")):
        signals.append("gateway_ws_backpressure")
    if _number_metric(metrics.get("ws_send_timeout_ms")):
        signals.append("gateway_ws_send_timeout")
    underruns = _number_metric(metrics.get("client_playback_underruns"))
    if underruns and underruns > 0:
        signals.append(f"client_playback_underruns={_round_float(underruns)}")
    zero_fill = _number_metric(metrics.get("client_playback_zero_fill_samples"))
    if zero_fill and zero_fill > 0:
        signals.append(f"client_zero_fill_samples={_round_float(zero_fill)}")
    empty_tts = _number_metric(metrics.get("tts_empty_audio_chunks"))
    if empty_tts and empty_tts > 0:
        signals.append(f"tts_empty_audio_chunks={_round_float(empty_tts)}")
    tool_calls = _number_metric(metrics.get("llm_tool_call_count"))
    if tool_calls and tool_calls > 0:
        signals.append(f"tool_calls={_round_float(tool_calls)}")
    return signals


def _number_metric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
