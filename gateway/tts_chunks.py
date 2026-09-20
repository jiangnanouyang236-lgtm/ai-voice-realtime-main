from __future__ import annotations

import time
from typing import Any

from tts import tts_service_pb2


def build_tts_text_chunk(
    text: str,
    *,
    is_final: bool,
    session_id: str,
    trace_id: str | None,
    round_id: str | None,
    playback_id: str | None,
    bot_tts_settings: dict[str, Any] | None = None,
    include_config: bool = False,
    gateway_send_epoch_ms: float | None = None,
) -> tts_service_pb2.TextChunk:
    kwargs: dict[str, Any] = {
        "text": text,
        "is_final": is_final,
        "session_id": session_id,
        "trace_id": trace_id or "",
        "round_id": round_id or "",
        "playback_id": playback_id or "",
        "gateway_send_epoch_ms": (
            gateway_send_epoch_ms
            if gateway_send_epoch_ms is not None
            else time.time() * 1000.0
        ),
    }
    if include_config and bot_tts_settings:
        kwargs["config"] = tts_service_pb2.TTSConfig(
            tts_profile_id=str(bot_tts_settings.get("tts_profile_id") or ""),
        )
    return tts_service_pb2.TextChunk(**kwargs)
