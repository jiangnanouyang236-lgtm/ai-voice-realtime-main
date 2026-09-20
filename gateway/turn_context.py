"""Helpers for per-turn trace/playback identifiers."""

from __future__ import annotations

from typing import Any

from gateway.playback_report import request_playback_id, request_round_id, request_trace_id


def build_turn_ids(data: dict[str, Any], session_id: str, round_seq: int) -> dict[str, str]:
    round_id = request_round_id(data, session_id, round_seq)
    trace_id = request_trace_id(data, round_id)
    playback_id = request_playback_id(data, round_id)
    return {
        "trace_id": trace_id,
        "round_id": round_id,
        "playback_id": playback_id,
    }


def build_turn_trace(
    data: dict[str, Any],
    trace_context: dict[str, Any] | None = None,
    *,
    trace_id: str | None,
    round_id: str | None,
    playback_id: str | None,
    round_seq: int | None,
) -> dict[str, Any]:
    resolved_round_id = round_id or trace_id
    resolved_playback_id = playback_id
    if resolved_playback_id is None and resolved_round_id:
        resolved_playback_id = request_playback_id(data, resolved_round_id)

    return {
        "trace_id": trace_id,
        "round_seq": round_seq,
        "round_id": resolved_round_id,
        "playback_id": resolved_playback_id,
        **(trace_context or {}),
    }
