"""Helpers for client playback report IDs and numeric metrics."""

from __future__ import annotations

from typing import Any


def round_seq_from_trace_id(session_id: str, trace_id: str | None) -> int | None:
    if not trace_id or not trace_id.startswith(f"{session_id}:"):
        return None
    try:
        return int(trace_id.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        return None


def non_empty_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def request_round_id(data: dict[str, Any], session_id: str, round_seq: int) -> str:
    round_id = non_empty_str(data.get("round_id"))
    if round_id:
        return round_id
    return f"{session_id}:{round_seq}"


def request_trace_id(data: dict[str, Any], round_id: str) -> str:
    return non_empty_str(data.get("trace_id")) or round_id


def request_playback_id(data: dict[str, Any], round_id: str) -> str:
    return non_empty_str(data.get("playback_id")) or f"{round_id}:playback"


def build_playback_cancel_payload(
    cancelled_round: dict[str, str] | None,
    *,
    reason: str,
) -> dict[str, str] | None:
    if not cancelled_round or not cancelled_round.get("round_id"):
        return None
    return {
        "round_id": cancelled_round["round_id"],
        "playback_id": cancelled_round.get("playback_id") or "",
        "reason": reason,
    }


def int_from_client_report(data: dict[str, Any], key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def float_from_client_report(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def build_client_playback_summary(
    data: dict[str, Any],
    *,
    interrupted: bool,
) -> dict[str, Any] | None:
    round_id = data.get("round_id")
    if not isinstance(round_id, str) or not round_id:
        return None

    trace_id = request_trace_id(data, round_id)
    playback_id = data.get("playback_id")
    if not isinstance(playback_id, str):
        playback_id = ""
    reason = data.get("reason")
    if not isinstance(reason, str):
        reason = None

    summary = {
        "trace_id": trace_id,
        "round_id": round_id,
        "playback_id": playback_id,
        "client_playback_completed": not interrupted,
        "client_playback_interrupted": interrupted,
        "client_playback_chunks": int_from_client_report(data, "pushed_chunks"),
        "client_playback_samples": int_from_client_report(data, "pushed_samples"),
        "client_playback_underruns": int_from_client_report(data, "underrun_callbacks"),
        "client_playback_zero_fill_samples": int_from_client_report(
            data,
            "zero_filled_samples",
        ),
        "client_playback_max_buffered_samples": int_from_client_report(
            data,
            "max_buffered_samples",
        ),
    }
    first_audio_to_playback_ms = float_from_client_report(
        data,
        "first_audio_to_playback_start_ms",
    )
    playback_start_to_complete_ms = float_from_client_report(
        data,
        "playback_start_to_complete_ms",
    )
    if first_audio_to_playback_ms is not None:
        summary["client_first_audio_to_playback_start_ms"] = first_audio_to_playback_ms
    if playback_start_to_complete_ms is not None:
        summary["client_playback_start_to_complete_ms"] = playback_start_to_complete_ms
    if reason:
        summary["reason"] = reason
    return summary


def build_client_playback_trace_event(
    session_id: str,
    data: dict[str, Any],
    *,
    interrupted: bool,
    trace_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    summary = build_client_playback_summary(data, interrupted=interrupted)
    if summary is None:
        return None

    trace_context = trace_context or {}
    trace_id = summary["trace_id"]
    round_id = summary["round_id"]
    return {
        "trace_id": trace_id,
        "round_seq": (
            round_seq_from_trace_id(session_id, round_id)
            or round_seq_from_trace_id(session_id, trace_id)
        ),
        "robot_id": trace_context.get("robot_id"),
        "bot_id": trace_context.get("bot_id"),
        "bot_name": trace_context.get("bot_name"),
        "stage": "client_playback_interrupted" if interrupted else "client_playback_completed",
        "summary": summary,
    }
