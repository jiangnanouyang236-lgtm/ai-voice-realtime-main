"""WAV/PCM validation and conversion helpers for gateway audio payloads."""

from __future__ import annotations

import io
import logging
import wave
from typing import Any

from gateway.audio_protocol import AudioValidationError
from gateway.config import (
    GATEWAY_ALLOWED_AUDIO_CHANNELS,
    GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
    GATEWAY_ALLOWED_SAMPLE_WIDTH_BYTES,
    GATEWAY_MAX_AUDIO_DURATION_MS,
)


logger = logging.getLogger(__name__)


def validate_wav_audio(wav_data: bytes) -> dict[str, Any]:
    """Validate the short-utterance WAV contract used by the current Rust client."""
    try:
        with wave.open(io.BytesIO(wav_data), "rb") as wf:
            sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frames = wf.getnframes()
    except wave.Error as exc:
        raise AudioValidationError("INVALID_AUDIO_DATA", "音频数据不是有效 WAV") from exc
    except Exception as exc:
        raise AudioValidationError("INVALID_AUDIO_DATA", "音频数据解析失败") from exc

    if sample_rate <= 0:
        raise AudioValidationError("INVALID_AUDIO_DATA", "WAV 采样率无效")
    duration_ms = frames / sample_rate * 1000.0 if frames else 0.0
    metadata = {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "frames": frames,
        "duration_ms": round(duration_ms, 2),
    }

    if sample_rate != GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的采样率: {sample_rate}Hz，仅支持 {GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE}Hz",
            details=metadata,
        )
    if channels != GATEWAY_ALLOWED_AUDIO_CHANNELS:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的声道数: {channels}，仅支持 {GATEWAY_ALLOWED_AUDIO_CHANNELS} 声道",
            details=metadata,
        )
    if sample_width != GATEWAY_ALLOWED_SAMPLE_WIDTH_BYTES:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的采样位宽: {sample_width} bytes，仅支持 {GATEWAY_ALLOWED_SAMPLE_WIDTH_BYTES} bytes",
            details=metadata,
        )
    if frames <= 0:
        raise AudioValidationError(
            "INVALID_AUDIO_DATA",
            "音频数据为空",
            details=metadata,
        )
    if duration_ms > GATEWAY_MAX_AUDIO_DURATION_MS:
        metadata.update(
            {
                "duration_limit_exceeded": True,
                "max_duration_ms": GATEWAY_MAX_AUDIO_DURATION_MS,
            }
        )

    return metadata


def truncate_wav_audio_to_limit(wav_data: bytes) -> tuple[bytes, dict[str, Any]]:
    """Trim valid WAV audio to the configured ASR duration budget."""
    metadata = validate_wav_audio(wav_data)
    if not metadata.get("duration_limit_exceeded"):
        return wav_data, metadata

    max_frames = max(1, int(metadata["sample_rate"] * GATEWAY_MAX_AUDIO_DURATION_MS / 1000))
    try:
        with wave.open(io.BytesIO(wav_data), "rb") as wf:
            trimmed_frames = wf.readframes(max_frames)
    except wave.Error as exc:
        raise AudioValidationError("INVALID_AUDIO_DATA", "音频数据不是有效 WAV") from exc
    except Exception as exc:
        raise AudioValidationError("INVALID_AUDIO_DATA", "音频数据解析失败") from exc

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(metadata["channels"])
        wf.setsampwidth(metadata["sample_width"])
        wf.setframerate(metadata["sample_rate"])
        wf.writeframes(trimmed_frames)

    truncated_data = buffer.getvalue()
    truncated_metadata = validate_wav_audio(truncated_data)
    truncated_metadata.update(
        {
            "truncated": True,
            "duration_limit_exceeded": True,
            "original_audio_bytes": len(wav_data),
            "original_duration_ms": metadata["duration_ms"],
            "original_frames": metadata["frames"],
            "max_duration_ms": GATEWAY_MAX_AUDIO_DURATION_MS,
        }
    )
    return truncated_data, truncated_metadata


def pcm16_to_wav_bytes(pcm_data: bytes, sample_rate: int, channels: int) -> bytes:
    if sample_rate != GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的 Opus 采样率: {sample_rate}Hz，仅支持 {GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE}Hz",
            details={"sample_rate": sample_rate},
        )
    if channels != GATEWAY_ALLOWED_AUDIO_CHANNELS:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的 Opus 声道数: {channels}，仅支持 {GATEWAY_ALLOWED_AUDIO_CHANNELS} 声道",
            details={"channels": channels},
        )
    frame_bytes = channels * GATEWAY_ALLOWED_SAMPLE_WIDTH_BYTES
    if not pcm_data or len(pcm_data) % frame_bytes != 0:
        raise AudioValidationError("INVALID_AUDIO_DATA", "Opus 解码后的 PCM 数据长度非法")

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(GATEWAY_ALLOWED_SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buffer.getvalue()


def wav_to_pcm(wav_data: bytes) -> tuple[bytes, int]:
    """
    将 WAV 格式转换为 PCM 格式

    Args:
        wav_data: WAV 格式的音频数据

    Returns:
        (pcm_data, sample_rate): PCM 数据和采样率
    """
    try:
        wav_data, _metadata = truncate_wav_audio_to_limit(wav_data)
        wav_buffer = io.BytesIO(wav_data)
        with wave.open(wav_buffer, "rb") as wf:
            sample_rate = wf.getframerate()
            pcm_data = wf.readframes(wf.getnframes())

        return pcm_data, sample_rate
    except AudioValidationError:
        raise
    except Exception as e:
        logger.error("WAV 转 PCM 失败: %s", e)
        raise
