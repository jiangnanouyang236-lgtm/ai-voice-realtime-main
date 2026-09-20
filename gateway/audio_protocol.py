"""Shared WebSocket/VAF1 audio protocol helpers."""

from __future__ import annotations

import json
import os
from typing import Any

from gateway.config import GATEWAY_MAX_AUDIO_RAW_BYTES


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


AUDIO_FRAME_MAGIC = b"VAF1"
AUDIO_FRAME_VERSION = 1
AUDIO_FRAME_HEADER_MAX_BYTES = max(1, _env_int("GATEWAY_AUDIO_FRAME_HEADER_MAX_BYTES", 4096))


class AudioValidationError(ValueError):
    """Raised when client audio violates the current Gateway input contract."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def header_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def encode_audio_frame(payload: bytes, **header: Any) -> bytes:
    """Encode a protocol-v2 binary audio frame."""
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError("audio frame payload must be bytes")
    frame_header = {
        "type": "audio_frame",
        "version": AUDIO_FRAME_VERSION,
        **header,
    }
    header_json = json.dumps(
        frame_header,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(header_json) > AUDIO_FRAME_HEADER_MAX_BYTES:
        raise ValueError(
            f"audio frame header too large: {len(header_json)} > {AUDIO_FRAME_HEADER_MAX_BYTES}"
        )
    return (
        AUDIO_FRAME_MAGIC
        + len(header_json).to_bytes(4, "big")
        + header_json
        + bytes(payload)
    )


def decode_audio_frame(frame: bytes) -> tuple[dict[str, Any], bytes]:
    """Decode and size-check a protocol-v2 binary audio frame."""
    if not isinstance(frame, (bytes, bytearray)):
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧必须是 bytes")

    raw = bytes(frame)
    header_start = len(AUDIO_FRAME_MAGIC) + 4
    if len(raw) < header_start:
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧过短")
    if raw[:len(AUDIO_FRAME_MAGIC)] != AUDIO_FRAME_MAGIC:
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧 magic 不匹配")

    header_len = int.from_bytes(raw[len(AUDIO_FRAME_MAGIC):header_start], "big")
    if header_len <= 0 or header_len > AUDIO_FRAME_HEADER_MAX_BYTES:
        raise AudioValidationError(
            "INVALID_AUDIO_FRAME",
            "二进制音频帧头长度非法",
            details={
                "header_bytes": header_len,
                "max_header_bytes": AUDIO_FRAME_HEADER_MAX_BYTES,
            },
        )
    header_end = header_start + header_len
    if len(raw) < header_end:
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧头不完整")

    try:
        header = json.loads(raw[header_start:header_end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧头不是有效 JSON") from exc
    if not isinstance(header, dict):
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧头必须是 JSON object")
    if header.get("type") != "audio_frame":
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧类型非法")
    if header.get("version") != AUDIO_FRAME_VERSION:
        raise AudioValidationError("UNSUPPORTED_AUDIO_FRAME", "不支持的二进制音频帧版本")

    payload = raw[header_end:]
    if not payload:
        raise AudioValidationError("INVALID_DATA", "缺少音频数据")
    if len(payload) > GATEWAY_MAX_AUDIO_RAW_BYTES:
        raise AudioValidationError(
            "AUDIO_TOO_LARGE",
            f"音频数据过大，原始大小最大 {GATEWAY_MAX_AUDIO_RAW_BYTES} bytes",
            details={
                "audio_bytes": len(payload),
                "max_audio_bytes": GATEWAY_MAX_AUDIO_RAW_BYTES,
            },
        )
    return header, payload


def decode_client_audio_frame(frame: bytes) -> dict[str, Any]:
    header, payload = decode_audio_frame(frame)
    direction = header.get("direction")
    encoding = header.get("encoding")
    stream_event = header.get("stream_event")
    event_type = header.get("event_type")
    if direction not in (None, "client_input"):
        raise AudioValidationError("INVALID_AUDIO_FRAME", "二进制音频帧方向非法")
    if encoding != "opus":
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            "当前客户端上行二进制音频仅支持 Opus",
            details={"encoding": encoding},
        )

    if event_type not in (None, "", "turn_candidate", "barge_in_probe"):
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FRAME",
            "不支持的客户端音频 event_type",
            details={"event_type": event_type},
        )

    data: dict[str, Any] = {
        "type": event_type if event_type in {"turn_candidate", "barge_in_probe"} else (
            "audio_chunk" if stream_event == "chunk" else "audio"
        ),
        "audio_bytes": payload,
        "audio_encoding": encoding,
        "audio_transport": "binary_stream_chunk" if stream_event == "chunk" else "binary_frame",
    }
    for key in (
        "bot_id",
        "trace_id",
        "round_id",
        "playback_id",
        "utterance_id",
        "seq",
        "timestamp_ms",
        "duration_ms",
        "sample_rate",
        "channels",
        "opus_frame_ms",
        "packet_count",
        "chunk_seq",
        "stream_event",
        "event_type",
        "context_session_id",
        "candidate_seq",
        "speech_epoch",
        "audio_watermark",
        "silence_ms",
        "shadow",
    ):
        if key in header:
            data[key] = header[key]
    return data


def decode_ws_text_message(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AudioValidationError("INVALID_JSON", "WebSocket 文本消息不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise AudioValidationError("INVALID_JSON", "WebSocket 文本消息必须是 JSON object")
    return data
