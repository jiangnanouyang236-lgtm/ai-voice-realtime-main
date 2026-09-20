import asyncio
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import grpc

os.environ["CONFIG_DATABASE_URL"] = ""

from gateway import gateway_server as gateway
from gateway import opus_audio
from gateway.grpc_control import (
    DEFAULT_GRPC_CHANNEL_OPTIONS,
    channel_connectivity_label,
    create_grpc_channel,
    grpc_timeout_arg,
    is_channel_ready,
)
from llm import llm_service_pb2
from stt import stt_service_pb2
from tts import tts_service_pb2


class GatewayGrpcDeadlineTest(unittest.TestCase):
    def setUp(self):
        self._opuslib_patch = patch.object(
            opus_audio,
            "opuslib",
            SimpleNamespace(Encoder=_FakeOpusEncoder),
        )
        self._opuslib_patch.start()

    def tearDown(self):
        self._opuslib_patch.stop()

    def _round_session(self, trace_id: str = "trace-deadline"):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        manager.start_round(
            session_id,
            round_id=trace_id,
            playback_id=f"{trace_id}:playback",
        )
        return manager, session_id

    def test_grpc_timeout_arg_treats_zero_as_disabled(self):
        self.assertEqual(3.5, grpc_timeout_arg(3.5))
        self.assertIsNone(grpc_timeout_arg(0))
        self.assertIsNone(grpc_timeout_arg(None))
        self.assertIsNone(grpc_timeout_arg("not-a-number"))

    def test_channel_ready_accepts_ready_and_idle_states(self):
        self.assertTrue(is_channel_ready(_FakeGrpcChannel(grpc.ChannelConnectivity.READY)))
        self.assertTrue(is_channel_ready(_FakeGrpcChannel(grpc.ChannelConnectivity.IDLE)))
        self.assertTrue(is_channel_ready(_FakeGrpcChannel(grpc.ChannelConnectivity.CONNECTING)))
        self.assertFalse(is_channel_ready(_FakeGrpcChannel(grpc.ChannelConnectivity.TRANSIENT_FAILURE)))
        self.assertFalse(is_channel_ready(None))

    def test_channel_ready_accepts_real_grpc_integer_states(self):
        self.assertTrue(is_channel_ready(_FakeGrpcChannel(0)))
        self.assertTrue(is_channel_ready(_FakeGrpcChannel(1)))
        self.assertTrue(is_channel_ready(_FakeGrpcChannel(2)))
        self.assertFalse(is_channel_ready(_FakeGrpcChannel(3)))
        self.assertFalse(is_channel_ready(_FakeGrpcChannel(4)))

    def test_channel_connectivity_label_reports_state_or_error(self):
        self.assertEqual(
            "ready",
            channel_connectivity_label(_FakeGrpcChannel(grpc.ChannelConnectivity.READY)),
        )
        self.assertEqual("idle", channel_connectivity_label(_FakeGrpcChannel(0)))
        self.assertEqual("connecting", channel_connectivity_label(_FakeGrpcChannel(1)))
        self.assertEqual("ready", channel_connectivity_label(_FakeGrpcChannel(2)))
        self.assertEqual("missing", channel_connectivity_label(None))
        self.assertEqual(
            "unknown: boom",
            channel_connectivity_label(_BrokenGrpcChannel()),
        )

    def test_get_tts_stub_replaces_unhealthy_channel_without_closing_inflight_rpc(self):
        old_channel = _ClosableFakeGrpcChannel(grpc.ChannelConnectivity.TRANSIENT_FAILURE)
        old_stub = object()
        new_channel = _ClosableFakeGrpcChannel(grpc.ChannelConnectivity.IDLE)
        new_stub = object()

        with (
            patch.object(gateway, "tts_channel", old_channel),
            patch.object(gateway, "tts_stub", old_stub),
            patch.object(gateway, "_create_grpc_channel", return_value=new_channel),
            patch.object(gateway.tts_service_pb2_grpc, "TTSServiceStub", return_value=new_stub),
        ):
            actual = gateway.get_tts_stub()

            self.assertIs(new_stub, actual)
            self.assertIs(new_channel, gateway.tts_channel)
            self.assertEqual(0, old_channel.close_count)

    def test_create_grpc_channel_uses_default_options_for_insecure_channel(self):
        with patch(
            "gateway.grpc_control.grpc.insecure_channel",
            return_value="channel",
        ) as insecure_channel:
            channel = create_grpc_channel("127.0.0.1:50054", secure=False)

        self.assertEqual("channel", channel)
        insecure_channel.assert_called_once()
        self.assertEqual(("127.0.0.1:50054",), insecure_channel.call_args.args)
        self.assertEqual(
            list(DEFAULT_GRPC_CHANNEL_OPTIONS),
            insecure_channel.call_args.kwargs["options"],
        )

    def test_create_grpc_channel_uses_tls_credentials_for_secure_channel(self):
        with (
            patch(
                "gateway.grpc_control.grpc.ssl_channel_credentials",
                return_value="creds",
            ) as credentials,
            patch(
                "gateway.grpc_control.grpc.secure_channel",
                return_value="channel",
            ) as secure_channel,
        ):
            channel = create_grpc_channel("example.com:443", secure=True)

        self.assertEqual("channel", channel)
        credentials.assert_called_once()
        secure_channel.assert_called_once()
        self.assertEqual(("example.com:443", "creds"), secure_channel.call_args.args)
        self.assertEqual(
            list(DEFAULT_GRPC_CHANNEL_OPTIONS),
            secure_channel.call_args.kwargs["options"],
        )

    def test_process_asr_passes_configured_stt_timeout(self):
        stub = _FakeSTTStub()

        with (
            patch.object(gateway, "GATEWAY_STT_RPC_TIMEOUT_SEC", 2.5),
            patch.object(gateway, "get_stt_stub", return_value=stub),
            patch.object(gateway, "wav_to_pcm", return_value=(b"\x00\x00", 16000)),
        ):
            text, _duration_ms, _metadata = asyncio.run(
                gateway.process_asr(b"fake-wav", "session-deadline")
            )

        self.assertEqual("你好。", text)
        self.assertEqual(2.5, stub.timeout)
        self.assertEqual(b"\x00\x00", stub.request.audio_data)

    def test_process_asr_handles_stt_deadline_error(self):
        stub = _DeadlineSTTStub()

        with (
            patch.object(gateway, "GATEWAY_STT_RPC_TIMEOUT_SEC", 0.1),
            patch.object(gateway, "get_stt_stub", return_value=stub),
            patch.object(gateway, "wav_to_pcm", return_value=(b"\x00\x00", 16000)),
        ):
            text, duration_ms, metadata = asyncio.run(
                gateway.process_asr(b"fake-wav", "session-deadline")
            )

        self.assertIsNone(text)
        self.assertEqual(0, duration_ms)
        self.assertEqual({}, metadata)
        self.assertEqual(0.1, stub.timeout)

    def test_process_llm_tts_stream_passes_stream_timeouts(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _FakeTTSStub()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-deadline")

        with (
            patch.object(gateway, "GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC", 11.0),
            patch.object(gateway, "GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 12.0),
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试一下",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-deadline",
                        "round_seq": 1,
                        "round_id": "trace-deadline",
                        "playback_id": "trace-deadline:playback",
                    },
                    history_query="测试一下",
                )
            )

        self.assertEqual(11.0, llm_stub.timeout)
        self.assertEqual(12.0, tts_stub.timeout)
        self.assertEqual("测试一下", llm_stub.request.text)
        self.assertEqual("trace-deadline", llm_stub.request.trace_id)
        self.assertEqual(["你好。", ""], [chunk.text for chunk in tts_stub.text_chunks])
        self.assertEqual(
            ["playback_start", "audio_frame", "done"],
            [message["type"] for message in websocket.messages],
        )
        self.assertEqual("trace-deadline", websocket.messages[0]["round_id"])
        self.assertEqual("trace-deadline:playback", websocket.messages[0]["playback_id"])
        self.assertEqual("trace-deadline", websocket.messages[1]["round_id"])
        self.assertEqual("trace-deadline:playback", websocket.messages[1]["playback_id"])
        self.assertEqual(1, websocket.messages[1]["chunk_seq"])
        self.assertEqual(16000, websocket.messages[1]["sample_rate"])
        self.assertEqual("server_tts", websocket.messages[1]["direction"])
        self.assertEqual("opus", websocket.messages[1]["encoding"])
        self.assertEqual(20, websocket.messages[1]["opus_frame_ms"])
        self.assertEqual(1, websocket.messages[1]["packet_count"])
        self.assertEqual(2, websocket.messages[1]["pcm_bytes"])
        self.assertEqual(b"OPUSRAW1\x00\x02OP", websocket.messages[1]["payload"])
        self.assertEqual("trace-deadline", websocket.messages[2]["round_id"])
        self.assertEqual("trace-deadline:playback", websocket.messages[2]["playback_id"])

        metrics = recorder.get_round("trace-deadline")["trace"]["metrics"]
        self.assertGreaterEqual(metrics["llm_first_token_ms"], 0)
        self.assertGreaterEqual(metrics["llm_total_ms"], 0)
        self.assertEqual("chat", metrics["llm_router_kind"])
        self.assertEqual("no_tools", metrics["llm_router_source"])
        self.assertEqual(12.5, metrics["llm_router_ms"])
        self.assertEqual(5.0, metrics["llm_mcp_prepare_ms"])
        self.assertEqual(123.4, metrics["llm_service_total_ms"])
        self.assertGreaterEqual(metrics["tts_connect_ms"], 0)
        self.assertGreaterEqual(metrics["tts_first_commit_ms"], 0)
        self.assertGreaterEqual(metrics["tts_first_audio_ms"], 0)
        self.assertGreaterEqual(metrics["ws_send_ms"], 0)
        self.assertFalse(metrics["cancelled"])
        self.assertFalse(metrics["timeout"])

    def test_process_direct_tts_stream_bypasses_llm_and_uses_tts_config(self):
        tts_stub = _FakeTTSStub()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-event")

        with (
            patch.object(gateway, "GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 12.0),
            patch.object(gateway, "get_llm_stub", side_effect=AssertionError("LLM must not be used")),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
        ):
            asyncio.run(
                gateway.process_direct_tts_stream(
                    "我在呢，你说。",
                    session_id,
                    websocket,
                    bot_id="xiaowen",
                    bot_tts_settings={"tts_profile_id": "default_tts_profile"},
                    trace={
                        "trace_id": "trace-event",
                        "round_seq": 1,
                        "round_id": "trace-event",
                        "playback_id": "trace-event:playback",
                    },
                    event_type="wake_idle",
                )
            )

        self.assertEqual(12.0, tts_stub.timeout)
        self.assertEqual(["我在呢，你说。", ""], [chunk.text for chunk in tts_stub.text_chunks])
        self.assertEqual("default_tts_profile", tts_stub.text_chunks[0].config.tts_profile_id)
        self.assertEqual(
            ["playback_start", "audio_frame", "done"],
            [message["type"] for message in websocket.messages],
        )
        self.assertFalse(websocket.messages[-1]["exit"])
        trace = recorder.get_round("trace-event")["trace"]
        self.assertEqual("client_event_tts_done", trace["last_stage"])
        self.assertTrue(
            any(
                event["stage"] == "client_event_tts_start"
                and event["summary"]["event_type"] == "wake_idle"
                for event in trace["events"]
            )
        )
        metrics = trace["metrics"]
        self.assertEqual(1, metrics["audio_chunks"])
        self.assertGreaterEqual(metrics["tts_first_audio_ms"], 0)

    def test_process_direct_tts_stream_retries_once_on_closed_tts_channel_before_audio(self):
        failed_stub = _ChannelClosedTTSStub()
        ok_stub = _FakeTTSStub()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-retry-direct")

        with (
            patch.object(gateway, "GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 12.0),
            patch.object(gateway, "get_tts_stub", side_effect=[failed_stub, ok_stub]) as get_tts_stub,
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", 0.5),
        ):
            asyncio.run(
                gateway.process_direct_tts_stream(
                    "我在呢，你说。",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-retry-direct",
                        "round_seq": 1,
                        "round_id": "trace-retry-direct",
                        "playback_id": "trace-retry-direct:playback",
                    },
                    event_type="wake_idle",
                )
            )

        self.assertEqual(2, get_tts_stub.call_count)
        self.assertEqual(12.0, failed_stub.timeout)
        self.assertEqual(12.0, ok_stub.timeout)
        self.assertEqual(["我在呢，你说。", ""], [chunk.text for chunk in ok_stub.text_chunks])
        self.assertEqual(
            ["playback_start", "audio_frame", "done"],
            [message["type"] for message in websocket.messages],
        )

    def test_process_direct_tts_stream_retries_once_on_empty_audio(self):
        empty_stub = _EmptyAudioTTSStub()
        ok_stub = _FakeTTSStub()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-retry-empty-direct")

        with (
            patch.object(gateway, "GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 12.0),
            patch.object(gateway, "get_tts_stub", side_effect=[empty_stub, ok_stub]) as get_tts_stub,
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", 0.5),
        ):
            asyncio.run(
                gateway.process_direct_tts_stream(
                    "我在呢，你说。",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-retry-empty-direct",
                        "round_seq": 1,
                        "round_id": "trace-retry-empty-direct",
                        "playback_id": "trace-retry-empty-direct:playback",
                    },
                    event_type="wake_idle",
                )
            )

        self.assertEqual(2, get_tts_stub.call_count)
        self.assertEqual(12.0, empty_stub.timeout)
        self.assertEqual(12.0, ok_stub.timeout)
        self.assertEqual(
            ["playback_start", "audio_frame", "done"],
            [message["type"] for message in websocket.messages],
        )

    def test_process_direct_tts_stream_stops_after_second_empty_audio(self):
        first_empty = _EmptyAudioTTSStub()
        second_empty = _EmptyAudioTTSStub()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-empty-twice-direct")

        with (
            patch.object(gateway, "GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 12.0),
            patch.object(
                gateway,
                "get_tts_stub",
                side_effect=[first_empty, second_empty],
            ) as get_tts_stub,
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", 0.5),
        ):
            asyncio.run(
                gateway.process_direct_tts_stream(
                    "我在呢，你说。",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-empty-twice-direct",
                        "round_seq": 1,
                        "round_id": "trace-empty-twice-direct",
                        "playback_id": "trace-empty-twice-direct:playback",
                    },
                    event_type="wake_idle",
                )
            )

        self.assertEqual(2, get_tts_stub.call_count)
        self.assertEqual(
            ["playback_start", "error"],
            [message["type"] for message in websocket.messages],
        )
        self.assertEqual("TTS_EMPTY_AUDIO", websocket.messages[-1]["code"])

    def test_process_llm_tts_stream_reports_deadline_error(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _DeadlineTTSStub()
        websocket = _FakeWebSocket()
        manager, session_id = self._round_session("trace-deadline")

        with (
            patch.object(gateway, "GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC", 0.1),
            patch.object(gateway, "GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 0.2),
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "session_manager", manager),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试一下",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-deadline",
                        "round_seq": 1,
                        "round_id": "trace-deadline",
                        "playback_id": "trace-deadline:playback",
                    },
                    history_query="测试一下",
                )
            )

        self.assertEqual(0.2, tts_stub.timeout)
        self.assertEqual("playback_start", websocket.messages[0]["type"])
        self.assertEqual("error", websocket.messages[-1]["type"])
        self.assertEqual("LLM_TTS_FAILED", websocket.messages[-1]["code"])
        self.assertIn("deadline exceeded", websocket.messages[-1]["message"])

    def test_process_llm_tts_stream_still_reports_unexpected_channel_closed(self):
        websocket = _FakeWebSocket()
        manager, session_id = self._round_session("trace-unexpected-channel-closed")

        with (
            patch.object(gateway, "get_llm_stub", return_value=_FakeLLMStub()),
            patch.object(gateway, "get_tts_stub", return_value=_ChannelClosedTTSStub()),
            patch.object(gateway, "session_manager", manager),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试异常断链",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-unexpected-channel-closed",
                        "round_id": "trace-unexpected-channel-closed",
                        "playback_id": "trace-unexpected-channel-closed:playback",
                    },
                )
            )

        self.assertEqual("error", websocket.messages[-1]["type"])
        self.assertEqual("LLM_TTS_FAILED", websocket.messages[-1]["code"])
        self.assertIn("Channel closed!", websocket.messages[-1]["message"])

    def test_process_llm_tts_stream_treats_channel_closed_after_interrupt_as_cancelled(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _InterruptChannelClosedTTSStub()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-interrupt-channel-closed")

        async def run_test():
            task = asyncio.create_task(
                gateway.process_llm_tts_stream(
                    "测试打断",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-interrupt-channel-closed",
                        "round_seq": 1,
                        "round_id": "trace-interrupt-channel-closed",
                        "playback_id": "trace-interrupt-channel-closed:playback",
                    },
                    history_query="测试打断",
                )
            )
            started = await asyncio.to_thread(tts_stub.started.wait, 1.0)
            self.assertTrue(started)
            manager.set_interrupted(session_id, True)
            tts_stub.release.set()
            await task

        with (
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", 1.0),
        ):
            asyncio.run(run_test())

        self.assertFalse(any(message["type"] == "error" for message in websocket.messages))
        self.assertEqual("done", websocket.messages[-1]["type"])
        trace = recorder.get_round("trace-interrupt-channel-closed")["trace"]
        self.assertEqual("llm_tts_interrupted", trace["last_stage"])

    def test_process_llm_tts_stream_skips_empty_final_audio_chunk(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _FakeTTSStubWithEmptyFinal()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-empty-final")

        with (
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试一下",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-empty-final",
                        "round_seq": 1,
                        "round_id": "trace-empty-final",
                        "playback_id": "trace-empty-final:playback",
                    },
                    history_query="测试一下",
                )
            )

        self.assertEqual(["playback_start", "audio_frame", "done"], [message["type"] for message in websocket.messages])
        self.assertEqual("opus", websocket.messages[1]["encoding"])
        self.assertEqual(b"OPUSRAW1\x00\x02OP", websocket.messages[1]["payload"])
        metrics = recorder.get_round("trace-empty-final")["trace"]["metrics"]
        self.assertEqual(1, metrics["audio_chunks"])
        self.assertEqual(1, metrics["tts_empty_audio_chunks"])

    def test_process_llm_tts_stream_plays_selected_singing_asset(self):
        class CountingOpusEncoder(_FakeOpusEncoder):
            instances = 0

            def __init__(self, sample_rate: int, channels: int, application: int):
                super().__init__(sample_rate, channels, application)
                CountingOpusEncoder.instances += 1

        llm_stub = _FakeSingingLLMStub()
        tts_stub = _FakeTTSStub()
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-singing")
        song_audio = SimpleNamespace(
            asset_id="serena-v1:002",
            voice_id="serena-v1",
            song_id="002",
            title="彩虹的微笑",
            duration_seconds=0.2,
            sample_rate=16000,
            channels=1,
            chunks=lambda chunk_ms=100: iter([
                b"\x00\x00" * 1600,
                b"\x00\x00" * 1600,
            ]),
        )

        with (
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "_load_singing_audio", return_value=song_audio) as load_song,
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
            patch.object(
                opus_audio,
                "opuslib",
                SimpleNamespace(Encoder=CountingOpusEncoder),
            ),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "唱一首彩虹的微笑",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-singing",
                        "round_seq": 1,
                        "round_id": "trace-singing",
                        "playback_id": "trace-singing:playback",
                    },
                    history_query="唱一首彩虹的微笑",
                )
            )

        load_song.assert_called_once_with("serena-v1:002")
        message_types = [message["type"] for message in websocket.messages]
        self.assertEqual("playback_start", message_types[0])
        self.assertEqual("done", message_types[-1])
        self.assertNotIn("error", message_types)
        self.assertEqual(3, message_types.count("audio_frame"))
        # One stateless encoder for the preparation TTS and one persistent
        # encoder for both 100ms singing chunks.
        self.assertEqual(2, CountingOpusEncoder.instances)
        trace = recorder.get_round("trace-singing")["trace"]
        self.assertEqual("llm_tts_done", trace["last_stage"])

    def test_process_llm_tts_stream_cancels_round_on_slow_audio_send(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _FakeMultiChunkTTSStub()
        websocket = _SlowFakeWebSocket(delay_seconds=0.02)
        recorder = gateway.TraceRecorder(max_events=50, max_rounds=10)
        manager, session_id = self._round_session("trace-slow-send")

        with (
            patch.object(gateway, "GATEWAY_WS_AUDIO_SLOW_SEND_MS", 1.0),
            patch.object(gateway, "GATEWAY_WS_SEND_TIMEOUT_SEC", 0.0),
            patch.object(gateway, "GATEWAY_WS_SLOW_SEND_MAX_STRIKES", 3),
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试一下",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-slow-send",
                        "round_seq": 1,
                        "round_id": "trace-slow-send",
                        "playback_id": "trace-slow-send:playback",
                    },
                    history_query="测试一下",
                )
            )

        self.assertEqual(["playback_start", "audio_frame"], [message["type"] for message in websocket.messages])
        self.assertFalse(websocket.close_calls)

        metrics = recorder.get_round("trace-slow-send")["trace"]["metrics"]
        self.assertTrue(metrics["ws_backpressure"])
        self.assertTrue(metrics["cancelled"])
        self.assertEqual(1, metrics["ws_slow_send_strikes"])
        self.assertGreaterEqual(metrics["ws_slow_send_ms"], 1.0)

    def test_process_llm_tts_stream_closes_client_after_slow_send_limit(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _FakeMultiChunkTTSStub()
        websocket = _SlowFakeWebSocket(delay_seconds=0.02)
        manager, session_id = self._round_session("trace-close-slow")

        with (
            patch.object(gateway, "GATEWAY_WS_AUDIO_SLOW_SEND_MS", 1.0),
            patch.object(gateway, "GATEWAY_WS_SEND_TIMEOUT_SEC", 0.0),
            patch.object(gateway, "GATEWAY_WS_SLOW_SEND_MAX_STRIKES", 1),
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "session_manager", manager),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试一下",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-close-slow",
                        "round_seq": 1,
                        "round_id": "trace-close-slow",
                        "playback_id": "trace-close-slow:playback",
                    },
                    history_query="测试一下",
                )
            )

        self.assertEqual([(1011, "WebSocket audio send backpressure")], websocket.close_calls)

    def test_process_llm_tts_stream_cancels_tts_on_unexpected_audio_send_error(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _CancellableTTSStub()
        websocket = _FailingAudioWebSocket()
        manager, session_id = self._round_session("trace-send-error")

        with (
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", 0.5),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试一下",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-send-error",
                        "round_seq": 1,
                        "round_id": "trace-send-error",
                        "playback_id": "trace-send-error:playback",
                    },
                    history_query="测试一下",
                )
            )

        self.assertIsNotNone(tts_stub.call)
        self.assertTrue(tts_stub.call.cancelled.is_set())
        self.assertEqual("error", websocket.messages[-1]["type"])
        self.assertEqual("LLM_TTS_FAILED", websocket.messages[-1]["code"])

    def test_process_llm_tts_stream_cancels_grpc_when_round_expires_after_dequeue(self):
        llm_stub = _FakeLLMStub()
        tts_stub = _CancellableTTSStub()
        websocket = _FakeWebSocket()
        manager, session_id = self._round_session("trace-stale-after-dequeue")

        main_thread = threading.current_thread()
        main_round_checks = {"count": 0}
        original_is_current_round = manager.is_current_round

        def is_current_round(session_id_arg, round_id_arg):
            if threading.current_thread() is main_thread:
                main_round_checks["count"] += 1
                if main_round_checks["count"] >= 3:
                    return False
            return original_is_current_round(session_id_arg, round_id_arg)

        async def fail_if_audio_is_sent(*args, **kwargs):
            raise AssertionError("stale round audio must not be sent")

        manager.is_current_round = is_current_round

        with (
            patch.object(gateway, "get_llm_stub", return_value=llm_stub),
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "send_audio_message", fail_if_audio_is_sent),
            patch.object(gateway, "LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", 0.5),
        ):
            asyncio.run(
                gateway.process_llm_tts_stream(
                    "测试一下",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-stale-after-dequeue",
                        "round_seq": 1,
                        "round_id": "trace-stale-after-dequeue",
                        "playback_id": "trace-stale-after-dequeue:playback",
                    },
                    history_query="测试一下",
                )
            )

        self.assertIsNotNone(tts_stub.call)
        self.assertTrue(tts_stub.call.cancelled.is_set())
        self.assertGreaterEqual(main_round_checks["count"], 3)

    def test_process_direct_tts_stream_cancels_tts_on_unexpected_audio_send_error(self):
        tts_stub = _CancellableTTSStub()
        websocket = _FailingAudioWebSocket()
        manager, session_id = self._round_session("trace-direct-send-error")

        with (
            patch.object(gateway, "get_tts_stub", return_value=tts_stub),
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", 0.5),
        ):
            asyncio.run(
                gateway.process_direct_tts_stream(
                    "我在呢。",
                    session_id,
                    websocket,
                    trace={
                        "trace_id": "trace-direct-send-error",
                        "round_seq": 1,
                        "round_id": "trace-direct-send-error",
                        "playback_id": "trace-direct-send-error:playback",
                    },
                    event_type="wake_idle",
                )
            )

        self.assertIsNotNone(tts_stub.call)
        self.assertTrue(tts_stub.call.cancelled.is_set())
        self.assertEqual("error", websocket.messages[-1]["type"])
        self.assertEqual("TTS_FAILED", websocket.messages[-1]["code"])

    def test_cleanup_llm_session_calls_clear_session_with_timeout(self):
        stub = _FakeLLMCleanupStub()

        with (
            patch.object(gateway, "llm_stub", stub),
            patch.object(gateway, "get_llm_stub", return_value=stub),
            patch.object(gateway, "GATEWAY_LLM_SESSION_CLEANUP_TIMEOUT_SEC", 1.5),
        ):
            ok = asyncio.run(gateway.cleanup_llm_session("session-cleanup"))

        self.assertTrue(ok)
        self.assertEqual("session-cleanup", stub.clear_session_id)
        self.assertEqual(1.5, stub.clear_timeout)

    def test_cleanup_llm_session_skips_when_llm_stub_was_never_created(self):
        with (
            patch.object(gateway, "llm_stub", None),
            patch.object(gateway, "get_llm_stub", side_effect=AssertionError("unexpected reconnect")),
        ):
            ok = asyncio.run(gateway.cleanup_llm_session("session-cleanup"))

        self.assertFalse(ok)


class _FakeSTTStub:
    def __init__(self):
        self.request = None
        self.timeout = None

    def RecognizeSpeech(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        return stt_service_pb2.TextResponse(
            text="你好。",
            raw_text="你好。",
            language="zh",
            event_type="speech",
            metadata_json="{}",
            confidence_source="unavailable",
        )


class _DeadlineSTTStub:
    def __init__(self):
        self.timeout = None

    def RecognizeSpeech(self, request, timeout=None):
        self.timeout = timeout
        raise _FakeDeadlineExceeded()


class _FakeLLMStub:
    def __init__(self):
        self.request = None
        self.timeout = None

    def StreamChat(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        return iter([
            llm_service_pb2.ChatResponse(text="你好。"),
            llm_service_pb2.ChatResponse(
                text="",
                is_final=True,
                metrics_json=json.dumps(
                    {
                        "llm_router_kind": "chat",
                        "llm_router_source": "no_tools",
                        "llm_router_ms": 12.5,
                        "llm_mcp_prepare_ms": 5.0,
                        "llm_service_total_ms": 123.4,
                    }
                ),
            ),
        ])


class _FakeSingingLLMStub(_FakeLLMStub):
    def StreamChat(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        return iter([
            llm_service_pb2.ChatResponse(text="好，我来唱一段。"),
            llm_service_pb2.ChatResponse(text="[SINGING_PLAYBACK:serena-v1:002]"),
            llm_service_pb2.ChatResponse(text="", is_final=True),
        ])


class _FakeLLMCleanupStub(_FakeLLMStub):
    def __init__(self):
        super().__init__()
        self.clear_session_id = None
        self.clear_timeout = None

    def ClearSession(self, request, timeout=None):
        self.clear_session_id = request.session_id
        self.clear_timeout = timeout
        return llm_service_pb2.ClearSessionResponse(
            success=True,
            cleared=True,
            message="cleared",
        )


class _FakeTTSStub:
    def __init__(self):
        self.text_chunks = []
        self.timeout = None

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        self.timeout = timeout
        self.text_chunks = list(request_iterator)
        return iter([
            tts_service_pb2.AudioChunk(
                audio_data=b"\x00\x00",
                sample_rate=16000,
                channels=1,
                sample_width=2,
                is_final=True,
            )
        ])


class _EmptyAudioTTSStub:
    def __init__(self):
        self.timeout = None

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        self.timeout = timeout
        list(request_iterator)
        return iter([
            tts_service_pb2.AudioChunk(
                audio_data=b"",
                sample_rate=16000,
                channels=1,
                sample_width=2,
                is_final=True,
            )
        ])


class _FakeMultiChunkTTSStub:
    def __init__(self):
        self.timeout = None

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        self.timeout = timeout
        list(request_iterator)
        return iter([
            tts_service_pb2.AudioChunk(
                audio_data=b"\x00\x00",
                sample_rate=16000,
                channels=1,
                sample_width=2,
                is_final=False,
            ),
            tts_service_pb2.AudioChunk(
                audio_data=b"\x01\x00",
                sample_rate=16000,
                channels=1,
                sample_width=2,
                is_final=True,
            ),
        ])


class _CancellableTTSCall:
    def __init__(self):
        self.cancelled = threading.Event()
        self._sent_first = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent_first:
            self._sent_first = True
            return tts_service_pb2.AudioChunk(
                audio_data=b"\x00\x00",
                sample_rate=16000,
                channels=1,
                sample_width=2,
                is_final=False,
            )
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if self.cancelled.is_set():
                raise StopIteration
            time.sleep(0.01)
        raise RuntimeError("TTS call was not cancelled")

    def cancel(self):
        self.cancelled.set()
        return True


class _CancellableTTSStub:
    def __init__(self):
        self.timeout = None
        self.text_chunks = []
        self.call = None

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        self.timeout = timeout
        self.text_chunks = list(request_iterator)
        self.call = _CancellableTTSCall()
        return self.call


class _FakeTTSStubWithEmptyFinal:
    def __init__(self):
        self.timeout = None

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        self.timeout = timeout
        list(request_iterator)
        return iter([
            tts_service_pb2.AudioChunk(
                audio_data=b"\x00\x00",
                sample_rate=16000,
                channels=1,
                sample_width=2,
                is_final=False,
            ),
            tts_service_pb2.AudioChunk(
                audio_data=b"",
                sample_rate=16000,
                channels=1,
                sample_width=2,
                is_final=True,
            ),
        ])


class _DeadlineTTSStub:
    def __init__(self):
        self.timeout = None

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        self.timeout = timeout
        raise _FakeDeadlineExceeded()


class _ChannelClosedTTSStub:
    def __init__(self):
        self.timeout = None

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        self.timeout = timeout
        list(request_iterator)
        raise _FakeChannelClosed()


class _InterruptChannelClosedTTSStub:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def StreamTextToSpeech(self, request_iterator, timeout=None):
        list(request_iterator)
        self.started.set()
        self.release.wait(timeout=1.0)
        raise _FakeChannelClosed()


class _FakeDeadlineExceeded(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.DEADLINE_EXCEEDED

    def details(self):
        return "deadline exceeded"

    def __str__(self):
        return self.details()


class _FakeChannelClosed(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.CANCELLED

    def details(self):
        return "Channel closed!"

    def __str__(self):
        return self.details()


class _FakeInnerGrpcChannel:
    def __init__(self, state):
        self.state = state

    def check_connectivity_state(self, try_to_connect: bool):
        return self.state


class _FakeGrpcChannel:
    def __init__(self, state):
        self._channel = _FakeInnerGrpcChannel(state)


class _ClosableFakeGrpcChannel(_FakeGrpcChannel):
    def __init__(self, state):
        super().__init__(state)
        self.close_count = 0

    def close(self):
        self.close_count += 1


class _BrokenInnerGrpcChannel:
    def check_connectivity_state(self, try_to_connect: bool):
        raise RuntimeError("boom")


class _BrokenGrpcChannel:
    _channel = _BrokenInnerGrpcChannel()


class _FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)

    async def send_bytes(self, frame):
        header, payload = gateway.decode_audio_frame(frame)
        self.messages.append({**header, "payload": payload})


class _FakeOpusEncoder:
    def __init__(self, sample_rate: int, channels: int, application: int):
        self.sample_rate = sample_rate
        self.channels = channels
        self.application = application

    def encode(self, frame: bytes, frame_size: int) -> bytes:
        if not frame or frame_size != 320:
            raise ValueError("unexpected fake opus frame")
        return b"OP"


class _SlowFakeWebSocket(_FakeWebSocket):
    def __init__(self, delay_seconds: float):
        super().__init__()
        self.delay_seconds = delay_seconds
        self.close_calls = []

    async def send_json(self, message):
        await asyncio.sleep(self.delay_seconds)
        await super().send_json(message)

    async def send_bytes(self, frame):
        await asyncio.sleep(self.delay_seconds)
        await super().send_bytes(frame)

    async def close(self, code=None, reason=None):
        self.close_calls.append((code, reason))


class _FailingAudioWebSocket(_FakeWebSocket):
    async def send_bytes(self, frame):
        raise RuntimeError("audio send failed")


if __name__ == "__main__":
    unittest.main()
