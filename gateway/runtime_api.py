"""Runtime API payload helpers for the Python voice gateway."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


SENSITIVE_NOTE_REDACTION = "[redacted sensitive note]"
_SENSITIVE_NOTE_KEY_PATTERN = re.compile(
    r"\b(robot[_-]?secret|secret|token|password|passwd|api[_-]?key|authorization)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_BARE_SECRET_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{24,}$")


def redact_runtime_robot_note(note: Any) -> str | None:
    """Redact secret-looking runtime robot notes before returning admin payloads."""
    if note is None:
        return None
    text = str(note)
    if not text:
        return text

    if _SENSITIVE_NOTE_KEY_PATTERN.search(text):
        return SENSITIVE_NOTE_REDACTION

    for token in re.split(r"[\s,;]+", text):
        candidate = token.strip("\"'()[]{}<>，。；：、")
        if not _BARE_SECRET_TOKEN_PATTERN.fullmatch(candidate):
            continue
        has_letter = any(ch.isalpha() for ch in candidate)
        has_digit = any(ch.isdigit() for ch in candidate)
        if has_letter and has_digit:
            return SENSITIVE_NOTE_REDACTION

    return text


def sanitize_runtime_robot(robot: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(robot)
    original_note = item.get("notes")
    redacted_note = redact_runtime_robot_note(original_note)
    item["notes"] = redacted_note
    item["notes_redacted"] = redacted_note == SENSITIVE_NOTE_REDACTION and bool(original_note)
    return item


def sanitize_runtime_robots(robots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [sanitize_runtime_robot(robot) for robot in robots]


def build_stats_payload(active_connections: int, session_stats: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "active_connections": active_connections,
        **dict(session_stats),
    }


def build_runtime_robots_payload(
    *,
    active_connections: int,
    session_stats: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]],
    robots: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    rtc: Mapping[str, Any],
    upstreams: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "success": True,
        "stats": build_stats_payload(active_connections, session_stats),
        "sessions": [dict(session) for session in sessions],
        "robots": sanitize_runtime_robots(robots),
        "settings": dict(settings),
        "rtc": dict(rtc),
        "upstreams": [dict(upstream) for upstream in upstreams],
    }


def build_runtime_session_payload(session: Mapping[str, Any] | None) -> dict[str, Any]:
    if not session:
        return {"success": False, "session": None, "message": "会话不存在或已清理"}
    return {"success": True, "session": dict(session)}
