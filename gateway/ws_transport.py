from __future__ import annotations

import asyncio
import time
from typing import Any

from gateway.audio_protocol import header_int
from gateway.grpc_control import grpc_timeout_arg
from gateway.opus_audio import (
    DEFAULT_TTS_SAMPLE_RATE,
    OpusPCMStreamEncoder,
    encode_server_tts_pcm_frame,
)


class WebSocketBackpressureError(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        send_time_ms: float,
        threshold_ms: float | None = None,
        timeout_sec: float | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.send_time_ms = send_time_ms
        self.threshold_ms = threshold_ms
        self.timeout_sec = timeout_sec


async def send_json_message(websocket, msg_type: str, *, logger, **kwargs) -> float:
    """发送 JSON 消息到客户端。"""
    t1 = time.time()
    message = {"type": msg_type, **kwargs}
    await websocket.send_json(message)
    send_time = (time.time() - t1) * 1000
    if send_time > 10:
        logger.warning("发送消息 %s 耗时: %.0fms", msg_type, send_time)
    logger.debug("发送消息: %s", msg_type)
    return send_time


async def send_audio_pcm_message(
    websocket,
    payload: bytes,
    *,
    header: dict[str, Any],
    ws_send_timeout_sec: float | int | None,
    ws_audio_slow_send_ms: float | int | None,
    logger,
    opus_stream_encoder: OpusPCMStreamEncoder | None = None,
    finalize_opus_stream: bool = False,
) -> float:
    """发送 TTS PCM 音频，并将慢客户端转成可控 backpressure 信号。"""
    t1 = time.time()
    frame_header = dict(header)
    frame_header.setdefault("timestamp_ms", int(t1 * 1000))
    sample_rate = header_int(frame_header.get("sample_rate"), DEFAULT_TTS_SAMPLE_RATE)
    channels = header_int(frame_header.get("channels"), 1)
    frame, _opus_metadata = encode_server_tts_pcm_frame(
        payload,
        sample_rate=sample_rate,
        channels=channels,
        header=frame_header,
        stream_encoder=opus_stream_encoder,
        finalize_stream=finalize_opus_stream,
    )
    timeout = grpc_timeout_arg(ws_send_timeout_sec)
    try:
        if timeout:
            await asyncio.wait_for(websocket.send_bytes(frame), timeout=timeout)
        else:
            await websocket.send_bytes(frame)
    except asyncio.TimeoutError as exc:
        send_time_ms = (time.time() - t1) * 1000
        raise WebSocketBackpressureError(
            "ws_send_timeout",
            send_time_ms=send_time_ms,
            timeout_sec=timeout,
        ) from exc

    send_time_ms = (time.time() - t1) * 1000
    slow_threshold_ms = float(ws_audio_slow_send_ms or 0)
    if slow_threshold_ms > 0 and send_time_ms >= slow_threshold_ms:
        raise WebSocketBackpressureError(
            "ws_slow_send",
            send_time_ms=send_time_ms,
            threshold_ms=slow_threshold_ms,
            timeout_sec=timeout,
        )
    if send_time_ms > 50:
        logger.warning("发送二进制音频帧耗时: %.0fms", send_time_ms)
    logger.debug("发送消息: audio_frame")
    return send_time_ms


async def send_error_message(websocket, code: str, message: str, *, logger) -> None:
    """发送错误消息到客户端。"""
    await send_json_message(websocket, "error", logger=logger, code=code, message=message)
    logger.error("错误: %s - %s", code, message)
