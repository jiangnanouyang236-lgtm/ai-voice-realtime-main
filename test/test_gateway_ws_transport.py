import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ["CONFIG_DATABASE_URL"] = ""

import gateway.opus_audio as opus_audio
from gateway.audio_protocol import decode_audio_frame
from gateway.ws_transport import (
    WebSocketBackpressureError,
    send_audio_pcm_message,
    send_error_message,
    send_json_message,
)


def test_send_json_message_sends_typed_payload():
    websocket = _FakeWebSocket()
    logger = _FakeLogger()

    elapsed = asyncio.run(send_json_message(websocket, "status", logger=logger, message="ok"))

    assert elapsed >= 0
    assert websocket.json_messages == [{"type": "status", "message": "ok"}]
    assert logger.debug_messages[-1][0] == "发送消息: %s"


def test_send_error_message_sends_error_and_logs():
    websocket = _FakeWebSocket()
    logger = _FakeLogger()

    asyncio.run(send_error_message(websocket, "BAD", "坏消息", logger=logger))

    assert websocket.json_messages == [{"type": "error", "code": "BAD", "message": "坏消息"}]
    assert logger.error_messages == [("错误: %s - %s", ("BAD", "坏消息"))]


def test_send_audio_pcm_message_encodes_server_tts_frame():
    websocket = _FakeWebSocket()
    logger = _FakeLogger()
    original_opuslib = opus_audio.opuslib
    opus_audio.opuslib = SimpleNamespace(Encoder=_FakeOpusEncoder, APPLICATION_AUDIO=2049)
    try:
        elapsed = asyncio.run(
            send_audio_pcm_message(
                websocket,
                b"\x00\x00" * 320,
                header={
                    "sample_rate": 16000,
                    "channels": 1,
                    "trace_id": "trace-1",
                    "round_id": "round-1",
                    "playback_id": "round-1:playback",
                    "chunk_seq": 2,
                },
                ws_send_timeout_sec=1,
                ws_audio_slow_send_ms=0,
                logger=logger,
            )
        )
    finally:
        opus_audio.opuslib = original_opuslib

    header, payload = decode_audio_frame(websocket.binary_messages[0])

    assert elapsed >= 0
    assert header["direction"] == "server_tts"
    assert header["encoding"] == "opus"
    assert header["trace_id"] == "trace-1"
    assert header["round_id"] == "round-1"
    assert header["playback_id"] == "round-1:playback"
    assert header["chunk_seq"] == 2
    assert header["opus_frame_ms"] == 20
    assert header["packet_count"] == 1
    assert header["pcm_bytes"] == 640
    assert payload == b"OPUSRAW1\x00\x02OP"


def test_send_audio_pcm_message_reports_slow_send():
    websocket = _SlowFakeWebSocket(delay_seconds=0.01)
    logger = _FakeLogger()
    original_opuslib = opus_audio.opuslib
    opus_audio.opuslib = SimpleNamespace(Encoder=_FakeOpusEncoder, APPLICATION_AUDIO=2049)
    try:
        with pytest.raises(WebSocketBackpressureError) as ctx:
            asyncio.run(
                send_audio_pcm_message(
                    websocket,
                    b"\x00\x00" * 320,
                    header={"sample_rate": 16000, "channels": 1},
                    ws_send_timeout_sec=1,
                    ws_audio_slow_send_ms=1,
                    logger=logger,
                )
            )
    finally:
        opus_audio.opuslib = original_opuslib

    assert ctx.value.reason == "ws_slow_send"
    assert ctx.value.threshold_ms == 1.0
    assert ctx.value.send_time_ms >= 1.0


class _FakeWebSocket:
    def __init__(self):
        self.json_messages = []
        self.binary_messages = []

    async def send_json(self, message):
        self.json_messages.append(message)

    async def send_bytes(self, payload):
        self.binary_messages.append(payload)


class _SlowFakeWebSocket(_FakeWebSocket):
    def __init__(self, delay_seconds: float):
        super().__init__()
        self.delay_seconds = delay_seconds

    async def send_bytes(self, payload):
        await asyncio.sleep(self.delay_seconds)
        await super().send_bytes(payload)


class _FakeLogger:
    def __init__(self):
        self.debug_messages = []
        self.warning_messages = []
        self.error_messages = []

    def debug(self, message, *args):
        self.debug_messages.append((message, args))

    def warning(self, message, *args):
        self.warning_messages.append((message, args))

    def error(self, message, *args):
        self.error_messages.append((message, args))


class _FakeOpusEncoder:
    def __init__(self, sample_rate: int, channels: int, application: int):
        self.sample_rate = sample_rate
        self.channels = channels
        self.application = application

    def encode(self, frame: bytes, frame_size: int) -> bytes:
        return b"OP"
