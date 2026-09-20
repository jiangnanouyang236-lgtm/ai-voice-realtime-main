import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

os.environ["CONFIG_DATABASE_URL"] = ""

from gateway import gateway_server as gateway
from gateway.audio_protocol import decode_audio_frame, encode_audio_frame
from gateway.internal_voice_protocol import (
    TYPE_CLIENT_EVENT,
    TYPE_HEARTBEAT_PING,
    TYPE_HEARTBEAT_PONG,
    TYPE_INPUT_AUDIO_CANCEL,
    TYPE_INPUT_AUDIO_BATCH,
    TYPE_INPUT_AUDIO_END,
    TYPE_INPUT_AUDIO_START,
    TYPE_INPUT_TEXT_COMMIT,
    TYPE_INTERRUPT,
    TYPE_ORCHESTRATOR_STATUS,
    TYPE_PLAYBACK_REPORT,
    TYPE_PROTOCOL_ERROR,
    TYPE_RESPONSE_ASR,
    TYPE_RESPONSE_DONE,
    TYPE_RESPONSE_CANCELLED,
    TYPE_SESSION_CLOSE,
    TYPE_SESSION_OPEN,
    ClientEventPayload,
    InputAudioEndPayload,
    InputAudioStartPayload,
    InputTextCommitPayload,
    InternalVoiceEnvelope,
    PlaybackReportPayload,
    SessionOpenPayload,
)
from gateway.opus_audio import build_opus_packet_stream


def _pop_session_state(payload, *, transition, state):
    session_state = payload.pop("session_state")
    assert session_state["transition"] == transition
    assert session_state["state"] == state
    assert "previous_state" in session_state
    assert session_state["sequence"] >= 1
    return session_state


def test_internal_voice_writer_suppresses_legacy_error_during_active_turn():
    class FakeWebSocket:
        def __init__(self):
            self.json_messages = []

        async def send_json(self, payload):
            self.json_messages.append(payload)

    async def run():
        raw = FakeWebSocket()
        websocket = gateway._SerializedInternalVoiceWebSocket(raw)
        control = {}
        websocket.set_turn_control(control)
        await websocket.send_json(
            {"type": "error", "code": "TTS_FAILED", "message": "channel closed"}
        )
        return raw, control

    raw, control = asyncio.run(run())

    assert raw.json_messages == []
    assert control["legacy_error"] == {
        "code": "TTS_FAILED",
        "message": "channel closed",
    }


def test_internal_voice_writer_converts_asr_text_to_typed_response():
    class FakeWebSocket:
        def __init__(self):
            self.json_messages = []

        async def send_json(self, payload):
            self.json_messages.append(payload)

    async def run():
        raw = FakeWebSocket()
        websocket = gateway._SerializedInternalVoiceWebSocket(raw)
        websocket.set_turn_control(
            {
                "session_id": "rtc_1",
                "trace_id": "trace_1",
                "utterance_id": "utterance_1",
                "round_id": "round_1",
                "playback_id": "playback_1",
            }
        )
        await websocket.send_json(
            {
                "type": "text",
                "content": "今天天气不错",
                "asr_time_ms": 123.5,
                "asr_metadata": {"provider": "internal-only"},
            }
        )
        return raw

    raw = asyncio.run(run())
    response = raw.json_messages[0]

    assert response["type"] == TYPE_RESPONSE_ASR
    assert response["session_id"] == "rtc_1"
    assert response["trace_id"] == "trace_1"
    assert response["utterance_id"] == "utterance_1"
    assert response["round_id"] == "round_1"
    assert response["playback_id"] == "playback_1"
    assert response["payload"] == {
        "text": "今天天气不错",
        "valid": True,
        "final": True,
        "asr_time_ms": 123.5,
    }


def test_internal_voice_ws_can_be_disabled(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", False)
    client = TestClient(gateway.app)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/internal/voice/ws"):
            pass


def test_internal_voice_ws_accepts_session_open_and_close(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    client = TestClient(gateway.app)

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_test_1",
                payload=SessionOpenPayload(
                    robot_id="test_01",
                    client_type="go_gateway",
                    bot_id="xiaowen",
                ).to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )

        opened = ws.receive_json()
        assert opened["type"] == "session.opened"
        assert opened["session_id"] == "rtc_test_1"
        opened_payload = opened["payload"]
        _pop_session_state(opened_payload, transition=TYPE_SESSION_OPEN, state="idle")
        assert opened_payload == {
            "accepted": True,
            "protocol": "internal_voice_ws",
            "mode": "handshake_only",
            "pre_registered": False,
        }

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_CLOSE,
                "rtc_test_1",
                timestamp_ms=124,
            ).to_dict()
        )

        closed = ws.receive_json()
        assert closed["type"] == "session.closed"
        assert closed["session_id"] == "rtc_test_1"
        closed_payload = closed["payload"]
        _pop_session_state(closed_payload, transition=TYPE_SESSION_CLOSE, state="closed")
        assert closed_payload == {
            "closed": True,
            "protocol": "internal_voice_ws",
        }


def test_internal_voice_ws_reports_protocol_errors(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    client = TestClient(gateway.app)

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            {
                "version": 1,
                "type": "session.open",
                "timestamp_ms": 123,
                "payload": {},
            }
        )

        error = ws.receive_json()
        assert error["type"] == TYPE_PROTOCOL_ERROR
        assert error["session_id"] == "unknown"
        assert error["payload"]["code"] == "INVALID_ENVELOPE"
        assert "session_id" in error["payload"]["message"]


def test_internal_voice_ws_acknowledges_client_event_without_orchestration(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE", "ack_only")
    client = TestClient(gateway.app)

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_test_1",
                payload=SessionOpenPayload(
                    robot_id="test_01",
                    client_type="go_gateway",
                    bot_id="xiaowen",
                ).to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        opened = ws.receive_json()
        assert opened["type"] == "session.opened"
        opened_payload = opened["payload"]
        _pop_session_state(opened_payload, transition=TYPE_SESSION_OPEN, state="idle")
        assert opened_payload == {
            "accepted": True,
            "protocol": "internal_voice_ws",
            "mode": "handshake_only",
            "pre_registered": False,
        }

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_CLIENT_EVENT,
                "rtc_test_1",
                trace_id="trace_1",
                payload=ClientEventPayload(
                    event="wake_interrupt",
                    event_id="event_1",
                    source="hardware_wake",
                    bot_id="xiaowen",
                ).to_dict(),
                timestamp_ms=124,
            ).to_dict()
        )

        ack = ws.receive_json()
        assert ack["type"] == TYPE_ORCHESTRATOR_STATUS
        assert ack["session_id"] == "rtc_test_1"
        assert ack["trace_id"] == "trace_1"
        ack_payload = ack["payload"]
        _pop_session_state(ack_payload, transition=TYPE_CLIENT_EVENT, state="speaking")
        assert ack_payload == {
            "status": "accepted",
            "protocol": "internal_voice_ws",
            "mode": "client_event_ack_only",
            "event": "wake_interrupt",
            "event_id": "event_1",
            "source": "hardware_wake",
        }


def test_internal_voice_ws_active_client_event_forwards_gateway_frames(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE", "active")
    registered_payloads = []
    handled_events = []

    async def fake_register_robot_session(session_id, payload):
        registered_payloads.append((session_id, dict(payload)))
        gateway.session_manager.register_robot(
            session_id,
            robot_id=payload["robot_id"],
            bot_id="xiaowen",
            bot_name="小文",
            client_type=payload.get("client_type"),
            is_new_robot=False,
        )
        return {
            "robot_id": payload["robot_id"],
            "bot_id": "xiaowen",
            "bot_name": "小文",
            "client_type": payload.get("client_type"),
            "is_new_robot": False,
        }

    async def fake_handle_client_event(websocket, session_id, data, **kwargs):
        assert gateway.session_manager.is_registered(session_id)
        handled_events.append((session_id, dict(data), dict(kwargs)))
        await websocket.send_json(
            {
                "type": "playback_start",
                "trace_id": kwargs["trace_id"],
                "round_id": kwargs["round_id"],
                "playback_id": kwargs["playback_id"],
            }
        )
        await websocket.send_bytes(
            encode_audio_frame(
                b"test-audio",
                direction="server_tts",
                encoding="opus",
                trace_id=kwargs["trace_id"],
                round_id=kwargs["round_id"],
                playback_id=kwargs["playback_id"],
            )
        )
        await websocket.send_json(
            {
                "type": "done",
                "exit": data["event"] == "sleep_exit",
                "trace_id": kwargs["trace_id"],
                "round_id": kwargs["round_id"],
                "playback_id": kwargs["playback_id"],
            }
        )

    monkeypatch.setattr(gateway, "register_robot_session", fake_register_robot_session)
    monkeypatch.setattr(gateway, "handle_client_event", fake_handle_client_event)

    client = TestClient(gateway.app)
    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_active_1",
                payload={
                    **SessionOpenPayload(
                        robot_id="test_01",
                        client_type="go_gateway",
                        bot_id="xiaowen",
                    ).to_dict(),
                    "robot_secret": "secret_1",
                },
                timestamp_ms=123,
            ).to_dict()
        )
        opened = ws.receive_json()
        assert opened["type"] == "session.opened"
        opened_payload = opened["payload"]
        _pop_session_state(opened_payload, transition=TYPE_SESSION_OPEN, state="idle")
        assert opened_payload == {
            "accepted": True,
            "protocol": "internal_voice_ws",
            "mode": "handshake_only",
            "pre_registered": True,
        }
        assert registered_payloads == [
            (
                "rtc_active_1",
                {
                    "type": "register",
                    "robot_id": "test_01",
                    "robot_secret": "secret_1",
                    "client_type": "go_gateway",
                },
            )
        ]

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_CLIENT_EVENT,
                "rtc_active_1",
                trace_id="trace_active_1",
                payload=ClientEventPayload(
                    event="wake_interrupt",
                    event_id="event_1",
                    source="hardware_wake",
                    bot_id="xiaowen",
                ).to_dict(),
                timestamp_ms=124,
            ).to_dict()
            )

        accepted = ws.receive_json()
        assert accepted["type"] == TYPE_ORCHESTRATOR_STATUS
        assert accepted["payload"]["mode"] == "client_event_active_accepted"
        playback_start = ws.receive_json()
        assert playback_start["type"] == "playback_start"
        assert playback_start["trace_id"] == "trace_active_1"
        assert playback_start["round_id"] == "trace_active_1"
        assert playback_start["playback_id"] == "trace_active_1:playback"
        audio_header, audio_payload = decode_audio_frame(ws.receive_bytes())
        assert audio_payload == b"test-audio"
        assert audio_header["event_type"] == "response.audio"
        assert audio_header["direction"] == "downlink"
        assert audio_header["session_id"] == "rtc_active_1"
        done = ws.receive_json()
        assert done["type"] == "done"
        assert done["exit"] is False
        response_done = ws.receive_json()
        assert response_done["type"] == TYPE_RESPONSE_DONE
        assert response_done["trace_id"] == "trace_active_1"
        response_payload = response_done["payload"]
        _pop_session_state(response_payload, transition=TYPE_RESPONSE_DONE, state="idle")
        assert response_payload == {
            "exit": False,
            "reason": "completed",
            "protocol": "internal_voice_ws",
            "mode": "client_event_active",
            "event": "wake_interrupt",
            "event_id": "event_1",
            "source": "hardware_wake",
        }

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_CLIENT_EVENT,
                "rtc_active_1",
                trace_id="trace_active_1",
                payload=ClientEventPayload(
                    event="wake_interrupt",
                    event_id="event_1",
                    source="hardware_wake",
                    bot_id="xiaowen",
                ).to_dict(),
                timestamp_ms=125,
            ).to_dict()
        )
        duplicate = ws.receive_json()
        assert duplicate["type"] == TYPE_ORCHESTRATOR_STATUS
        assert duplicate["payload"]["mode"] == "client_event_active_duplicate"
        assert duplicate["payload"]["duplicate"] is True
        assert ws.receive_json()["type"] == TYPE_RESPONSE_DONE

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_CLIENT_EVENT,
                "rtc_active_1",
                trace_id="trace_active_2",
                payload=ClientEventPayload(
                    event="wake_idle",
                    event_id="event_2",
                    source="hardware_wake",
                    bot_id="xiaowen",
                ).to_dict(),
                timestamp_ms=126,
            ).to_dict()
        )
        assert ws.receive_json()["payload"]["mode"] == "client_event_active_accepted"
        second_start = ws.receive_json()
        assert second_start["type"] == "playback_start"
        assert second_start["trace_id"] == "trace_active_2"
        second_audio_header, second_audio_payload = decode_audio_frame(ws.receive_bytes())
        assert second_audio_payload == b"test-audio"
        assert second_audio_header["trace_id"] == "trace_active_2"
        assert ws.receive_json()["type"] == "done"
        second_terminal = ws.receive_json()
        assert second_terminal["type"] == TYPE_RESPONSE_DONE
        assert second_terminal["trace_id"] == "trace_active_2"

    assert registered_payloads == [
        (
            "rtc_active_1",
            {
                "type": "register",
                "robot_id": "test_01",
                "robot_secret": "secret_1",
                "client_type": "go_gateway",
            },
        )
    ]
    assert handled_events[0][0] == "rtc_active_1"
    assert handled_events[0][1]["event"] == "wake_interrupt"
    assert len(handled_events) == 2
    assert handled_events[1][1]["event_id"] == "event_2"
    assert handled_events[1][2]["trace_id"] == "trace_active_2"


def test_internal_voice_ws_interrupt_cancels_active_turn_without_blocking_reader(
    monkeypatch,
):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE", "active")

    async def fake_register_robot_session(session_id, payload):
        gateway.session_manager.register_robot(
            session_id,
            robot_id=payload["robot_id"],
            bot_id="xiaowen",
            bot_name="小文",
            client_type=payload.get("client_type"),
            is_new_robot=False,
        )
        return {"robot_id": payload["robot_id"], "bot_id": "xiaowen"}

    async def blocking_handle_client_event(websocket, session_id, data, **kwargs):
        await websocket.send_json(
            {
                "type": "playback_start",
                "trace_id": kwargs["trace_id"],
                "round_id": kwargs["round_id"],
                "playback_id": kwargs["playback_id"],
            }
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(gateway, "register_robot_session", fake_register_robot_session)
    monkeypatch.setattr(gateway, "handle_client_event", blocking_handle_client_event)

    client = TestClient(gateway.app)
    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_interrupt_1",
                payload={
                    **SessionOpenPayload(
                        robot_id="test_01",
                        client_type="go_gateway",
                    ).to_dict(),
                    "robot_secret": "secret_1",
                },
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_CLIENT_EVENT,
                "rtc_interrupt_1",
                trace_id="trace_active_1",
                payload=ClientEventPayload(
                    event="wake_idle",
                    event_id="event_active_1",
                ).to_dict(),
                timestamp_ms=124,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == TYPE_ORCHESTRATOR_STATUS
        assert ws.receive_json()["type"] == "playback_start"

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INTERRUPT,
                "rtc_interrupt_1",
                trace_id="trace_interrupt_1",
                payload={"reason": "wake_interrupt", "source": "hardware_wake"},
                timestamp_ms=125,
            ).to_dict()
        )

        frames = [ws.receive_json(), ws.receive_json()]
        by_type = {frame["type"]: frame for frame in frames}
        interrupt_ack = by_type[TYPE_ORCHESTRATOR_STATUS]
        assert interrupt_ack["trace_id"] == "trace_interrupt_1"
        assert interrupt_ack["payload"]["status"] == "accepted"
        cancelled = by_type[TYPE_RESPONSE_CANCELLED]
        assert cancelled["trace_id"] == "trace_active_1"
        assert cancelled["payload"]["reason"] == "wake_interrupt"
        assert cancelled["payload"]["expected"] is True

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_CLOSE,
                "rtc_interrupt_1",
                timestamp_ms=126,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.closed"


def test_internal_voice_ws_heartbeat_echoes_nonce(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    client = TestClient(gateway.app)

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_heartbeat_1",
                timestamp_ms=122,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_HEARTBEAT_PING,
                "rtc_heartbeat_1",
                payload={"nonce": "ping-1"},
                timestamp_ms=123,
            ).to_dict()
        )
        pong = ws.receive_json()

    assert pong["type"] == TYPE_HEARTBEAT_PONG
    assert pong["session_id"] == "rtc_heartbeat_1"
    assert pong["payload"] == {"nonce": "ping-1"}


def test_internal_voice_ws_rejects_session_switch_on_open_connection(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    client = TestClient(gateway.app)

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_session_1",
                timestamp_ms=122,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_HEARTBEAT_PING,
                "rtc_session_2",
                payload={"nonce": "wrong-session"},
                timestamp_ms=123,
            ).to_dict()
        )
        error = ws.receive_json()

    assert error["type"] == TYPE_PROTOCOL_ERROR
    assert error["session_id"] == "rtc_session_1"
    assert error["payload"]["code"] == "SESSION_MISMATCH"


def test_internal_voice_ws_acknowledges_input_audio_metadata_without_asr(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    client = TestClient(gateway.app)

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_test_1",
                payload=SessionOpenPayload(robot_id="test_01").to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_START,
                "rtc_test_1",
                trace_id="trace_audio",
                utterance_id="utt_1",
                payload=InputAudioStartPayload(
                    codec="opus",
                    packet_format="rtp_opus",
                    sample_rate=48000,
                    channels=1,
                    frame_ms=20,
                    ice_route="turn",
                ).to_dict(),
                timestamp_ms=124,
            ).to_dict()
        )
        start_ack = ws.receive_json()
        assert start_ack["type"] == TYPE_ORCHESTRATOR_STATUS
        assert start_ack["utterance_id"] == "utt_1"
        start_payload = start_ack["payload"]
        start_state = _pop_session_state(
            start_payload,
            transition=TYPE_INPUT_AUDIO_START,
            state="receiving_audio",
        )
        assert start_state["active_utterance_id"] == "utt_1"
        assert start_payload == {
            "status": "accepted",
            "protocol": "internal_voice_ws",
            "mode": "input_audio_metadata_ack_only",
            "input_audio_type": TYPE_INPUT_AUDIO_START,
            "utterance_id": "utt_1",
            "codec": "opus",
            "packet_format": "rtp_opus",
            "sample_rate": 48000,
            "channels": 1,
            "frame_ms": 20,
            "ice_route": "turn",
        }

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_END,
                "rtc_test_1",
                trace_id="trace_audio",
                utterance_id="utt_1",
                payload=InputAudioEndPayload(
                    packet_count=0,
                    payload_bytes=0,
                    duration_ms=0,
                ).to_dict(),
                timestamp_ms=125,
            ).to_dict()
        )
        end_ack = ws.receive_json()
        end_payload = end_ack["payload"]
        end_state = _pop_session_state(
            end_payload,
            transition=TYPE_INPUT_AUDIO_END,
            state="orchestrating",
        )
        assert end_state["active_utterance_id"] == "utt_1"
        assert end_payload == {
            "status": "accepted",
            "protocol": "internal_voice_ws",
            "mode": "input_audio_metadata_ack_only",
            "input_audio_type": TYPE_INPUT_AUDIO_END,
            "utterance_id": "utt_1",
            "packet_count": 0,
            "payload_bytes": 0,
            "duration_ms": 0,
            "lossy": False,
        }

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_CANCEL,
                "rtc_test_1",
                trace_id="trace_audio",
                utterance_id="utt_1",
                payload={
                    "reason": "interrupt",
                    "source": "hardware_wake",
                    "transport": "webrtc_datachannel",
                },
                timestamp_ms=126,
            ).to_dict()
        )
        cancel_ack = ws.receive_json()
        cancel_payload = cancel_ack["payload"]
        _pop_session_state(
            cancel_payload,
            transition=TYPE_INPUT_AUDIO_CANCEL,
            state="idle",
        )
        assert cancel_payload == {
            "status": "accepted",
            "protocol": "internal_voice_ws",
            "mode": "input_audio_metadata_ack_only",
            "input_audio_type": TYPE_INPUT_AUDIO_CANCEL,
            "utterance_id": "utt_1",
            "reason": "interrupt",
            "source": "hardware_wake",
            "transport": "webrtc_datachannel",
        }


def test_internal_voice_ws_accepts_and_correlates_audio_batch_shadow(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    client = TestClient(gateway.app)
    audio_payload = build_opus_packet_stream([b"abc", b"defg"])

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_test_1",
                payload=SessionOpenPayload(robot_id="test_01").to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_START,
                "rtc_test_1",
                trace_id="trace_audio",
                utterance_id="utt_1",
                payload=InputAudioStartPayload(
                    codec="opus",
                    packet_format="OPUSRAW1",
                    sample_rate=16000,
                    channels=1,
                    frame_ms=20,
                ).to_dict(),
                timestamp_ms=124,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == TYPE_ORCHESTRATOR_STATUS

        ws.send_bytes(
            encode_audio_frame(
                audio_payload,
                event_type=TYPE_INPUT_AUDIO_BATCH,
                direction="uplink",
                session_id="rtc_test_1",
                trace_id="trace_audio",
                utterance_id="utt_1",
                encoding="opus",
                sample_rate=16000,
                channels=1,
                opus_frame_ms=20,
                packet_count=2,
                payload_bytes=len(audio_payload),
            )
        )
        batch_ack = ws.receive_json()
        assert batch_ack["type"] == TYPE_ORCHESTRATOR_STATUS
        assert batch_ack["trace_id"] == "trace_audio"
        assert batch_ack["utterance_id"] == "utt_1"
        assert batch_ack["payload"] == {
            "status": "accepted",
            "protocol": "internal_voice_ws",
            "mode": "input_audio_batch_shadow",
            "input_audio_type": TYPE_INPUT_AUDIO_BATCH,
            "utterance_id": "utt_1",
            "packet_count": 2,
            "payload_bytes": len(audio_payload),
            "duplicate": False,
        }

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_END,
                "rtc_test_1",
                trace_id="trace_audio",
                utterance_id="utt_1",
                payload=InputAudioEndPayload(
                    packet_count=2,
                    payload_bytes=len(audio_payload),
                    duration_ms=40,
                ).to_dict(),
                timestamp_ms=125,
            ).to_dict()
        )
        end_ack = ws.receive_json()
        assert end_ack["payload"]["mode"] == "input_audio_batch_shadow_ready"
        assert end_ack["payload"]["packet_count"] == 2
        assert end_ack["payload"]["payload_bytes"] == len(audio_payload)


def test_internal_voice_ws_runs_audio_batch_active_with_typed_terminal(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    audio_payload = build_opus_packet_stream([b"abc"])
    handled_audio = []

    async def fake_ensure_registered(session_id, open_payload):
        return None

    async def fake_handle_audio(websocket, session_id, data, **kwargs):
        handled_audio.append((session_id, data["utterance_id"]))
        assert session_id == "rtc_audio_active_1"
        assert data["audio_bytes"] == audio_payload
        assert data["utterance_id"] == "utt_active_1"
        await websocket.send_json(
            {
                "type": "text",
                "content": "今天天气不错",
                "asr_time_ms": 123.5,
            }
        )
        await websocket.send_json(
            {
                "type": "playback_start",
                "trace_id": kwargs["trace_id"],
                "round_id": kwargs["round_id"],
                "playback_id": kwargs["playback_id"],
            }
        )
        await websocket.send_bytes(
            encode_audio_frame(
                b"tts-opus",
                direction="server_tts",
                encoding="opus",
                trace_id=kwargs["trace_id"],
                round_id=kwargs["round_id"],
                playback_id=kwargs["playback_id"],
            )
        )
        await websocket.send_json(
            {
                "type": "done",
                "exit": False,
                "trace_id": kwargs["trace_id"],
                "round_id": kwargs["round_id"],
                "playback_id": kwargs["playback_id"],
            }
        )

    monkeypatch.setattr(
        gateway,
        "_ensure_internal_voice_active_session_registered",
        fake_ensure_registered,
    )
    monkeypatch.setattr(gateway, "handle_audio", fake_handle_audio)

    client = TestClient(gateway.app)
    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_audio_active_1",
                payload=SessionOpenPayload(robot_id="test_01").to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_START,
                "rtc_audio_active_1",
                trace_id="trace_audio_active_1",
                utterance_id="utt_active_1",
                payload=InputAudioStartPayload(
                    codec="opus",
                    packet_format="OPUSRAW1",
                    sample_rate=16000,
                    channels=1,
                    frame_ms=20,
                ).to_dict(),
                timestamp_ms=124,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == TYPE_ORCHESTRATOR_STATUS

        ws.send_bytes(
            encode_audio_frame(
                audio_payload,
                event_type=TYPE_INPUT_AUDIO_BATCH,
                direction="uplink",
                session_id="rtc_audio_active_1",
                trace_id="trace_audio_active_1",
                utterance_id="utt_active_1",
                encoding="opus",
                sample_rate=16000,
                channels=1,
                opus_frame_ms=20,
                packet_count=1,
                payload_bytes=len(audio_payload),
            )
        )
        assert ws.receive_json()["type"] == TYPE_ORCHESTRATOR_STATUS

        end_payload = InputAudioEndPayload(
            packet_count=1,
            payload_bytes=len(audio_payload),
            duration_ms=20,
        ).to_dict()
        end_payload["orchestration_mode"] = "active"
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_END,
                "rtc_audio_active_1",
                trace_id="trace_audio_active_1",
                utterance_id="utt_active_1",
                payload=end_payload,
                timestamp_ms=125,
            ).to_dict()
        )
        accepted = ws.receive_json()
        assert accepted["type"] == TYPE_ORCHESTRATOR_STATUS
        assert accepted["payload"]["mode"] == "input_audio_active_accepted"

        asr_result = ws.receive_json()
        assert asr_result["type"] == TYPE_RESPONSE_ASR
        assert asr_result["trace_id"] == "trace_audio_active_1"
        assert asr_result["utterance_id"] == "utt_active_1"
        assert asr_result["round_id"] == "trace_audio_active_1"
        assert asr_result["playback_id"] == "trace_audio_active_1:playback"
        assert asr_result["payload"] == {
            "text": "今天天气不错",
            "valid": True,
            "final": True,
            "asr_time_ms": 123.5,
        }

        playback_start = ws.receive_json()
        assert playback_start["type"] == "playback_start"
        assert playback_start["trace_id"] == "trace_audio_active_1"
        assert playback_start["round_id"] == "trace_audio_active_1"
        assert playback_start["playback_id"] == "trace_audio_active_1:playback"

        audio_header, tts_payload = decode_audio_frame(ws.receive_bytes())
        assert tts_payload == b"tts-opus"
        assert audio_header["event_type"] == "response.audio"
        assert audio_header["direction"] == "downlink"
        assert audio_header["session_id"] == "rtc_audio_active_1"
        assert audio_header["trace_id"] == "trace_audio_active_1"
        assert audio_header["round_id"] == "trace_audio_active_1"
        assert audio_header["playback_id"] == "trace_audio_active_1:playback"

        assert ws.receive_json()["type"] == "done"
        terminal = ws.receive_json()
        assert terminal["type"] == TYPE_RESPONSE_DONE
        assert terminal["trace_id"] == "trace_audio_active_1"
        assert terminal["utterance_id"] == "utt_active_1"
        assert terminal["round_id"] == "trace_audio_active_1"
        assert terminal["playback_id"] == "trace_audio_active_1:playback"
        assert terminal["payload"]["mode"] == "input_audio_active"
        assert terminal["payload"]["reason"] == "completed"

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_AUDIO_END,
                "rtc_audio_active_1",
                trace_id="trace_audio_active_1",
                utterance_id="utt_active_1",
                payload=end_payload,
                timestamp_ms=126,
            ).to_dict()
        )
        duplicate = ws.receive_json()
        assert duplicate["type"] == TYPE_ORCHESTRATOR_STATUS
        assert duplicate["payload"]["mode"] == "input_audio_active_duplicate"
        assert duplicate["payload"]["duplicate"] is True
        assert duplicate["payload"]["status"] == "terminal"
        assert duplicate["payload"]["terminal_type"] == TYPE_RESPONSE_DONE
        assert handled_audio == [("rtc_audio_active_1", "utt_active_1")]


def test_internal_voice_ws_active_text_commit_preserves_m1_session_and_is_idempotent(
    monkeypatch,
):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    handled_text = []

    async def fake_ensure_registered(session_id, open_payload):
        return None

    async def fake_handle_text(websocket, session_id, data, **kwargs):
        handled_text.append((session_id, dict(data), dict(kwargs)))
        await websocket.send_json(
            {
                "type": "text",
                "content": data["content"],
                "asr_time_ms": None,
            }
        )
        await websocket.send_json(
            {
                "type": "done",
                "exit": False,
                "trace_id": kwargs["trace_id"],
                "round_id": kwargs["round_id"],
                "playback_id": kwargs["playback_id"],
            }
        )

    monkeypatch.setattr(
        gateway,
        "_ensure_internal_voice_active_session_registered",
        fake_ensure_registered,
    )
    monkeypatch.setattr(gateway, "handle_text", fake_handle_text)

    request = InternalVoiceEnvelope.create(
        TYPE_INPUT_TEXT_COMMIT,
        "rtc_agent_1",
        trace_id="trace_text_1",
        utterance_id="utt_text_1",
        payload=InputTextCommitPayload(
            content="可以开始吧",
            source="turn_gate_candidate_asr",
            asr_time_ms=88.5,
            candidate_seq=3,
            speech_epoch=2,
            audio_watermark=9600,
        ).to_dict(),
        timestamp_ms=124,
    ).to_dict()

    client = TestClient(gateway.app)
    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_agent_1",
                payload=SessionOpenPayload(robot_id="test_01").to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"

        ws.send_json(request)
        accepted = ws.receive_json()
        assert accepted["type"] == TYPE_ORCHESTRATOR_STATUS
        assert accepted["session_id"] == "rtc_agent_1"
        assert accepted["payload"]["mode"] == "input_text_active_accepted"

        asr_result = ws.receive_json()
        assert asr_result["type"] == TYPE_RESPONSE_ASR
        assert asr_result["session_id"] == "rtc_agent_1"
        assert asr_result["utterance_id"] == "utt_text_1"
        assert asr_result["payload"] == {
            "text": "可以开始吧",
            "valid": True,
            "final": True,
            "asr_time_ms": 88.5,
        }

        assert ws.receive_json()["type"] == "done"
        terminal = ws.receive_json()
        assert terminal["type"] == TYPE_RESPONSE_DONE
        assert terminal["session_id"] == "rtc_agent_1"
        assert terminal["payload"]["mode"] == "input_text_active"

        ws.send_json(request)
        duplicate = ws.receive_json()
        assert duplicate["type"] == TYPE_ORCHESTRATOR_STATUS
        assert duplicate["payload"]["mode"] == "input_text_active_duplicate"
        assert duplicate["payload"]["duplicate"] is True
        assert ws.receive_json()["type"] == TYPE_RESPONSE_DONE

    assert len(handled_text) == 1
    assert handled_text[0][0] == "rtc_agent_1"
    assert handled_text[0][1]["content"] == "可以开始吧"
    assert handled_text[0][1]["candidate_seq"] == 3


def test_internal_voice_ws_inflight_duplicate_text_commit_receives_same_terminal(
    monkeypatch,
):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    handled_text = []

    async def fake_ensure_registered(session_id, open_payload):
        return None

    async def delayed_handle_text(websocket, session_id, data, **kwargs):
        handled_text.append((session_id, dict(data)))
        await asyncio.sleep(0.05)
        await websocket.send_json(
            {"type": "text", "content": data["content"], "asr_time_ms": None}
        )
        await websocket.send_json(
            {
                "type": "done",
                "exit": False,
                "trace_id": kwargs["trace_id"],
                "round_id": kwargs["round_id"],
                "playback_id": kwargs["playback_id"],
            }
        )

    monkeypatch.setattr(
        gateway,
        "_ensure_internal_voice_active_session_registered",
        fake_ensure_registered,
    )
    monkeypatch.setattr(gateway, "handle_text", delayed_handle_text)
    request = InternalVoiceEnvelope.create(
        TYPE_INPUT_TEXT_COMMIT,
        "rtc_inflight_1",
        trace_id="trace_inflight_1",
        utterance_id="utt_inflight_1",
        payload=InputTextCommitPayload(
            content="好，开始吧",
            source="turn_gate_candidate_asr",
        ).to_dict(),
        timestamp_ms=124,
    ).to_dict()

    client = TestClient(gateway.app)
    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_inflight_1",
                payload=SessionOpenPayload(robot_id="test_01").to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"

        ws.send_json(request)
        assert ws.receive_json()["payload"]["mode"] == "input_text_active_accepted"
        ws.send_json(request)
        frames = [ws.receive_json() for _ in range(5)]

    assert len(handled_text) == 1
    duplicate = next(
        frame
        for frame in frames
        if frame["type"] == TYPE_ORCHESTRATOR_STATUS
    )
    assert duplicate["payload"]["mode"] == "input_text_active_duplicate"
    assert sum(frame["type"] == TYPE_RESPONSE_DONE for frame in frames) == 2
    assert sum(frame["type"] == TYPE_RESPONSE_ASR for frame in frames) == 1


def test_internal_voice_ws_rejects_empty_text_commit_without_orchestration(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    handled = []

    async def unexpected_handle_text(*args, **kwargs):
        handled.append(True)

    monkeypatch.setattr(gateway, "handle_text", unexpected_handle_text)
    client = TestClient(gateway.app)
    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_empty_text_1",
                payload=SessionOpenPayload(robot_id="test_01").to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INPUT_TEXT_COMMIT,
                "rtc_empty_text_1",
                trace_id="trace_empty_1",
                utterance_id="utt_empty_1",
                payload={"content": "   "},
                timestamp_ms=124,
            ).to_dict()
        )
        error = ws.receive_json()

    assert error["type"] == TYPE_PROTOCOL_ERROR
    assert error["payload"]["code"] == "INPUT_TEXT_CONTENT_MISSING"
    assert handled == []


def test_internal_voice_ws_acknowledges_interrupt_and_playback_report(monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
    client = TestClient(gateway.app)

    with client.websocket_connect("/internal/voice/ws") as ws:
        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_SESSION_OPEN,
                "rtc_test_1",
                payload=SessionOpenPayload(robot_id="test_01").to_dict(),
                timestamp_ms=123,
            ).to_dict()
        )
        assert ws.receive_json()["type"] == "session.opened"

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_INTERRUPT,
                "rtc_test_1",
                trace_id="trace_interrupt",
                utterance_id="utt_1",
                payload={
                    "reason": "wake_interrupt",
                    "source": "hardware_wake",
                    "transport": "webrtc_datachannel",
                },
                timestamp_ms=124,
            ).to_dict()
        )
        interrupt_ack = ws.receive_json()
        assert interrupt_ack["type"] == TYPE_ORCHESTRATOR_STATUS
        assert interrupt_ack["trace_id"] == "trace_interrupt"
        assert interrupt_ack["utterance_id"] == "utt_1"
        interrupt_payload = interrupt_ack["payload"]
        _pop_session_state(interrupt_payload, transition=TYPE_INTERRUPT, state="idle")
        assert interrupt_payload == {
            "status": "accepted",
            "protocol": "internal_voice_ws",
            "mode": "interrupt_ack_only",
            "reason": "wake_interrupt",
            "source": "hardware_wake",
            "transport": "webrtc_datachannel",
        }

        ws.send_json(
            InternalVoiceEnvelope.create(
                TYPE_PLAYBACK_REPORT,
                "rtc_test_1",
                trace_id="trace_playback",
                round_id="round_1",
                playback_id="playback_1",
                payload=PlaybackReportPayload(
                    report_type="playback_complete",
                    first_audio_to_playback_start_ms=117.5,
                    pushed_chunks=72,
                    pushed_samples=23040,
                ).to_dict(),
                timestamp_ms=125,
            ).to_dict()
        )
        playback_ack = ws.receive_json()
        assert playback_ack["type"] == TYPE_ORCHESTRATOR_STATUS
        assert playback_ack["trace_id"] == "trace_playback"
        assert playback_ack["round_id"] == "round_1"
        assert playback_ack["playback_id"] == "playback_1"
        playback_payload = playback_ack["payload"]
        _pop_session_state(playback_payload, transition=TYPE_PLAYBACK_REPORT, state="idle")
        assert playback_payload == {
            "status": "accepted",
            "protocol": "internal_voice_ws",
            "mode": "playback_report_ack_only",
            "report_type": "playback_complete",
            "reason": None,
            "round_id": "round_1",
            "playback_id": "playback_1",
        }
