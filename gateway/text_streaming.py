"""Text streaming helpers used before sending LLM text to TTS."""

from __future__ import annotations

import re

from singing.protocol import PLAYBACK_MARKER_PREFIX, PLAYBACK_MARKER_RE


DEFAULT_EXIT_TAG = "[EXIT]"
_STANDALONE_TOOL_TAG_RE = re.compile(
    r"(?m)^[ \t]*\[(?:robot_remote|websearch|utils_remote|robots_task_service)[^\]\r\n]*\][ \t]*(?:\r?\n)?"
)


def split_exit_safe_buffer(
    buffer: str,
    exit_tag: str = DEFAULT_EXIT_TAG,
) -> tuple[str, str, bool]:
    """Return text safe for TTS while preserving only a possible trailing exit-tag prefix."""
    if not buffer:
        return "", "", False

    if exit_tag in buffer:
        return buffer.replace(exit_tag, ""), "", True

    pending_len = 0
    max_prefix_len = min(len(buffer), len(exit_tag) - 1)
    for size in range(max_prefix_len, 0, -1):
        if exit_tag.startswith(buffer[-size:]):
            pending_len = size
            break

    if pending_len:
        return buffer[:-pending_len], buffer[-pending_len:], False
    return buffer, "", False


def split_control_safe_buffer(
    buffer: str,
    exit_tag: str = DEFAULT_EXIT_TAG,
) -> tuple[str, str, bool, str | None]:
    """Filter exit/song controls while retaining a possibly split trailing control."""
    if not buffer:
        return "", "", False, None

    found_exit = exit_tag in buffer
    asset_ids = PLAYBACK_MARKER_RE.findall(buffer)
    if len(set(asset_ids)) > 1:
        raise ValueError("同一文本流不能包含多个歌曲控制标记")
    safe = PLAYBACK_MARKER_RE.sub("", buffer.replace(exit_tag, ""))

    pending_len = 0
    prefixes = (exit_tag, PLAYBACK_MARKER_PREFIX)
    max_prefix_len = min(len(safe), max(len(value) for value in prefixes) - 1)
    for size in range(max_prefix_len, 0, -1):
        if any(value.startswith(safe[-size:]) for value in prefixes):
            pending_len = size
            break

    marker_start = safe.rfind(PLAYBACK_MARKER_PREFIX)
    if marker_start >= 0:
        marker_tail = safe[marker_start:]
        if "]" not in marker_tail and re.fullmatch(
            re.escape(PLAYBACK_MARKER_PREFIX) + r"[a-zA-Z0-9_:-]*",
            marker_tail,
        ):
            pending_len = max(pending_len, len(marker_tail))

    if pending_len:
        return safe[:-pending_len], safe[-pending_len:], found_exit, asset_ids[-1] if asset_ids else None
    return safe, "", found_exit, asset_ids[-1] if asset_ids else None


def strip_standalone_tool_tags(text: str) -> str:
    if not text:
        return ""
    return _STANDALONE_TOOL_TAG_RE.sub("", text)


def direct_text_source(data: dict) -> str:
    return data.get("source") or "client_text"


def build_direct_text_summary(content: str, data: dict) -> dict:
    return {"content": content, "source": direct_text_source(data)}


def build_direct_text_asr_metadata(data: dict) -> dict:
    return {"source": direct_text_source(data)}
