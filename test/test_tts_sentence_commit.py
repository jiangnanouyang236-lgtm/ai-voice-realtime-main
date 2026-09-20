import json
import os
import queue
import threading
import unittest
from unittest.mock import patch

import grpc

os.environ.setdefault("QWEN3_TTS_CUSTOM_VOICE_API_KEY", "test-key")
os.environ["CONFIG_DATABASE_URL"] = ""

from server_config.models import TTSProfileConfig
from tts import tts_service_pb2
from tts.tts_grpc_server import LocalQwen3TTSBridge
from tts.tts_grpc_server import TTSAudioQueueFullError
from tts.tts_grpc_server import TTS_OUTPUT_SAMPLE_RATE
from tts.tts_grpc_server import TTSServiceServicer
from tts.tts_grpc_server import _build_provider_session
from tts.tts_grpc_server import _resolve_local_qwen3_voice
from tts.tts_grpc_server import clean_text_for_tts


def _tts_config(profile_id: str = "default_tts_profile") -> tts_service_pb2.TTSConfig:
    return tts_service_pb2.TTSConfig(tts_profile_id=profile_id)


class TTSDirectStreamingTest(unittest.TestCase):
    def setUp(self):
        self._custom_voice_key_patch = patch(
            "tts.tts_grpc_server.QWEN3_TTS_CUSTOM_VOICE_API_KEY",
            "test-key",
        )
        self._custom_voice_key_patch.start()

    def tearDown(self):
        self._custom_voice_key_patch.stop()

    def test_text_stream_sends_each_cleaned_character_immediately(self):
        ws = _FakeWebSocket()
        bridge = LocalQwen3TTSBridge(queue.Queue())
        context = _FakeContext()
        chunks = [
            tts_service_pb2.TextChunk(text="我帮你查一下天气，稍等一下。", is_final=False, config=_tts_config()),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        TTSServiceServicer()._process_text_stream(chunks, ws, context, bridge)

        payloads = ws.sent_payloads()
        self.assertEqual("session.config", payloads[0]["type"])
        self.assertNotIn("split_granularity", payloads[0])
        input_texts = [payload["text"] for payload in payloads if payload["type"] == "input.text"]
        self.assertEqual(list("我帮你查一下天气，稍等一下。"), input_texts)
        self.assertEqual({"type": "input.done"}, payloads[-1])

    def test_text_stream_falls_back_from_unsupported_voice(self):
        ws = _FakeWebSocket()
        bridge = LocalQwen3TTSBridge(queue.Queue())
        context = _FakeContext()
        chunks = [
            tts_service_pb2.TextChunk(
                text="你好。",
                is_final=False,
                config=_tts_config(),
            ),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        TTSServiceServicer()._process_text_stream(chunks, ws, context, bridge)

        self.assertEqual("serena", ws.sent_payloads()[0]["voice"])

    def test_resolve_local_qwen3_voice_accepts_case_insensitive_supported_voice(self):
        self.assertEqual("serena", _resolve_local_qwen3_voice("Serena"))

    def test_qwen3_base_profile_uses_file_uri_ref_audio(self):
        profile = TTSProfileConfig(
            tts_id="base_leijun",
            tts_name="Base Leijun",
            provider_type="qwen3_base",
            speed=1.0,
            provider_config={
                "path": "/base/leijun-6s.mp3",
                "content": "大家好，我是雷军。",
            },
        )

        connection, payload = _build_provider_session(profile)

        self.assertEqual("qwen3_base", connection["provider_type"])
        self.assertEqual("Base", payload["task_type"])
        self.assertEqual("file:///base/leijun-6s.mp3", payload["ref_audio"])
        self.assertEqual("大家好，我是雷军。", payload["ref_text"])

    def test_text_stream_sends_each_character_immediately_for_short_prefix(self):
        ws = _FakeWebSocket()
        bridge = LocalQwen3TTSBridge(queue.Queue())
        context = _FakeContext()
        chunks = [
            tts_service_pb2.TextChunk(text="从前，", is_final=False, config=_tts_config()),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        TTSServiceServicer()._process_text_stream(chunks, ws, context, bridge)

        input_texts = [payload["text"] for payload in ws.sent_payloads() if payload["type"] == "input.text"]
        self.assertEqual(["从", "前", "，"], input_texts)
        self.assertEqual({"type": "input.done"}, ws.sent_payloads()[-1])

    def test_clean_text_normalizes_decimal_prices_for_speech(self):
        text = clean_text_for_tts("当前黄金价格是955.34元/克。")

        self.assertEqual("当前黄金价格是955点34元每克。", text)

    def test_clean_text_normalizes_temperature_and_percent_for_speech(self):
        text = clean_text_for_tts("气温25℃，涨幅3.5%。")

        self.assertEqual("气温25摄氏度，涨幅百分之3点5。", text)

    def test_clean_text_keeps_versions_and_ip_dots(self):
        text = clean_text_for_tts("版本v1.2，地址127.0.0.1。")

        self.assertEqual("版本v1.2，地址127.0.0.1。", text)

    def test_local_qwen3_event_marks_session_done(self):
        bridge = LocalQwen3TTSBridge(queue.Queue())

        TTSServiceServicer()._handle_local_qwen3_event('{"type":"session.done"}', bridge)

        self.assertTrue(bridge.complete_event.is_set())
        self.assertIsNone(bridge.error)

    def test_local_qwen3_event_marks_error(self):
        bridge = LocalQwen3TTSBridge(queue.Queue())

        TTSServiceServicer()._handle_local_qwen3_event(
            '{"type":"error","error":{"message":"provider failed"}}',
            bridge,
        )

        self.assertTrue(bridge.complete_event.is_set())
        self.assertIsInstance(bridge.error, dict)
        self.assertEqual("provider failed", bridge.error["message"])

    def test_text_stream_sends_multiple_input_text_events_without_commit_or_done_waits(self):
        ws = _FakeWebSocket()
        bridge = LocalQwen3TTSBridge(queue.Queue())
        context = _FakeContext()
        text = (
            "小游戏完成，总分 2 分。有一些表现值得继续观察。"
            "记录：近期事件遗忘：从不；重复提问：从不；词汇寻找困难：经常。"
            "这不是医学诊断，只作日常参考。"
        )
        chunks = [
            tts_service_pb2.TextChunk(text=text, is_final=False, config=_tts_config()),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        TTSServiceServicer()._process_text_stream(chunks, ws, context, bridge)

        payloads = ws.sent_payloads()
        event_types = [payload["type"] for payload in payloads]
        self.assertEqual("session.config", event_types[0])
        self.assertEqual(1, event_types.count("session.config"))
        self.assertGreater(event_types.count("input.text"), 1)
        input_text = "".join(payload["text"] for payload in payloads if payload["type"] == "input.text")
        self.assertEqual(text, input_text)
        self.assertNotIn("commit", event_types)
        self.assertNotIn("response.done", event_types)
        self.assertEqual(1, event_types.count("input.done"))
        self.assertEqual("input.done", event_types[-1])

    def test_multiple_gateway_text_chunks_share_one_local_qwen3_session(self):
        ws = _FakeWebSocket()
        bridge = LocalQwen3TTSBridge(queue.Queue())
        context = _FakeContext()
        chunks = [
            tts_service_pb2.TextChunk(text="好的，那我们来做个小游戏吧，一共三题。", is_final=False, config=_tts_config()),
            tts_service_pb2.TextChunk(text="第 1 题：你最近是否经常忘记刚发生的事情？", is_final=False),
            tts_service_pb2.TextChunk(text="请回答经常、偶尔或从不。", is_final=False),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        TTSServiceServicer()._process_text_stream(chunks, ws, context, bridge)

        payloads = ws.sent_payloads()
        event_types = [payload["type"] for payload in payloads]
        input_text = "".join(payload["text"] for payload in payloads if payload["type"] == "input.text")
        self.assertEqual(1, event_types.count("session.config"))
        self.assertEqual(1, event_types.count("input.done"))
        self.assertEqual(
            "好的，那我们来做个小游戏吧，一共三题。"
            "第 1 题：你最近是否经常忘记刚发生的事情？"
            "请回答经常、偶尔或从不。",
            input_text,
        )
        self.assertNotIn("commit", event_types)
        self.assertNotIn("response.done", event_types)

    def test_tts_bridge_marks_queue_full_as_error(self):
        audio_queue = queue.Queue(maxsize=1)
        audio_queue.put_nowait(b"already-buffered")
        bridge = LocalQwen3TTSBridge(audio_queue)

        with patch("tts.tts_grpc_server.TTS_AUDIO_QUEUE_PUT_TIMEOUT_SEC", 0.0):
            bridge.add_audio(b"\x00\x01")

        self.assertTrue(bridge.complete_event.is_set())
        self.assertIsInstance(bridge.error, TTSAudioQueueFullError)
        self.assertEqual(1, audio_queue.qsize())

    def test_tts_bridge_ignores_empty_binary_audio_payloads(self):
        audio_queue = queue.Queue()
        bridge = LocalQwen3TTSBridge(audio_queue)

        bridge.add_audio(b"")
        bridge.add_audio(b"\x00\x01")

        self.assertIsNone(bridge.error)
        self.assertFalse(bridge.complete_event.is_set())
        self.assertEqual(1, bridge.empty_binary_payloads)
        self.assertEqual(b"\x00\x01", audio_queue.get_nowait())

    def test_stream_text_to_speech_returns_16k_audio_and_final_marker(self):
        fake_ws = _StreamingFakeWebSocket([b"\x00\x00" * 240, '{"type":"session.done"}'])
        context = _FakeGrpcContext()
        chunks = [
            tts_service_pb2.TextChunk(
                text="你好，测试一下本地语音。",
                is_final=False,
                config=_tts_config(),
            ),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        result = list(TTSServiceServicer(ws_connect=lambda *args, **kwargs: fake_ws).StreamTextToSpeech(chunks, context))

        self.assertIsNone(context.code)
        self.assertEqual(2, len(result))
        self.assertFalse(result[0].is_final)
        self.assertTrue(result[0].audio_data)
        self.assertEqual(TTS_OUTPUT_SAMPLE_RATE, result[0].sample_rate)
        self.assertTrue(result[-1].is_final)
        self.assertEqual(b"", result[-1].audio_data)
        self.assertTrue(fake_ws.closed)

    def test_stream_text_to_speech_returns_internal_on_ws_error(self):
        fake_ws = _StreamingFakeWebSocket(['{"type":"error","error":{"message":"provider failed"}}'])
        context = _FakeGrpcContext()
        chunks = [
            tts_service_pb2.TextChunk(text="你好。", is_final=False, config=_tts_config()),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        result = list(TTSServiceServicer(ws_connect=lambda *args, **kwargs: fake_ws).StreamTextToSpeech(chunks, context))

        self.assertEqual([], result)
        self.assertEqual(grpc.StatusCode.INTERNAL, context.code)
        self.assertIn("provider failed", context.details)
        self.assertTrue(fake_ws.closed)

    def test_stream_text_to_speech_errors_when_done_has_no_audio_for_text(self):
        fake_ws = _StreamingFakeWebSocket(['{"type":"session.done"}'])
        context = _FakeGrpcContext()
        chunks = [
            tts_service_pb2.TextChunk(text="你好。", is_final=False, config=_tts_config()),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        result = list(TTSServiceServicer(ws_connect=lambda *args, **kwargs: fake_ws).StreamTextToSpeech(chunks, context))

        self.assertEqual([], result)
        self.assertEqual(grpc.StatusCode.INTERNAL, context.code)
        self.assertIn("completed without audio", context.details)
        self.assertTrue(fake_ws.closed)

    def test_stream_text_to_speech_closes_ws_on_context_cancel(self):
        fake_ws = _BlockingAfterFirstAudioWebSocket()
        context = _CancellableFakeGrpcContext()
        chunks = [
            tts_service_pb2.TextChunk(text="你好。", is_final=False, config=_tts_config()),
            tts_service_pb2.TextChunk(text="", is_final=True),
        ]

        stream = TTSServiceServicer(ws_connect=lambda *args, **kwargs: fake_ws).StreamTextToSpeech(chunks, context)
        first = next(stream)
        self.assertFalse(first.is_final)
        self.assertTrue(first.audio_data)
        self.assertTrue(fake_ws.recv_blocked.wait(timeout=2.0))

        context.cancel()

        with self.assertRaises(StopIteration):
            next(stream)
        self.assertTrue(fake_ws.closed)
        self.assertTrue(fake_ws.closed_event.is_set())

    def test_connect_local_qwen3_ws_sets_independent_recv_timeout(self):
        fake_ws = _TimeoutTrackingWebSocket()
        captured_kwargs = {}

        def fake_connect(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return fake_ws

        with patch("tts.tts_grpc_server.LOCAL_QWEN3_TTS_RECV_TIMEOUT_SEC", 42.0):
            TTSServiceServicer(ws_connect=fake_connect)._connect_local_qwen3_ws(
                {
                    "provider_type": "qwen3_custom_voice",
                    "tts_profile_id": "default_tts_profile",
                    "tts_name": "Default CustomVoice",
                    "ws_url": "ws://tts.example/v1/audio/speech/stream",
                    "api_key": "test-key",
                    "model": "qwen3-tts",
                }
            )

        self.assertEqual(42.0, fake_ws.timeout)
        self.assertNotEqual(captured_kwargs["timeout"], fake_ws.timeout)


class _FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True

    def sent_payloads(self):
        return [json.loads(payload) for payload in self.sent]


class _TimeoutTrackingWebSocket(_FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout


class _StreamingFakeWebSocket(_FakeWebSocket):
    def __init__(self, incoming_after_done):
        super().__init__()
        self.incoming_after_done = queue.Queue()
        for item in incoming_after_done:
            self.incoming_after_done.put(item)
        self.input_done = threading.Event()

    def send(self, payload):
        super().send(payload)
        parsed = json.loads(payload)
        if parsed.get("type") == "input.done":
            self.input_done.set()

    def recv(self):
        if not self.input_done.wait(timeout=2.0):
            raise TimeoutError("input.done was not sent")
        return self.incoming_after_done.get(timeout=2.0)


class _BlockingAfterFirstAudioWebSocket(_FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.input_done = threading.Event()
        self.closed_event = threading.Event()
        self.recv_blocked = threading.Event()
        self.first_audio_sent = False

    def send(self, payload):
        super().send(payload)
        parsed = json.loads(payload)
        if parsed.get("type") == "input.done":
            self.input_done.set()

    def recv(self):
        if not self.input_done.wait(timeout=2.0):
            raise TimeoutError("input.done was not sent")
        if not self.first_audio_sent:
            self.first_audio_sent = True
            return b"\x00\x00" * 240
        self.recv_blocked.set()
        if not self.closed_event.wait(timeout=2.0):
            raise TimeoutError("websocket close was not called")
        return None

    def close(self):
        super().close()
        self.closed_event.set()


class _FakeContext:
    def is_active(self):
        return True


class _FakeGrpcContext:
    def __init__(self):
        self.code = None
        self.details = None

    def is_active(self):
        return True

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


class _CancellableFakeGrpcContext(_FakeGrpcContext):
    def __init__(self):
        super().__init__()
        self.active = True
        self.callbacks = []

    def is_active(self):
        return self.active

    def add_callback(self, callback):
        self.callbacks.append(callback)
        return self.active

    def cancel(self):
        self.active = False
        for callback in list(self.callbacks):
            callback()


if __name__ == "__main__":
    unittest.main()
