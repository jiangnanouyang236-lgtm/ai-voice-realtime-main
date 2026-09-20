from __future__ import annotations

import json
import re
from typing import Any

from singing.library import SONG_ID_RE, VOICE_ID_RE


PLAYBACK_MARKER_PREFIX = "[SINGING_PLAYBACK:"
PLAYBACK_MARKER_RE = re.compile(
    r"\[SINGING_PLAYBACK:([a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+)\]"
)


def playback_marker(voice_id: str, song_id: str) -> str:
    if not VOICE_ID_RE.fullmatch(voice_id or ""):
        raise ValueError("唱歌音色 id 非法")
    if not SONG_ID_RE.fullmatch(song_id or ""):
        raise ValueError("歌曲 id 非法")
    return f"{PLAYBACK_MARKER_PREFIX}{voice_id}:{song_id}]"


def parse_playback_tool_result(text: str) -> dict[str, Any] | None:
    value = (text or "").strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or parsed.get("kind") != "singing_playback":
        return None
    song_id = str(parsed.get("song_id") or "")
    voice_id = str(parsed.get("voice_id") or "")
    if not SONG_ID_RE.fullmatch(song_id) or not VOICE_ID_RE.fullmatch(voice_id):
        return None
    if parsed.get("asset_id") != f"{voice_id}:{song_id}":
        return None
    return parsed


def playback_control_from_tool_result(text: str) -> str | None:
    parsed = parse_playback_tool_result(text)
    return playback_marker(parsed["voice_id"], parsed["song_id"]) if parsed else None
