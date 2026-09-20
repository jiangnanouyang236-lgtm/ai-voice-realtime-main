"""Opus packet stream, stream assembly, and codec helpers."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

try:
    import opuslib
except Exception:  # opuslib raises a generic Exception when native libopus is absent.
    opuslib = None

from gateway.audio_protocol import (
    AudioValidationError,
    encode_audio_frame,
    header_int as _header_int,
)
from gateway.config import (
    GATEWAY_ALLOWED_AUDIO_CHANNELS,
    GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
    GATEWAY_MAX_AUDIO_RAW_BYTES,
    OPUS_BITRATE_BPS,
)
from gateway.wav_audio import pcm16_to_wav_bytes


logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


DEFAULT_TTS_SAMPLE_RATE = 16000
OPUS_PACKET_STREAM_MAGIC = b"OPUSRAW1"
OPUS_DEFAULT_FRAME_MS = 20
GATEWAY_AUDIO_STREAM_MAX_CHUNKS = max(1, _env_int("GATEWAY_AUDIO_STREAM_MAX_CHUNKS", 256))
_OPUS_BITRATE_SET_UNSUPPORTED = False
_OPUS_BITRATE_SET_LOCK = threading.Lock()


def parse_opus_packet_stream(payload: bytes) -> list[bytes]:
    """Parse the compact OPUSRAW1 packet stream carried inside VAF1 payloads."""
    if not isinstance(payload, (bytes, bytearray)):
        raise AudioValidationError("INVALID_AUDIO_DATA", "Opus payload 必须是 bytes")

    raw = bytes(payload)
    if not raw.startswith(OPUS_PACKET_STREAM_MAGIC):
        raise AudioValidationError("INVALID_AUDIO_DATA", "Opus payload magic 不匹配")

    packets: list[bytes] = []
    offset = len(OPUS_PACKET_STREAM_MAGIC)
    while offset < len(raw):
        if offset + 2 > len(raw):
            raise AudioValidationError("INVALID_AUDIO_DATA", "Opus packet 长度字段不完整")
        packet_len = int.from_bytes(raw[offset:offset + 2], "big")
        offset += 2
        if packet_len <= 0:
            raise AudioValidationError("INVALID_AUDIO_DATA", "Opus packet 为空")
        packet_end = offset + packet_len
        if packet_end > len(raw):
            raise AudioValidationError("INVALID_AUDIO_DATA", "Opus packet 数据不完整")
        packets.append(raw[offset:packet_end])
        offset = packet_end

    if not packets:
        raise AudioValidationError("INVALID_AUDIO_DATA", "Opus payload 不包含音频 packet")
    return packets


def build_opus_packet_stream(packets: list[bytes]) -> bytes:
    if not packets:
        raise AudioValidationError("INVALID_AUDIO_DATA", "Opus packet 列表为空")
    output = bytearray(OPUS_PACKET_STREAM_MAGIC)
    for packet in packets:
        if not packet:
            raise AudioValidationError("INVALID_AUDIO_DATA", "Opus packet 为空")
        if len(packet) > 65535:
            raise AudioValidationError("INVALID_AUDIO_DATA", "Opus packet 过大")
        output.extend(len(packet).to_bytes(2, "big"))
        output.extend(packet)
    return bytes(output)


def audio_stream_utterance_id(data: dict[str, Any]) -> str:
    return str(data.get("utterance_id") or "").strip()


def build_audio_end_log_fields(
    audio_data: dict[str, Any],
    *,
    utterance_id: str,
    queue_size: int,
    dropped: int,
) -> dict[str, Any]:
    return {
        "utterance_id": utterance_id,
        "chunks": audio_data.get("stream_chunk_count"),
        "packets": audio_data.get("packet_count"),
        "opus_bytes": audio_data.get("stream_source_audio_bytes"),
        "queue_size": queue_size,
        "dropped": dropped,
        "elapsed_ms": audio_data.get("stream_elapsed_ms", 0.0),
    }


class AudioStreamAssembler:
    """Collect client Opus chunks into the existing batch-ASR audio payload."""

    def __init__(self, start: dict[str, Any]):
        utterance_id = str(start.get("utterance_id") or "").strip()
        if not utterance_id:
            raise AudioValidationError("INVALID_AUDIO_STREAM", "audio_start 缺少 utterance_id")
        self.utterance_id = utterance_id
        self.bot_id = start.get("bot_id")
        self.sample_rate = _header_int(start.get("sample_rate"), GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE)
        self.channels = _header_int(start.get("channels"), GATEWAY_ALLOWED_AUDIO_CHANNELS)
        self.opus_frame_ms = _header_int(start.get("opus_frame_ms"), OPUS_DEFAULT_FRAME_MS)
        if self.sample_rate != GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE:
            raise AudioValidationError(
                "UNSUPPORTED_AUDIO_FORMAT",
                f"不支持的 Opus 采样率: {self.sample_rate}Hz，仅支持 {GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE}Hz",
                details={"sample_rate": self.sample_rate},
            )
        if self.channels != GATEWAY_ALLOWED_AUDIO_CHANNELS:
            raise AudioValidationError(
                "UNSUPPORTED_AUDIO_FORMAT",
                f"不支持的 Opus 声道数: {self.channels}，仅支持 {GATEWAY_ALLOWED_AUDIO_CHANNELS} 声道",
                details={"channels": self.channels},
            )
        if self.opus_frame_ms != OPUS_DEFAULT_FRAME_MS:
            raise AudioValidationError(
                "UNSUPPORTED_AUDIO_FORMAT",
                f"不支持的 Opus frame_ms: {self.opus_frame_ms}，仅支持 {OPUS_DEFAULT_FRAME_MS}ms",
                details={"opus_frame_ms": self.opus_frame_ms},
            )
        self.started_at = time.time()
        self.expected_chunk_seq = 1
        self.chunks = 0
        self.source_audio_bytes = 0
        self.duration_ms = 0.0
        self.packets: list[bytes] = []

    def append(self, data: dict[str, Any]) -> None:
        if data.get("utterance_id") != self.utterance_id:
            raise AudioValidationError("INVALID_AUDIO_STREAM", "audio_chunk utterance_id 不匹配")
        chunk_seq = _header_int(data.get("chunk_seq") or data.get("seq"), self.expected_chunk_seq)
        if chunk_seq != self.expected_chunk_seq:
            raise AudioValidationError(
                "INVALID_AUDIO_STREAM",
                f"audio_chunk seq 不连续: expected={self.expected_chunk_seq}, got={chunk_seq}",
                details={"expected_chunk_seq": self.expected_chunk_seq, "chunk_seq": chunk_seq},
            )
        payload = data.get("audio_bytes")
        if not isinstance(payload, (bytes, bytearray)):
            raise AudioValidationError("INVALID_AUDIO_STREAM", "audio_chunk 缺少二进制 payload")
        packets = parse_opus_packet_stream(bytes(payload))
        if self.chunks + 1 > GATEWAY_AUDIO_STREAM_MAX_CHUNKS:
            raise AudioValidationError(
                "AUDIO_TOO_LARGE",
                f"音频分片过多，最大 {GATEWAY_AUDIO_STREAM_MAX_CHUNKS} 个 chunk",
                details={"max_chunks": GATEWAY_AUDIO_STREAM_MAX_CHUNKS},
            )
        self.packets.extend(packets)
        self.chunks += 1
        self.expected_chunk_seq += 1
        self.source_audio_bytes += len(payload)
        try:
            self.duration_ms += max(0.0, float(data.get("duration_ms") or 0.0))
        except (TypeError, ValueError):
            pass
        if self.source_audio_bytes > GATEWAY_MAX_AUDIO_RAW_BYTES:
            raise AudioValidationError(
                "AUDIO_TOO_LARGE",
                f"音频数据过大，原始大小最大 {GATEWAY_MAX_AUDIO_RAW_BYTES} bytes",
                details={
                    "audio_bytes": self.source_audio_bytes,
                    "max_audio_bytes": GATEWAY_MAX_AUDIO_RAW_BYTES,
                },
            )

    def finish(self, data: dict[str, Any]) -> dict[str, Any]:
        if data.get("utterance_id") != self.utterance_id:
            raise AudioValidationError("INVALID_AUDIO_STREAM", "audio_end utterance_id 不匹配")
        if not self.packets:
            raise AudioValidationError("INVALID_AUDIO_STREAM", "audio_end 前未收到音频分片")
        duration_ms = self.duration_ms
        try:
            explicit_duration_ms = float(data.get("duration_ms") or 0.0)
            if explicit_duration_ms > 0:
                duration_ms = explicit_duration_ms
        except (TypeError, ValueError):
            pass
        return {
            "type": "audio",
            "audio_bytes": build_opus_packet_stream(self.packets),
            "audio_encoding": "opus",
            "audio_transport": "binary_stream",
            "bot_id": data.get("bot_id") or self.bot_id,
            "utterance_id": self.utterance_id,
            "duration_ms": duration_ms,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "opus_frame_ms": self.opus_frame_ms,
            "packet_count": len(self.packets),
            "stream_chunk_count": self.chunks,
            "stream_source_audio_bytes": self.source_audio_bytes,
            "stream_elapsed_ms": round((time.time() - self.started_at) * 1000.0, 1),
        }


def decode_opus_audio_to_wav(
    payload: bytes,
    *,
    sample_rate: Any,
    channels: Any,
    duration_ms: Any = None,
    opus_frame_ms: Any = None,
) -> tuple[bytes, dict[str, Any]]:
    if opuslib is None:
        raise AudioValidationError(
            "OPUS_UNAVAILABLE",
            "Gateway 未安装 opuslib/libopus，无法解码 Opus 上行音频",
        )

    sample_rate_int = _header_int(sample_rate, GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE)
    channels_int = _header_int(channels, GATEWAY_ALLOWED_AUDIO_CHANNELS)
    frame_ms = _header_int(opus_frame_ms, OPUS_DEFAULT_FRAME_MS)
    if sample_rate_int != GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的 Opus 采样率: {sample_rate_int}Hz，仅支持 {GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE}Hz",
            details={"sample_rate": sample_rate_int},
        )
    if channels_int != GATEWAY_ALLOWED_AUDIO_CHANNELS:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的 Opus 声道数: {channels_int}，仅支持 {GATEWAY_ALLOWED_AUDIO_CHANNELS} 声道",
            details={"channels": channels_int},
        )
    if frame_ms != OPUS_DEFAULT_FRAME_MS:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            f"不支持的 Opus frame_ms: {frame_ms}，仅支持 {OPUS_DEFAULT_FRAME_MS}ms",
            details={"opus_frame_ms": frame_ms},
        )

    packets = parse_opus_packet_stream(payload)
    frame_size = int(sample_rate_int * frame_ms / 1000)
    if frame_size <= 0:
        raise AudioValidationError("INVALID_AUDIO_DATA", "Opus frame_size 非法")

    decoder = opuslib.Decoder(sample_rate_int, channels_int)
    pcm_parts: list[bytes] = []
    try:
        for packet in packets:
            pcm_parts.append(decoder.decode(packet, frame_size, decode_fec=False))
    except Exception as exc:
        raise AudioValidationError("INVALID_AUDIO_DATA", "Opus 音频解码失败") from exc

    pcm_data = b"".join(pcm_parts)
    try:
        duration_float = float(duration_ms)
    except (TypeError, ValueError):
        duration_float = 0.0
    if duration_float > 0:
        expected_bytes = int(round(sample_rate_int * duration_float / 1000.0)) * channels_int * 2
        if 0 < expected_bytes <= len(pcm_data):
            pcm_data = pcm_data[:expected_bytes]

    if len(pcm_data) > GATEWAY_MAX_AUDIO_RAW_BYTES:
        raise AudioValidationError(
            "AUDIO_TOO_LARGE",
            f"Opus 解码后音频过大，原始大小最大 {GATEWAY_MAX_AUDIO_RAW_BYTES} bytes",
            details={
                "decoded_pcm_bytes": len(pcm_data),
                "max_audio_bytes": GATEWAY_MAX_AUDIO_RAW_BYTES,
            },
        )

    wav_data = pcm16_to_wav_bytes(pcm_data, sample_rate_int, channels_int)
    metadata = {
        "source_audio_bytes": len(payload),
        "decoded_pcm_bytes": len(pcm_data),
        "opus_packets": len(packets),
        "opus_frame_ms": frame_ms,
        "sample_rate": sample_rate_int,
        "channels": channels_int,
    }
    return wav_data, metadata


def _set_opus_encoder_bitrate(encoder: Any) -> None:
    global _OPUS_BITRATE_SET_UNSUPPORTED
    if not _OPUS_BITRATE_SET_UNSUPPORTED:
        try:
            encoder.bitrate = OPUS_BITRATE_BPS
        except Exception as exc:
            with _OPUS_BITRATE_SET_LOCK:
                if not _OPUS_BITRATE_SET_UNSUPPORTED:
                    _OPUS_BITRATE_SET_UNSUPPORTED = True
                    logger.warning("设置 Opus bitrate=%s 失败，使用编码器默认值继续: %s", OPUS_BITRATE_BPS, exc)


class OpusPCMStreamEncoder:
    """Keep one Opus encoder state for a complete downlink playback stream."""

    def __init__(
        self,
        *,
        sample_rate: Any,
        channels: Any,
        opus_frame_ms: Any = None,
    ) -> None:
        if opuslib is None:
            raise AudioValidationError(
                "OPUS_UNAVAILABLE",
                "Gateway 未安装 opuslib/libopus，无法编码 Opus 下行音频",
            )
        self.sample_rate = _header_int(sample_rate, DEFAULT_TTS_SAMPLE_RATE)
        self.channels = _header_int(channels, 1)
        self.frame_ms = _header_int(opus_frame_ms, OPUS_DEFAULT_FRAME_MS)
        if self.sample_rate != DEFAULT_TTS_SAMPLE_RATE:
            raise AudioValidationError(
                "UNSUPPORTED_AUDIO_FORMAT",
                f"不支持的 TTS Opus 采样率: {self.sample_rate}Hz，仅支持 {DEFAULT_TTS_SAMPLE_RATE}Hz",
                details={"sample_rate": self.sample_rate},
            )
        if self.channels != 1:
            raise AudioValidationError(
                "UNSUPPORTED_AUDIO_FORMAT",
                f"不支持的 TTS Opus 声道数: {self.channels}，仅支持单声道",
                details={"channels": self.channels},
            )
        if self.frame_ms != OPUS_DEFAULT_FRAME_MS:
            raise AudioValidationError(
                "UNSUPPORTED_AUDIO_FORMAT",
                f"不支持的 TTS Opus frame_ms: {self.frame_ms}，仅支持 {OPUS_DEFAULT_FRAME_MS}ms",
                details={"opus_frame_ms": self.frame_ms},
            )
        self.frame_bytes = int(self.sample_rate * self.frame_ms / 1000) * self.channels * 2
        if self.frame_bytes <= 0:
            raise AudioValidationError("INVALID_AUDIO_DATA", "TTS Opus frame_bytes 非法")
        application = getattr(opuslib, "APPLICATION_AUDIO", 2049)
        self._encoder = opuslib.Encoder(self.sample_rate, self.channels, application)
        _set_opus_encoder_bitrate(self._encoder)
        self._pending = bytearray()
        self._finalized = False

    def encode(self, pcm_data: bytes, *, final: bool = False) -> tuple[bytes, dict[str, Any]]:
        if self._finalized:
            raise AudioValidationError("INVALID_AUDIO_DATA", "Opus 流已经结束")
        if not pcm_data:
            raise AudioValidationError("INVALID_AUDIO_DATA", "TTS PCM 数据为空")
        if len(pcm_data) % 2 != 0:
            raise AudioValidationError("INVALID_AUDIO_DATA", "TTS PCM16LE 数据长度非法")

        self._pending.extend(pcm_data)
        packets: list[bytes] = []
        while len(self._pending) >= self.frame_bytes:
            frame = bytes(self._pending[:self.frame_bytes])
            del self._pending[:self.frame_bytes]
            packets.append(self._encode_frame(frame))
        if final and self._pending:
            frame = bytes(self._pending) + b"\x00" * (self.frame_bytes - len(self._pending))
            self._pending.clear()
            packets.append(self._encode_frame(frame))
        if final:
            self._finalized = True
        if not packets:
            raise AudioValidationError("INVALID_AUDIO_DATA", "PCM 数据不足一个 Opus 帧")
        return build_opus_packet_stream(packets), {
            "opus_frame_ms": self.frame_ms,
            "packet_count": len(packets),
            "pcm_bytes": len(pcm_data),
        }

    def _encode_frame(self, frame: bytes) -> bytes:
        try:
            packet = self._encoder.encode(frame, self.frame_bytes // (self.channels * 2))
        except Exception as exc:
            raise AudioValidationError("INVALID_AUDIO_DATA", "TTS PCM 编码 Opus 失败") from exc
        if not packet:
            raise AudioValidationError("INVALID_AUDIO_DATA", "TTS Opus packet 为空")
        if len(packet) > 65535:
            raise AudioValidationError("INVALID_AUDIO_DATA", "TTS Opus packet 过大")
        return packet


def encode_pcm16_to_opus_packet_stream(
    pcm_data: bytes,
    *,
    sample_rate: Any,
    channels: Any,
    opus_frame_ms: Any = None,
) -> tuple[bytes, dict[str, Any]]:
    encoder = OpusPCMStreamEncoder(
        sample_rate=sample_rate,
        channels=channels,
        opus_frame_ms=opus_frame_ms,
    )
    return encoder.encode(pcm_data, final=True)


def encode_server_tts_pcm_frame(
    pcm_data: bytes,
    *,
    sample_rate: Any,
    channels: Any,
    header: dict[str, Any],
    stream_encoder: OpusPCMStreamEncoder | None = None,
    finalize_stream: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    sample_rate_int = _header_int(sample_rate, DEFAULT_TTS_SAMPLE_RATE)
    channels_int = _header_int(channels, 1)
    if stream_encoder is None:
        encoded_payload, opus_metadata = encode_pcm16_to_opus_packet_stream(
            pcm_data,
            sample_rate=sample_rate_int,
            channels=channels_int,
            opus_frame_ms=OPUS_DEFAULT_FRAME_MS,
        )
    else:
        if (stream_encoder.sample_rate, stream_encoder.channels) != (sample_rate_int, channels_int):
            raise AudioValidationError("UNSUPPORTED_AUDIO_FORMAT", "Opus 流编码器格式与 PCM 不一致")
        encoded_payload, opus_metadata = stream_encoder.encode(
            pcm_data,
            final=finalize_stream,
        )
    frame_header = dict(header)
    frame_header.update(opus_metadata)
    frame = encode_audio_frame(
        encoded_payload,
        direction="server_tts",
        encoding="opus",
        **frame_header,
    )
    return frame, opus_metadata
