import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["CONFIG_DATABASE_URL"] = ""

from gateway import gateway_server as gateway
from gateway.opus_audio import build_opus_packet_stream
from gateway.request_queue import (
    REQUEST_QUEUE_METRICS_KEY,
    attach_request_queue_metrics,
    build_request_dequeued_summary,
    enqueue_latest_request,
    pop_request_queue_metrics,
)
from gateway.playback_report import (
    float_from_client_report,
    int_from_client_report,
    request_playback_id,
    request_round_id,
    request_trace_id,
    round_seq_from_trace_id,
)


class GatewayRequestQueueTest(unittest.TestCase):
    def test_enqueue_latest_user_request_replaces_pending_and_interrupts_active_round(self):
        request_queue = asyncio.Queue(maxsize=1)
        request_queue.put_nowait({"type": "audio", "id": "old"})
        fake_session_manager = _FakeSessionManager()

        with patch.object(gateway, "session_manager", fake_session_manager):
            dropped = gateway._enqueue_latest_user_request(
                request_queue,
                {"type": "text", "id": "new"},
                session_id="session-1",
                reason="test replace",
            )

        self.assertEqual(1, dropped)
        self.assertEqual(1, request_queue.qsize())
        self.assertEqual({"type": "text", "id": "new"}, request_queue.get_nowait())
        self.assertEqual([("session-1", True)], fake_session_manager.interrupt_calls)

    def test_turn_candidate_shadow_runs_asr_without_enqueuing_user_request(self):
        websocket = _FakeWebSocket()
        manager = _AudioStreamSessionManager()
        decoded = gateway.DecodedAudioRequest(
            audio_data=b"wav",
            audio_metadata={"duration_ms": 420.0},
            audio_transport="binary_frame",
            audio_encoding="opus",
        )

        async def fake_process_asr(audio_data, session_id):
            self.assertEqual(b"wav", audio_data)
            self.assertEqual("session-1", session_id)
            return "我还没说完", 123.0, {"language": "zh"}

        data = {
            "type": "turn_candidate",
            "audio_bytes": b"OPUSRAW1\x00\x01a",
            "audio_encoding": "opus",
            "trace_id": "trace-1",
            "utterance_id": "utt-1",
            "candidate_seq": 1,
            "speech_epoch": 0,
            "audio_watermark": 16000,
            "silence_ms": 300,
            "shadow": True,
        }
        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "TURN_GATE_SHADOW_ENABLED", True),
            patch.object(gateway, "_decode_audio_request_payload", return_value=decoded),
            patch.object(gateway, "process_asr", side_effect=fake_process_asr),
        ):
            handled = asyncio.run(
                gateway._handle_turn_candidate_shadow_message(
                    websocket,
                    session_id="session-1",
                    data=data,
                    msg_type="turn_candidate",
                )
            )

        self.assertTrue(handled)
        self.assertEqual(["turn_candidate_result", "done"], [item["type"] for item in websocket.messages])
        result = websocket.messages[0]
        self.assertEqual("observed", result["status"])
        self.assertEqual("我还没说完", result["asr_text"])
        self.assertEqual("disabled", result["smart_turn"]["status"])
        self.assertEqual("disabled", result["livekit_eou"]["status"])
        self.assertEqual("unavailable", result["policy_preview"])
        self.assertFalse(result["committed"])
        self.assertEqual([], getattr(manager, "interrupt_calls", []))

    def test_turn_candidate_shadow_records_smart_eou_agreement_without_commit(self):
        websocket = _FakeWebSocket()
        manager = _TurnGateSessionManager()
        decoded = gateway.DecodedAudioRequest(
            audio_data=b"wav",
            audio_metadata={"duration_ms": 420.0},
            audio_transport="binary_frame",
            audio_encoding="opus",
        )

        async def fake_process_asr(_audio_data, _session_id):
            return "好的，就这样吧", 100.0, {"language": "zh"}

        data = {
            "type": "turn_candidate",
            "audio_bytes": b"OPUSRAW1\x00\x01a",
            "audio_encoding": "opus",
            "trace_id": "trace-1",
            "utterance_id": "utt-1",
            "candidate_seq": 1,
            "speech_epoch": 0,
            "audio_watermark": 16000,
            "silence_ms": 300,
            "shadow": True,
            "context_session_id": "python-main-session",
        }
        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "TURN_GATE_SHADOW_ENABLED", True),
            patch.object(gateway, "TURN_GATE_MODELS_ENABLED", True),
            patch.object(gateway, "_decode_audio_request_payload", return_value=decoded),
            patch.object(gateway, "process_asr", side_effect=fake_process_asr),
            patch.object(
                gateway,
                "_run_smart_turn_shadow",
                return_value={"status": "ok", "probability": 0.8, "end": True},
            ),
            patch.object(
                gateway,
                "_run_livekit_eou_shadow",
                return_value={"status": "ok", "probability": 0.02, "end": True},
            ) as eou,
        ):
            handled = asyncio.run(
                gateway._handle_turn_candidate_shadow_message(
                    websocket,
                    session_id="candidate-session",
                    data=data,
                    msg_type="turn_candidate",
                )
            )

        self.assertTrue(handled)
        result = websocket.messages[0]
        self.assertEqual("both_end", result["agreement"])
        self.assertEqual("early_commit", result["policy_preview"])
        self.assertEqual("primary_python_session", result["context_source"])
        self.assertEqual(2, result["context_turns"])
        self.assertFalse(result["committed"])
        eou.assert_called_once_with(manager.history, "好的，就这样吧")

    def test_turn_candidate_active_requires_explicit_opt_in_and_returns_non_shadow_result(self):
        websocket = _FakeWebSocket()
        manager = _AudioStreamSessionManager()
        decoded = gateway.DecodedAudioRequest(
            audio_data=b"wav",
            audio_metadata={"duration_ms": 420.0},
            audio_transport="binary_frame",
            audio_encoding="opus",
        )

        async def fake_process_asr(_audio_data, _session_id):
            return "好的，就这样吧", 80.0, {"language": "zh"}

        data = {
            "type": "turn_candidate",
            "audio_bytes": b"OPUSRAW1\x00\x01a",
            "audio_encoding": "opus",
            "trace_id": "trace-active",
            "utterance_id": "utt-active",
            "candidate_seq": 1,
            "speech_epoch": 0,
            "audio_watermark": 16000,
            "silence_ms": 300,
            "shadow": False,
        }
        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "TURN_GATE_SHADOW_ENABLED", False),
            patch.object(gateway, "TURN_GATE_ACTIVE_ENABLED", True),
            patch.object(gateway, "_decode_audio_request_payload", return_value=decoded),
            patch.object(gateway, "process_asr", side_effect=fake_process_asr),
        ):
            handled = asyncio.run(
                gateway._handle_turn_candidate_shadow_message(
                    websocket,
                    session_id="candidate-session",
                    data=data,
                    msg_type="turn_candidate",
                )
            )

        self.assertTrue(handled)
        self.assertEqual(["turn_candidate_result", "done"], [item["type"] for item in websocket.messages])
        self.assertFalse(websocket.messages[0]["shadow"])
        self.assertFalse(websocket.messages[0]["committed"])

    def test_enqueue_latest_user_request_can_preserve_active_round(self):
        request_queue = asyncio.Queue(maxsize=1)
        fake_session_manager = _FakeSessionManager()

        with patch.object(gateway, "session_manager", fake_session_manager):
            dropped = gateway._enqueue_latest_user_request(
                request_queue,
                {"type": "audio", "id": "new"},
                session_id="session-1",
                reason="test no interrupt",
                cancel_active_round=False,
            )

        self.assertEqual(0, dropped)
        self.assertEqual({"type": "audio", "id": "new"}, request_queue.get_nowait())
        self.assertEqual([], fake_session_manager.interrupt_calls)

    def test_drain_request_queue_returns_dropped_count(self):
        request_queue = asyncio.Queue(maxsize=3)
        request_queue.put_nowait({"id": 1})
        request_queue.put_nowait({"id": 2})

        dropped = gateway._drain_request_queue(request_queue)

        self.assertEqual(2, dropped)
        self.assertTrue(request_queue.empty())

    def test_enqueue_latest_request_replaces_pending_without_session_side_effects(self):
        request_queue = asyncio.Queue(maxsize=1)
        request_queue.put_nowait({"type": "audio", "id": "old"})

        dropped = enqueue_latest_request(request_queue, {"type": "audio", "id": "new"})

        self.assertEqual(1, dropped)
        self.assertEqual({"type": "audio", "id": "new"}, request_queue.get_nowait())

    def test_request_queue_metrics_round_trip(self):
        request_queue = asyncio.Queue(maxsize=2)
        request_queue.put_nowait({"id": "pending"})
        data = {"type": "audio"}

        attach_request_queue_metrics(data, request_queue, dropped=3)
        metrics = data[REQUEST_QUEUE_METRICS_KEY]
        self.assertEqual(1, metrics["queue_size"])
        self.assertEqual(2, metrics["queue_maxsize"])
        self.assertEqual(3, metrics["dropped_pending"])

        request_queue.get_nowait()
        popped = pop_request_queue_metrics(data, request_queue)

        self.assertNotIn(REQUEST_QUEUE_METRICS_KEY, data)
        self.assertEqual(1, popped["queue_size"])
        self.assertEqual(2, popped["queue_maxsize"])
        self.assertEqual(3, popped["dropped_pending"])
        self.assertGreaterEqual(popped["queue_wait_ms"], 0.0)
        self.assertEqual(0, popped["queue_size_after_dequeue"])

    def test_build_request_dequeued_summary_merges_queue_metrics(self):
        self.assertEqual(
            {
                "message_type": "audio",
                "round_id": "round-1",
                "playback_id": "round-1:playback",
                "queue_size": 1,
                "queue_wait_ms": 12.5,
            },
            build_request_dequeued_summary(
                {"type": "audio"},
                round_id="round-1",
                playback_id="round-1:playback",
                queue_metrics={"queue_size": 1, "queue_wait_ms": 12.5},
            ),
        )

    def test_session_round_lifecycle_tracks_current_playback(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()

        self.assertTrue(
            manager.start_round(
                session_id,
                round_id="round-1",
                playback_id="playback-1",
            )
        )
        self.assertTrue(manager.is_current_round(session_id, "round-1"))

        cancelled = manager.cancel_current_round(session_id)
        self.assertEqual(
            {"round_id": "round-1", "playback_id": "playback-1"},
            cancelled,
        )
        self.assertTrue(manager.is_interrupted(session_id))

        self.assertTrue(manager.complete_round(session_id, "round-1"))
        self.assertFalse(manager.is_current_round(session_id, "round-1"))
        self.assertIsNone(manager.get_current_round(session_id))

    def test_cancel_round_if_matches_rejects_stale_or_incomplete_report(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        manager.start_round(
            session_id,
            round_id="round-new",
            playback_id="playback-new",
        )

        self.assertFalse(
            manager.cancel_round_if_matches(
                session_id,
                round_id="round-old",
                playback_id="playback-old",
            )
        )
        self.assertFalse(
            manager.cancel_round_if_matches(
                session_id,
                round_id="round-new",
                playback_id="",
            )
        )
        self.assertFalse(manager.is_interrupted(session_id))
        self.assertTrue(manager.is_current_round(session_id, "round-new"))

        self.assertTrue(
            manager.cancel_round_if_matches(
                session_id,
                round_id="round-new",
                playback_id="playback-new",
            )
        )
        self.assertTrue(manager.is_interrupted(session_id))

    def test_send_playback_cancel_uses_round_metadata(self):
        websocket = _FakeWebSocket()

        asyncio.run(
            gateway._send_playback_cancel(
                websocket,
                session_id="session-1",
                cancelled_round={
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                },
                reason="new_audio",
            )
        )

        self.assertEqual(
            [
                {
                    "type": "playback_cancel",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                    "reason": "new_audio",
                }
            ],
            websocket.messages,
        )

    def test_queue_new_user_input_enqueues_metrics_and_sends_cancel(self):
        request_queue = asyncio.Queue(maxsize=1)
        data = {"type": "audio"}
        websocket = _FakeWebSocket()
        fake_session_manager = _RoundCancellingSessionManager()

        with patch.object(gateway, "session_manager", fake_session_manager):
            dropped = asyncio.run(
                gateway._queue_new_user_input(
                    websocket,
                    request_queue,
                    data,
                    session_id="session-1",
                    queue_reason="test queue",
                    playback_cancel_reason="new_audio",
                )
            )

        self.assertEqual(0, dropped)
        self.assertIs(request_queue.get_nowait(), data)
        self.assertEqual(1, data[REQUEST_QUEUE_METRICS_KEY]["queue_size"])
        self.assertEqual(0, data[REQUEST_QUEUE_METRICS_KEY]["dropped_pending"])
        self.assertEqual(
            [
                {
                    "type": "playback_cancel",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                    "reason": "new_audio",
                }
            ],
            websocket.messages,
        )
        self.assertEqual(["session-1"], fake_session_manager.cancel_calls)

    def test_send_rtc_session_mismatch_if_needed_sends_error_only_on_mismatch(self):
        websocket = _FakeWebSocket()

        matched = asyncio.run(
            gateway._send_rtc_session_mismatch_if_needed(
                websocket,
                {"session_id": "session-1"},
                session_id="session-1",
                message_type="rtc_offer",
            )
        )
        mismatched = asyncio.run(
            gateway._send_rtc_session_mismatch_if_needed(
                websocket,
                {"session_id": "other"},
                session_id="session-1",
                message_type="rtc_offer",
            )
        )

        self.assertFalse(matched)
        self.assertTrue(mismatched)
        self.assertEqual(
            [
                {
                    "type": "error",
                    "code": "RTC_SESSION_MISMATCH",
                    "message": "rtc_offer session_id 与当前会话不匹配",
                }
            ],
            websocket.messages,
        )

    def test_handle_register_message_sends_registered_payload_and_rtc_config(self):
        websocket = _FakeWebSocket()
        fake_session_manager = _TouchSessionManager()
        rtc_config_calls = []

        async def fake_register(session_id, payload):
            self.assertEqual("session-1", session_id)
            self.assertEqual({"type": "register", "robot_id": "test_01"}, payload)
            return {
                "robot_id": "test_01",
                "bot_id": "xiaowen",
                "bot_name": "小文",
                "client_type": "rust",
                "is_new_robot": False,
            }

        async def fake_send_rtc_config(websocket, session_id):
            rtc_config_calls.append(session_id)
            return True

        with (
            patch.object(gateway, "session_manager", fake_session_manager),
            patch.object(gateway, "register_robot_session", fake_register),
            patch.object(gateway, "send_rtc_config_if_enabled", fake_send_rtc_config),
        ):
            handled = asyncio.run(
                gateway._handle_register_message(
                    websocket,
                    session_id="session-1",
                    data={"type": "register", "robot_id": "test_01"},
                    msg_type="register",
                )
            )

        self.assertTrue(handled)
        self.assertEqual(["session-1"], fake_session_manager.touch_calls)
        self.assertEqual(["session-1"], rtc_config_calls)
        self.assertEqual(
            [
                {
                    "type": "registered",
                    "session_id": "session-1",
                    "robot_id": "test_01",
                    "bot_id": "xiaowen",
                    "bot_name": "小文",
                    "client_type": "rust",
                    "is_new_robot": False,
                }
            ],
            websocket.messages,
        )

    def test_handle_register_message_reports_failures_and_ignores_unknown_type(self):
        websocket = _FakeWebSocket()
        fake_session_manager = _TouchSessionManager()

        async def fake_register(session_id, payload):
            raise ValueError("robot_secret 校验失败")

        with (
            patch.object(gateway, "session_manager", fake_session_manager),
            patch.object(gateway, "register_robot_session", fake_register),
        ):
            handled_register = asyncio.run(
                gateway._handle_register_message(
                    websocket,
                    session_id="session-1",
                    data={"type": "register"},
                    msg_type="register",
                )
            )
            handled_unknown = asyncio.run(
                gateway._handle_register_message(
                    websocket,
                    session_id="session-1",
                    data={"type": "other"},
                    msg_type="other",
                )
            )

        self.assertTrue(handled_register)
        self.assertFalse(handled_unknown)
        self.assertEqual(["session-1"], fake_session_manager.touch_calls)
        self.assertEqual(
            [
                {
                    "type": "error",
                    "code": "REGISTER_FAILED",
                    "message": "robot_secret 校验失败",
                }
            ],
            websocket.messages,
        )

    def test_handle_rtc_signaling_message_sends_offer_ack_and_mismatch_error(self):
        websocket = _FakeWebSocket()
        fake_session_manager = _TouchSessionManager()

        with (
            patch.object(gateway, "session_manager", fake_session_manager),
            patch.object(gateway, "GATEWAY_RTC_SIGNALING_ENABLED", False),
        ):
            handled_offer = asyncio.run(
                gateway._handle_rtc_signaling_message(
                    websocket,
                    session_id="session-1",
                    data={"type": "rtc_offer", "session_id": "session-1", "sdp": "abc"},
                    msg_type="rtc_offer",
                )
            )
            handled_mismatch = asyncio.run(
                gateway._handle_rtc_signaling_message(
                    websocket,
                    session_id="session-1",
                    data={"type": "transport_ready", "session_id": "other"},
                    msg_type="transport_ready",
                )
            )
            handled_unknown = asyncio.run(
                gateway._handle_rtc_signaling_message(
                    websocket,
                    session_id="session-1",
                    data={"type": "other"},
                    msg_type="other",
                )
            )

        self.assertTrue(handled_offer)
        self.assertTrue(handled_mismatch)
        self.assertFalse(handled_unknown)
        self.assertEqual(["session-1", "session-1"], fake_session_manager.touch_calls)
        self.assertEqual(
            [
                {
                    "type": "transport_fallback_ack",
                    "session_id": "session-1",
                    "active_transport": "websocket",
                    "reason": "rtc_signaling_disabled",
                },
                {
                    "type": "error",
                    "code": "RTC_SESSION_MISMATCH",
                    "message": "transport_ready session_id 与当前会话不匹配",
                },
            ],
            websocket.messages,
        )

    def test_handle_keepalive_message_touches_session_and_replies(self):
        websocket = _FakeWebSocket()
        fake_session_manager = _TouchSessionManager()

        with patch.object(gateway, "session_manager", fake_session_manager):
            handled_ping = asyncio.run(
                gateway._handle_keepalive_message(websocket, "session-1", "ping")
            )
            handled_heartbeat = asyncio.run(
                gateway._handle_keepalive_message(websocket, "session-1", "heartbeat")
            )
            handled_unknown = asyncio.run(
                gateway._handle_keepalive_message(websocket, "session-1", "other")
            )

        self.assertTrue(handled_ping)
        self.assertTrue(handled_heartbeat)
        self.assertFalse(handled_unknown)
        self.assertEqual(["session-1", "session-1"], fake_session_manager.touch_calls)
        self.assertEqual(
            [{"type": "pong"}, {"type": "heartbeat_ack"}],
            websocket.messages,
        )

    def test_handle_playback_report_message_records_report(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        round_id = f"{session_id}:4"
        playback_id = f"{round_id}:playback"
        manager.start_round(
            session_id,
            round_id=round_id,
            playback_id=playback_id,
        )

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
        ):
            handled = gateway._handle_playback_report_message(
                session_id,
                {
                    "type": "playback_interrupted",
                    "round_id": round_id,
                    "playback_id": playback_id,
                    "reason": "wake_interrupt",
                },
                "playback_interrupted",
            )

        self.assertTrue(handled)
        trace = recorder.get_round(round_id)["trace"]
        self.assertEqual("client_playback_interrupted", trace["last_stage"])
        self.assertTrue(trace["metrics"]["cancelled"])
        self.assertTrue(manager.is_interrupted(session_id))

    def test_stale_playback_interrupted_report_does_not_cancel_replacement_round(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        manager.start_round(
            session_id,
            round_id="round-new",
            playback_id="playback-new",
        )

        with patch.object(gateway, "session_manager", manager):
            handled = gateway._handle_playback_report_message(
                session_id,
                {
                    "type": "playback_interrupted",
                    "round_id": "round-old",
                    "playback_id": "playback-old",
                    "reason": "wake_interrupt",
                },
                "playback_interrupted",
            )

        self.assertTrue(handled)
        self.assertFalse(manager.is_interrupted(session_id))
        self.assertTrue(manager.is_current_round(session_id, "round-new"))

    def test_playback_complete_report_does_not_interrupt_current_round(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        manager.start_round(
            session_id,
            round_id="round-current",
            playback_id="playback-current",
        )

        with patch.object(gateway, "session_manager", manager):
            handled = gateway._handle_playback_report_message(
                session_id,
                {
                    "type": "playback_complete",
                    "round_id": "round-current",
                    "playback_id": "playback-current",
                },
                "playback_complete",
            )

        self.assertTrue(handled)
        self.assertFalse(manager.is_interrupted(session_id))
        self.assertTrue(manager.is_current_round(session_id, "round-current"))

    def test_handle_interrupt_message_cancels_current_round_and_updates_cooldown(self):
        audio_streams = {"u1": object()}
        websocket = _FakeWebSocket()
        fake_session_manager = _QueuedInputSessionManager()

        with (
            patch.object(gateway, "session_manager", fake_session_manager),
            patch.object(gateway, "get_gateway_settings", return_value={"interrupt_enabled": True}),
            patch.object(gateway.time, "time", return_value=100.0),
        ):
            handled, updated_time = asyncio.run(
                gateway._handle_interrupt_message(
                    websocket,
                    audio_streams,
                    session_id="session-1",
                    msg_type="interrupt",
                    last_interrupt_time=98.0,
                )
            )

        self.assertTrue(handled)
        self.assertEqual(100.0, updated_time)
        self.assertEqual({}, audio_streams)
        self.assertEqual(["session-1"], fake_session_manager.cancel_calls)
        self.assertEqual(
            [
                {
                    "type": "playback_cancel",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                    "reason": "client_interrupt",
                }
            ],
            websocket.messages,
        )

    def test_handle_interrupt_message_respects_disabled_and_cooldown_states(self):
        disabled_streams = {"u1": object()}
        disabled_manager = _QueuedInputSessionManager()

        with (
            patch.object(gateway, "session_manager", disabled_manager),
            patch.object(gateway, "get_gateway_settings", return_value={"interrupt_enabled": False}),
        ):
            disabled_handled, disabled_time = asyncio.run(
                gateway._handle_interrupt_message(
                    _FakeWebSocket(),
                    disabled_streams,
                    session_id="session-1",
                    msg_type="interrupt",
                    last_interrupt_time=42.0,
                )
            )

        self.assertTrue(disabled_handled)
        self.assertEqual(42.0, disabled_time)
        self.assertEqual({}, disabled_streams)
        self.assertEqual([], disabled_manager.cancel_calls)

        cooldown_streams = {"u1": object()}
        cooldown_manager = _QueuedInputSessionManager()
        cooldown_websocket = _FakeWebSocket()

        with (
            patch.object(gateway, "session_manager", cooldown_manager),
            patch.object(gateway, "get_gateway_settings", return_value={"interrupt_enabled": True}),
            patch.object(gateway.time, "time", return_value=42.5),
        ):
            cooldown_handled, cooldown_time = asyncio.run(
                gateway._handle_interrupt_message(
                    cooldown_websocket,
                    cooldown_streams,
                    session_id="session-1",
                    msg_type="interrupt",
                    last_interrupt_time=42.0,
                )
            )

        self.assertTrue(cooldown_handled)
        self.assertEqual(42.0, cooldown_time)
        self.assertEqual({}, cooldown_streams)
        self.assertEqual([], cooldown_manager.cancel_calls)
        self.assertEqual([], cooldown_websocket.messages)

    def test_handle_interrupt_message_ignores_unknown_type(self):
        audio_streams = {"u1": object()}

        handled, updated_time = asyncio.run(
            gateway._handle_interrupt_message(
                _FakeWebSocket(),
                audio_streams,
                session_id="session-1",
                msg_type="other",
                last_interrupt_time=42.0,
            )
        )

        self.assertFalse(handled)
        self.assertEqual(42.0, updated_time)
        self.assertEqual(["u1"], list(audio_streams))

    def test_process_queued_request_dispatches_by_message_type(self):
        calls = []

        async def fake_handle_text(websocket, session_id, data, **kwargs):
            calls.append(("text", websocket, session_id, data, kwargs))

        async def fake_handle_client_event(websocket, session_id, data, **kwargs):
            calls.append(("client_event", websocket, session_id, data, kwargs))

        async def fake_handle_audio(websocket, session_id, data, **kwargs):
            calls.append(("audio", websocket, session_id, data, kwargs))

        websocket = _FakeWebSocket()
        common_kwargs = {
            "trace_id": "trace-1",
            "round_id": "round-1",
            "playback_id": "playback-1",
            "round_seq": 3,
        }

        with (
            patch.object(gateway, "handle_text", fake_handle_text),
            patch.object(gateway, "handle_client_event", fake_handle_client_event),
            patch.object(gateway, "handle_audio", fake_handle_audio),
        ):
            asyncio.run(
                gateway._process_queued_request(
                    websocket,
                    session_id="session-1",
                    data={"type": "text"},
                    **common_kwargs,
                )
            )
            asyncio.run(
                gateway._process_queued_request(
                    websocket,
                    session_id="session-1",
                    data={"type": "client_event"},
                    **common_kwargs,
                )
            )
            asyncio.run(
                gateway._process_queued_request(
                    websocket,
                    session_id="session-1",
                    data={"type": "audio"},
                    **common_kwargs,
                )
            )

        self.assertEqual(["text", "client_event", "audio"], [call[0] for call in calls])
        for _, called_websocket, called_session_id, _, kwargs in calls:
            self.assertIs(websocket, called_websocket)
            self.assertEqual("session-1", called_session_id)
            self.assertEqual(common_kwargs, kwargs)

    def test_start_queued_request_round_updates_request_and_emits_trace(self):
        request_queue = asyncio.Queue(maxsize=2)
        data = {"type": "audio"}
        attach_request_queue_metrics(data, request_queue, dropped=1)
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        manager.register_robot(
            session_id,
            robot_id="test_01",
            bot_id="xiaowen",
            bot_name="小文",
            client_type="rust",
            is_new_robot=False,
        )
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
        ):
            turn_ids = gateway._start_queued_request_round(
                request_queue,
                data,
                session_id=session_id,
                round_seq=4,
            )

        self.assertEqual(turn_ids["trace_id"], data["trace_id"])
        self.assertEqual(turn_ids["round_id"], data["round_id"])
        self.assertEqual(turn_ids["playback_id"], data["playback_id"])
        self.assertTrue(manager.is_current_round(session_id, turn_ids["round_id"]))

        trace = recorder.get_round(turn_ids["trace_id"])["trace"]
        self.assertEqual("request_dequeued", trace["last_stage"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        summary = trace["events"][0]["summary"]
        self.assertEqual("audio", summary["message_type"])
        self.assertEqual(turn_ids["round_id"], summary["round_id"])
        self.assertEqual(turn_ids["playback_id"], summary["playback_id"])
        self.assertEqual(1, summary["dropped_pending"])
        self.assertIn("queue_wait_ms", summary)

    def test_send_register_required_if_unregistered_emits_error_and_trace(self):
        websocket = _FakeWebSocket()
        manager = _RegisteredStateSessionManager(registered=False)
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
        ):
            handled = asyncio.run(
                gateway._send_register_required_if_unregistered(
                    websocket,
                    session_id="session-1",
                    trace_id="trace-1",
                    round_seq=2,
                )
            )

        self.assertTrue(handled)
        self.assertEqual(["session-1"], manager.is_registered_calls)
        self.assertEqual(
            [
                {
                    "type": "error",
                    "code": "REGISTER_REQUIRED",
                    "message": "请先完成 Robot 注册认证",
                }
            ],
            websocket.messages,
        )
        trace = recorder.get_round("trace-1")["trace"]
        self.assertEqual("register_required", trace["last_stage"])
        self.assertEqual("error", trace["status"])

    def test_send_register_required_if_unregistered_allows_registered_session(self):
        websocket = _FakeWebSocket()
        manager = _RegisteredStateSessionManager(registered=True)
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
        ):
            handled = asyncio.run(
                gateway._send_register_required_if_unregistered(
                    websocket,
                    session_id="session-1",
                    trace_id="trace-1",
                    round_seq=2,
                )
            )

        self.assertFalse(handled)
        self.assertEqual(["session-1"], manager.is_registered_calls)
        self.assertEqual([], websocket.messages)
        self.assertFalse(recorder.get_round("trace-1")["success"])

    def test_resolve_turn_runtime_context_updates_trace_and_loads_tts_settings(self):
        manager = _RuntimeContextSessionManager(robot_id="test_01")
        runtime_state = _FakeGatewayRuntimeState()
        trace = {}

        async def fake_resolve_request_bot(session_id, payload):
            self.assertEqual("session-1", session_id)
            self.assertEqual({"type": "audio"}, payload)
            return "xiaowen", "小文"

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "gateway_runtime_state", runtime_state),
            patch.object(gateway, "resolve_request_bot", fake_resolve_request_bot),
        ):
            context = asyncio.run(
                gateway._resolve_turn_runtime_context(
                    "session-1",
                    {"type": "audio"},
                    trace,
                )
            )

        expected = {
            "robot_id": "test_01",
            "bot_id": "xiaowen",
            "bot_name": "小文",
            "bot_tts_settings": {"tts_profile_id": "default_tts_profile"},
        }
        self.assertEqual(expected, context)
        self.assertEqual(
            {"robot_id": "test_01", "bot_id": "xiaowen", "bot_name": "小文"},
            trace,
        )
        self.assertEqual(["session-1"], manager.get_robot_id_calls)
        self.assertEqual(["xiaowen"], runtime_state.tts_settings_calls)

    def test_extract_direct_text_content_strips_text_and_reports_validation_errors(self):
        content, error = gateway._extract_direct_text_content({"content": "  你好  "})
        self.assertEqual("你好", content)
        self.assertIsNone(error)

        content, error = gateway._extract_direct_text_content({"content": "   "})
        self.assertIsNone(content)
        self.assertEqual("缺少文本内容", error)

        content, error = gateway._extract_direct_text_content(
            {"content": "x" * (gateway.DIRECT_TEXT_MAX_CHARS + 1)}
        )
        self.assertIsNone(content)
        self.assertEqual(f"文本内容过长，最大 {gateway.DIRECT_TEXT_MAX_CHARS} 字符", error)

    def test_emit_process_error_trace_records_context(self):
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with patch.object(gateway, "trace_recorder", recorder):
            gateway._emit_process_error_trace(
                trace_id="trace-1",
                session_id="session-1",
                round_seq=3,
                trace={
                    "robot_id": "test_01",
                    "bot_id": "xiaowen",
                    "bot_name": "小文",
                },
                error=RuntimeError("boom"),
            )

        trace = recorder.get_round("trace-1")["trace"]
        self.assertEqual("process_error", trace["last_stage"])
        self.assertEqual("error", trace["status"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual("boom", trace["events"][0]["error"])

    def test_record_audio_received_trace_touches_session_and_records_context(self):
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        manager = _TouchSessionManager()

        with (
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "session_manager", manager),
        ):
            gateway._record_audio_received_trace(
                trace_id="trace-1",
                session_id="session-1",
                round_seq=3,
                trace={
                    "robot_id": "test_01",
                    "bot_id": "xiaowen",
                    "bot_name": "小文",
                },
            )

        self.assertEqual(["session-1"], manager.touch_calls)
        trace = recorder.get_round("trace-1")["trace"]
        self.assertEqual("audio_received", trace["last_stage"])
        self.assertEqual(3, trace["round_seq"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])

    def test_warn_if_audio_truncated_only_logs_when_truncated(self):
        logger = _FakeLogger()

        with patch.object(gateway, "logger", logger):
            gateway._warn_if_audio_truncated("session-1", {"duration_ms": 100.0})
            gateway._warn_if_audio_truncated(
                "session-1",
                {
                    "truncated": True,
                    "original_duration_ms": 12000.0,
                    "max_duration_ms": 10000.0,
                    "duration_ms": 10000.0,
                },
            )

        self.assertEqual(
            [
                (
                    "会话 %s: 音频 %.0fms 超过限制 %.0fms，已截断为 %.0fms 后继续处理",
                    ("session-1", 12000.0, 10000.0, 10000.0),
                )
            ],
            logger.warning_messages,
        )

    def test_send_audio_rejected_error_emits_trace_and_error_message(self):
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        validation_error = gateway.AudioValidationError(
            "INVALID_AUDIO_TRANSPORT",
            "客户端 audio 消息必须使用 Opus 二进制帧",
            details={"audio_encoding": "pcm"},
        )

        with patch.object(gateway, "trace_recorder", recorder):
            asyncio.run(
                gateway._send_audio_rejected_error(
                    websocket,
                    trace_id="trace-1",
                    session_id="session-1",
                    round_seq=3,
                    trace={
                        "robot_id": "test_01",
                        "bot_id": "xiaowen",
                        "bot_name": "小文",
                    },
                    error=validation_error,
                )
            )

        self.assertEqual(
            [
                {
                    "type": "error",
                    "code": "INVALID_AUDIO_TRANSPORT",
                    "message": "客户端 audio 消息必须使用 Opus 二进制帧",
                }
            ],
            websocket.messages,
        )
        trace = recorder.get_round("trace-1")["trace"]
        self.assertEqual("audio_rejected", trace["last_stage"])
        self.assertEqual("error", trace["status"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual("客户端 audio 消息必须使用 Opus 二进制帧", trace["events"][0]["error"])
        self.assertEqual({"audio_encoding": "pcm"}, trace["events"][0]["summary"])

    def test_record_audio_decoded_trace_and_logs_records_metadata_summary(self):
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with patch.object(gateway, "trace_recorder", recorder):
            gateway._record_audio_decoded_trace_and_logs(
                trace_id="trace-1",
                session_id="session-1",
                round_seq=3,
                round_id="round-1",
                playback_id="playback-1",
                robot_id="test_01",
                bot_id="xiaowen",
                bot_name="小文",
                audio_data=b"abc",
                audio_metadata={
                    "utterance_id": "utt-1",
                    "duration_ms": 120.0,
                    "opus_packets": 6,
                    "stream_chunk_count": 3,
                    "stream_elapsed_ms": 456.7,
                },
                audio_transport="binary_stream",
                audio_encoding="opus",
            )

        trace = recorder.get_round("trace-1")["trace"]
        event = trace["events"][0]
        summary = event["summary"]
        self.assertEqual("audio_decoded", trace["last_stage"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual("round-1", summary["round_id"])
        self.assertEqual("playback-1", summary["playback_id"])
        self.assertEqual("utt-1", summary["utterance_id"])
        self.assertEqual(3, summary["audio_bytes"])
        self.assertEqual(120.0, summary["audio_duration_ms"])
        self.assertEqual("binary_stream", summary["audio_transport"])
        self.assertEqual("opus", summary["audio_encoding"])
        self.assertEqual(6, summary["opus_packets"])
        self.assertEqual(3, summary["stream_chunk_count"])

    def test_build_asr_trace_summary_for_decoded_audio_maps_audio_fields(self):
        decoded = gateway.DecodedAudioRequest(
            audio_data=b"abc",
            audio_metadata={
                "utterance_id": "utt-1",
                "duration_ms": 120.0,
                "opus_packets": 6,
                "stream_chunk_count": 3,
                "stream_elapsed_ms": 456.7,
            },
            audio_transport="binary_stream",
            audio_encoding="opus",
        )

        summary = gateway._build_asr_trace_summary_for_decoded_audio(
            decoded,
            trace_id="trace-1",
            round_id="round-1",
            playback_id="playback-1",
        )

        self.assertEqual("trace-1", summary["trace_id"])
        self.assertEqual("round-1", summary["round_id"])
        self.assertEqual("playback-1", summary["playback_id"])
        self.assertEqual("utt-1", summary["utterance_id"])
        self.assertEqual(3, summary["audio_bytes"])
        self.assertEqual(120.0, summary["audio_duration_ms"])
        self.assertEqual("binary_stream", summary["audio_transport"])
        self.assertEqual("opus", summary["audio_encoding"])
        self.assertEqual(6, summary["opus_packets"])
        self.assertEqual(3, summary["stream_chunk_count"])

    def test_send_no_valid_speech_done_sends_status_then_done(self):
        websocket = _FakeWebSocket()

        asyncio.run(
            gateway._send_no_valid_speech_done(
                websocket,
                trace_id="trace-1",
                round_id="round-1",
                playback_id="playback-1",
            )
        )

        self.assertEqual(
            [
                {
                    "type": "status",
                    "message": "未检测到有效语音",
                },
                {
                    "type": "done",
                    "trace_id": "trace-1",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                },
            ],
            websocket.messages,
        )

    def test_send_no_valid_asr_result_records_trace_and_done(self):
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with patch.object(gateway, "trace_recorder", recorder):
            asyncio.run(
                gateway._send_no_valid_asr_result(
                    websocket,
                    trace_id="trace-1",
                    session_id="session-1",
                    round_seq=3,
                    round_id="round-1",
                    playback_id="playback-1",
                    stage="asr_invalid",
                    robot_id="test_01",
                    bot_id="xiaowen",
                    bot_name="小文",
                    asr_time_ms=12.3,
                    asr_trace_summary={"audio_bytes": 42},
                    asr_metadata={"language": "zh"},
                    text="嗯",
                )
            )

        self.assertEqual(
            [
                {"type": "status", "message": "未检测到有效语音"},
                {
                    "type": "done",
                    "trace_id": "trace-1",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                },
            ],
            websocket.messages,
        )
        trace = recorder.get_round("trace-1")["trace"]
        event = trace["events"][0]
        self.assertEqual("asr_invalid", trace["last_stage"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual(12.3, event["duration_ms"])
        self.assertEqual("嗯", event["summary"]["text"])
        self.assertEqual(42, event["summary"]["audio_bytes"])

    def test_send_asr_text_result_sends_recognition_payload(self):
        websocket = _FakeWebSocket()

        asyncio.run(
            gateway._send_asr_text_result(
                websocket,
                text="你好",
                asr_time_ms=123.4,
                asr_metadata={"language": "zh"},
                trace_id="trace-1",
                round_id="round-1",
                playback_id="playback-1",
            )
        )

        self.assertEqual(
            [
                {
                    "type": "text",
                    "content": "你好",
                    "asr_time_ms": 123.4,
                    "asr_metadata": {"language": "zh"},
                    "trace_id": "trace-1",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                }
            ],
            websocket.messages,
        )

    def test_send_valid_asr_result_records_trace_and_text_payload(self):
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with patch.object(gateway, "trace_recorder", recorder):
            asyncio.run(
                gateway._send_valid_asr_result(
                    websocket,
                    trace_id="trace-1",
                    session_id="session-1",
                    round_seq=3,
                    round_id="round-1",
                    playback_id="playback-1",
                    robot_id="test_01",
                    bot_id="xiaowen",
                    bot_name="小文",
                    text="你好",
                    asr_time_ms=12.3,
                    asr_trace_summary={"audio_bytes": 42},
                    asr_metadata={"language": "zh"},
                )
            )

        self.assertEqual(
            [
                {
                    "type": "text",
                    "content": "你好",
                    "asr_time_ms": 12.3,
                    "asr_metadata": {"language": "zh"},
                    "trace_id": "trace-1",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                }
            ],
            websocket.messages,
        )
        trace = recorder.get_round("trace-1")["trace"]
        event = trace["events"][0]
        self.assertEqual("asr_done", trace["last_stage"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual(12.3, event["duration_ms"])
        self.assertEqual("你好", event["summary"]["text"])
        self.assertEqual(42, event["summary"]["audio_bytes"])

    def test_record_interrupted_asr_result_marks_cancelled_without_client_message(self):
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        logger = _FakeLogger()

        with (
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "logger", logger),
        ):
            gateway._record_interrupted_asr_result(
                trace_id="trace-1",
                session_id="session-1",
                round_seq=3,
                robot_id="test_01",
                bot_id="xiaowen",
                bot_name="小文",
                asr_time_ms=12.3,
                asr_trace_summary={"audio_bytes": 42},
                asr_metadata={"language": "zh"},
            )

        trace = recorder.get_round("trace-1")["trace"]
        event = trace["events"][0]
        self.assertEqual("asr_interrupted", trace["last_stage"])
        self.assertTrue(trace["metrics"]["cancelled"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual(12.3, event["duration_ms"])
        self.assertTrue(event["summary"]["cancelled"])
        self.assertEqual(42, event["summary"]["audio_bytes"])
        self.assertEqual(
            [
                (
                    "会话 %s: ASR 完成后发现本轮已被新输入打断，跳过旧轮后续处理",
                    ("session-1",),
                )
            ],
            logger.info_messages,
        )

    def test_send_asr_start_status_and_trace_records_start_event(self):
        websocket = _FakeWebSocket()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with patch.object(gateway, "trace_recorder", recorder):
            asyncio.run(
                gateway._send_asr_start_status_and_trace(
                    websocket,
                    trace_id="trace-1",
                    session_id="session-1",
                    round_seq=3,
                    robot_id="test_01",
                    bot_id="xiaowen",
                    bot_name="小文",
                    asr_trace_summary={"audio_bytes": 42},
                )
            )

        self.assertEqual(
            [{"type": "status", "message": "识别中..."}],
            websocket.messages,
        )
        trace = recorder.get_round("trace-1")["trace"]
        self.assertEqual("asr_start", trace["last_stage"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual({"audio_bytes": 42}, trace["events"][0]["summary"])

    def test_stream_llm_tts_for_asr_result_preserves_history_query(self):
        websocket = _FakeWebSocket()
        calls = []

        async def fake_process_llm_tts_stream(
            query,
            session_id,
            websocket_arg,
            bot_id,
            bot_tts_settings,
            **kwargs,
        ):
            calls.append(
                {
                    "query": query,
                    "session_id": session_id,
                    "websocket": websocket_arg,
                    "bot_id": bot_id,
                    "bot_tts_settings": bot_tts_settings,
                    **kwargs,
                }
            )

        with (
            patch.object(
                gateway,
                "build_llm_query_with_audio_context",
                return_value="[语音上下文]\n用户说：你好",
            ),
            patch.object(gateway, "process_llm_tts_stream", fake_process_llm_tts_stream),
        ):
            asyncio.run(
                gateway._stream_llm_tts_for_asr_result(
                    text="你好",
                    session_id="session-1",
                    websocket=websocket,
                    bot_id="xiaowen",
                    bot_tts_settings={"tts_profile_id": "default_tts_profile"},
                    trace={"trace_id": "trace-1"},
                    asr_metadata={"emotion": "happy"},
                )
            )

        self.assertEqual(
            [{"type": "status", "message": "思考中..."}],
            websocket.messages,
        )
        self.assertEqual(1, len(calls))
        self.assertEqual("[语音上下文]\n用户说：你好", calls[0]["query"])
        self.assertEqual("你好", calls[0]["history_query"])
        self.assertEqual({"trace_id": "trace-1"}, calls[0]["trace"])
        self.assertIs(websocket, calls[0]["websocket"])

    def test_emit_asr_result_trace_records_result_summary(self):
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)

        with patch.object(gateway, "trace_recorder", recorder):
            gateway._emit_asr_result_trace(
                trace_id="trace-1",
                session_id="session-1",
                round_seq=3,
                stage="asr_invalid",
                robot_id="test_01",
                bot_id="xiaowen",
                bot_name="小文",
                asr_time_ms=123.4,
                asr_trace_summary={"audio_bytes": 42},
                asr_metadata={"language": "zh"},
                text="嗯",
                cancelled=True,
            )

        trace = recorder.get_round("trace-1")["trace"]
        event = trace["events"][0]
        self.assertEqual("asr_invalid", trace["last_stage"])
        self.assertEqual(123.4, event["duration_ms"])
        self.assertEqual("test_01", trace["robot_id"])
        self.assertEqual("xiaowen", trace["bot_id"])
        self.assertEqual("小文", trace["bot_name"])
        self.assertEqual(42, event["summary"]["audio_bytes"])
        self.assertEqual("{'language': 'zh'}", event["summary"]["metadata"])
        self.assertEqual("嗯", event["summary"]["text"])
        self.assertTrue(event["summary"]["cancelled"])

    def test_handle_audio_stream_message_lifecycle_enqueues_batch_audio(self):
        request_queue = asyncio.Queue(maxsize=1)
        audio_streams = {}
        websocket = _FakeWebSocket()
        fake_session_manager = _AudioStreamSessionManager()

        with (
            patch.object(gateway, "session_manager", fake_session_manager),
            patch.object(gateway, "ROBOT_SECRET_REQUIRED", False),
        ):
            handled_start = asyncio.run(
                gateway._handle_audio_stream_message(
                    websocket,
                    request_queue,
                    audio_streams,
                    session_id="session-1",
                    data={
                        "type": "audio_start",
                        "utterance_id": "u1",
                        "sample_rate": gateway.GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                        "channels": 1,
                        "opus_frame_ms": 20,
                    },
                    msg_type="audio_start",
                )
            )
            handled_chunk = asyncio.run(
                gateway._handle_audio_stream_message(
                    websocket,
                    request_queue,
                    audio_streams,
                    session_id="session-1",
                    data={
                        "type": "audio_chunk",
                        "utterance_id": "u1",
                        "audio_bytes": build_opus_packet_stream([b"abc"]),
                        "chunk_seq": 1,
                        "duration_ms": 20.0,
                    },
                    msg_type="audio_chunk",
                )
            )
            handled_end = asyncio.run(
                gateway._handle_audio_stream_message(
                    websocket,
                    request_queue,
                    audio_streams,
                    session_id="session-1",
                    data={"type": "audio_end", "utterance_id": "u1", "duration_ms": 20.0},
                    msg_type="audio_end",
                )
            )

        self.assertTrue(handled_start)
        self.assertTrue(handled_chunk)
        self.assertTrue(handled_end)
        self.assertEqual(["session-1", "session-1", "session-1"], fake_session_manager.touch_calls)
        self.assertEqual({}, audio_streams)
        queued = request_queue.get_nowait()
        self.assertEqual("audio", queued["type"])
        self.assertEqual("binary_stream", queued["audio_transport"])
        self.assertEqual("u1", queued["utterance_id"])
        self.assertEqual(1, queued["packet_count"])
        self.assertEqual(1, queued[REQUEST_QUEUE_METRICS_KEY]["queue_size"])
        self.assertEqual(
            [
                {
                    "type": "playback_cancel",
                    "round_id": "round-1",
                    "playback_id": "playback-1",
                    "reason": "audio_start",
                }
            ],
            websocket.messages,
        )

    def test_handle_audio_stream_message_cancel_removes_stream(self):
        request_queue = asyncio.Queue(maxsize=1)
        audio_streams = {"u1": object()}
        fake_session_manager = _AudioStreamSessionManager()

        with patch.object(gateway, "session_manager", fake_session_manager):
            handled = asyncio.run(
                gateway._handle_audio_stream_message(
                    _FakeWebSocket(),
                    request_queue,
                    audio_streams,
                    session_id="session-1",
                    data={"type": "audio_cancel", "utterance_id": "u1", "reason": "client_cancel"},
                    msg_type="audio_cancel",
                )
            )

        self.assertTrue(handled)
        self.assertEqual({}, audio_streams)
        self.assertEqual(["session-1"], fake_session_manager.touch_calls)

    def test_handle_queued_user_input_message_routes_supported_inputs(self):
        cases = [
            ("audio", {"type": "audio"}, "new_audio"),
            ("text", {"type": "text"}, "new_text"),
            ("client_event", {"type": "client_event", "event": "wake_interrupt"}, "new_client_event"),
        ]

        for msg_type, data, expected_reason in cases:
            with self.subTest(msg_type=msg_type):
                request_queue = asyncio.Queue(maxsize=1)
                audio_streams = {"u1": object()}
                websocket = _FakeWebSocket()
                fake_session_manager = _QueuedInputSessionManager()

                with patch.object(gateway, "session_manager", fake_session_manager):
                    handled = asyncio.run(
                        gateway._handle_queued_user_input_message(
                            websocket,
                            request_queue,
                            audio_streams,
                            session_id="session-1",
                            data=data,
                            msg_type=msg_type,
                        )
                    )

                self.assertTrue(handled)
                self.assertEqual({}, audio_streams)
                self.assertEqual(["session-1"], fake_session_manager.touch_calls)
                self.assertEqual(["session-1"], fake_session_manager.cancel_calls)
                self.assertIs(request_queue.get_nowait(), data)
                self.assertEqual(1, data[REQUEST_QUEUE_METRICS_KEY]["queue_size"])
                self.assertEqual(
                    [
                        {
                            "type": "playback_cancel",
                            "round_id": "round-1",
                            "playback_id": "playback-1",
                            "reason": expected_reason,
                        }
                    ],
                    websocket.messages,
                )

    def test_handle_queued_user_input_message_ignores_unknown_type(self):
        request_queue = asyncio.Queue(maxsize=1)
        audio_streams = {"u1": object()}
        fake_session_manager = _QueuedInputSessionManager()

        with patch.object(gateway, "session_manager", fake_session_manager):
            handled = asyncio.run(
                gateway._handle_queued_user_input_message(
                    _FakeWebSocket(),
                    request_queue,
                    audio_streams,
                    session_id="session-1",
                    data={"type": "other"},
                    msg_type="other",
                )
            )

        self.assertFalse(handled)
        self.assertEqual(["u1"], list(audio_streams))
        self.assertEqual([], fake_session_manager.touch_calls)
        self.assertEqual([], fake_session_manager.cancel_calls)

    def test_request_trace_id_can_be_distinct_from_round_id(self):
        data = {
            "trace_id": "rust-trace-1",
            "playback_id": "session-1:7:playback",
        }

        round_id = request_round_id(data, "session-1", 7)
        trace_id = request_trace_id(data, round_id)
        playback_id = request_playback_id(data, round_id)

        self.assertEqual("session-1:7", round_id)
        self.assertEqual("rust-trace-1", trace_id)
        self.assertEqual("session-1:7:playback", playback_id)

    def test_playback_report_numeric_helpers_ignore_negative_bool_and_invalid_values(self):
        data = {
            "good_int": "4",
            "negative_int": -3,
            "bool_int": True,
            "bad_int": "oops",
            "good_float": "1.25",
            "negative_float": -1.25,
            "bool_float": False,
            "bad_float": "oops",
        }

        self.assertEqual(4, int_from_client_report(data, "good_int"))
        self.assertEqual(0, int_from_client_report(data, "negative_int"))
        self.assertEqual(0, int_from_client_report(data, "bool_int"))
        self.assertEqual(0, int_from_client_report(data, "bad_int"))
        self.assertEqual(1.25, float_from_client_report(data, "good_float"))
        self.assertEqual(0.0, float_from_client_report(data, "negative_float"))
        self.assertIsNone(float_from_client_report(data, "bool_float"))
        self.assertIsNone(float_from_client_report(data, "bad_float"))

    def test_round_seq_from_trace_id_requires_matching_session_prefix(self):
        self.assertEqual(7, round_seq_from_trace_id("session-1", "session-1:7"))
        self.assertIsNone(round_seq_from_trace_id("session-1", "other:7"))
        self.assertIsNone(round_seq_from_trace_id("session-1", "session-1:bad"))

    def test_client_event_phrase_pool_covers_expected_events(self):
        self.assertEqual(
            {"startup_ready", "wake_idle", "wake_interrupt", "sleep_exit"},
            gateway.CLIENT_EVENT_TYPES,
        )
        expected_minimums = {
            "startup_ready": 16,
            "wake_idle": 16,
            "wake_interrupt": 16,
            "sleep_exit": 16,
        }
        total = sum(len(items) for items in gateway.CLIENT_EVENT_PHRASES.values())
        self.assertGreaterEqual(total, 64)
        for event_type in gateway.CLIENT_EVENT_TYPES:
            phrases = gateway.CLIENT_EVENT_PHRASES[event_type]
            self.assertGreaterEqual(len(phrases), expected_minimums[event_type])
            self.assertEqual(len(phrases), len(set(phrases)))
            self.assertIn(
                gateway.pick_client_event_phrase(event_type),
                phrases,
            )

    def test_record_client_playback_report_adds_trace_metrics(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        round_id = f"{session_id}:3"

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
        ):
            gateway._record_client_playback_report(
                session_id,
                {
                    "type": "playback_complete",
                    "round_id": round_id,
                    "playback_id": f"{round_id}:playback",
                    "pushed_chunks": 4,
                    "pushed_samples": 8000,
                    "underrun_callbacks": 0,
                    "zero_filled_samples": 0,
                    "max_buffered_samples": 12000,
                },
                interrupted=False,
            )

        trace = recorder.get_round(round_id)["trace"]
        self.assertEqual("client_playback_completed", trace["last_stage"])
        self.assertEqual(3, trace["round_seq"])
        metrics = trace["metrics"]
        self.assertTrue(metrics["client_playback_completed"])
        self.assertFalse(metrics["client_playback_interrupted"])
        self.assertEqual(4.0, metrics["client_playback_chunks"])
        self.assertEqual(8000.0, metrics["client_playback_samples"])
        self.assertEqual(12000.0, metrics["client_playback_max_buffered_samples"])

    def test_record_client_playback_report_prefers_client_trace_id(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        round_id = f"{session_id}:5"

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
        ):
            gateway._record_client_playback_report(
                session_id,
                {
                    "type": "playback_complete",
                    "trace_id": "rust-trace-5",
                    "round_id": round_id,
                    "playback_id": f"{round_id}:playback",
                    "pushed_chunks": 4,
                    "pushed_samples": 8000,
                },
                interrupted=False,
            )

        trace = recorder.get_round("rust-trace-5")["trace"]
        self.assertEqual("client_playback_completed", trace["last_stage"])
        self.assertEqual(5, trace["round_seq"])
        self.assertEqual(round_id, trace["events"][0]["summary"]["round_id"])
        self.assertEqual("rust-trace-5", trace["events"][0]["summary"]["trace_id"])
        self.assertEqual(4.0, trace["metrics"]["client_playback_chunks"])

    def test_record_client_playback_report_marks_interrupted(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        round_id = f"{session_id}:4"

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
        ):
            gateway._record_client_playback_report(
                session_id,
                {
                    "type": "playback_interrupted",
                    "round_id": round_id,
                    "playback_id": f"{round_id}:playback",
                    "reason": "wake_interrupt",
                    "pushed_chunks": 2,
                    "pushed_samples": 4000,
                },
                interrupted=True,
            )

        trace = recorder.get_round(round_id)["trace"]
        self.assertEqual("client_playback_interrupted", trace["last_stage"])
        self.assertTrue(trace["metrics"]["client_playback_interrupted"])
        self.assertTrue(trace["metrics"]["cancelled"])
        self.assertEqual("wake_interrupt", trace["events"][0]["summary"]["reason"])

    def test_record_client_playback_report_releases_workflow_barrier_when_enabled(self):
        manager = gateway.SessionManager()
        session_id = manager.create_session()
        recorder = gateway.TraceRecorder(max_events=20, max_rounds=10)
        barriers = Mock()
        round_id = f"{session_id}:6"

        with (
            patch.object(gateway, "session_manager", manager),
            patch.object(gateway, "trace_recorder", recorder),
            patch.object(gateway, "workflow_playback_barriers", barriers),
            patch.object(gateway, "GATEWAY_COMPLEX_WORKFLOW_ENABLED", True),
        ):
            gateway._record_client_playback_report(
                session_id,
                {
                    "round_id": round_id,
                    "playback_id": f"{round_id}:playback",
                },
                interrupted=False,
            )

        barriers.record_report.assert_called_once_with(
            session_id=session_id,
            round_id=round_id,
            playback_id=f"{round_id}:playback",
            report_type=gateway.PLAYBACK_COMPLETE,
            reason=None,
        )


class _FakeSessionManager:
    def __init__(self):
        self.interrupt_calls = []

    def set_interrupted(self, session_id, value=True):
        self.interrupt_calls.append((session_id, value))


class _RoundCancellingSessionManager:
    def __init__(self):
        self.cancel_calls = []

    def cancel_current_round(self, session_id):
        self.cancel_calls.append(session_id)
        return {"round_id": "round-1", "playback_id": "playback-1"}


class _TouchSessionManager:
    def __init__(self):
        self.touch_calls = []

    def touch_session(self, session_id):
        self.touch_calls.append(session_id)


class _AudioStreamSessionManager:
    def __init__(self):
        self.touch_calls = []

    def touch_session(self, session_id):
        self.touch_calls.append(session_id)

    def is_registered(self, session_id):
        return True

    def cancel_current_round(self, session_id):
        return {"round_id": "round-1", "playback_id": "playback-1"}


class _TurnGateSessionManager(_AudioStreamSessionManager):
    history = [
        {"role": "user", "content": "我们去哪里？"},
        {"role": "assistant", "content": "去北京怎么样？"},
    ]

    def get_session(self, session_id):
        if session_id == "candidate-session":
            return SimpleNamespace(client_type="go_voice_gateway")
        return None

    def get_history(self, session_id):
        return list(self.history) if session_id == "python-main-session" else []


class _QueuedInputSessionManager:
    def __init__(self):
        self.touch_calls = []
        self.cancel_calls = []

    def touch_session(self, session_id):
        self.touch_calls.append(session_id)

    def cancel_current_round(self, session_id):
        self.cancel_calls.append(session_id)
        return {"round_id": "round-1", "playback_id": "playback-1"}


class _RegisteredStateSessionManager:
    def __init__(self, *, registered):
        self.registered = registered
        self.is_registered_calls = []

    def is_registered(self, session_id):
        self.is_registered_calls.append(session_id)
        return self.registered


class _RuntimeContextSessionManager:
    def __init__(self, *, robot_id):
        self.robot_id = robot_id
        self.get_robot_id_calls = []

    def get_robot_id(self, session_id):
        self.get_robot_id_calls.append(session_id)
        return self.robot_id


class _FakeGatewayRuntimeState:
    def __init__(self):
        self.tts_settings_calls = []

    def get_bot_tts_settings(self, bot_id):
        self.tts_settings_calls.append(bot_id)
        return {"tts_profile_id": "default_tts_profile"}


class _FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.warning_messages = []

    def info(self, message, *args):
        self.info_messages.append((message, args))

    def warning(self, message, *args):
        self.warning_messages.append((message, args))


class _FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


if __name__ == "__main__":
    unittest.main()
