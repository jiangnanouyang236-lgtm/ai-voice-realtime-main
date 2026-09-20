import json
from pathlib import Path

import pytest

from gateway.internal_voice_protocol import (
    TYPE_HEARTBEAT_PING,
    PROTOCOL_VERSION,
    TYPE_INPUT_AUDIO_END,
    TYPE_INPUT_TEXT_COMMIT,
    TYPE_PROTOCOL_ERROR,
    TYPE_RESPONSE_AUDIO,
    TYPE_RESPONSE_CANCELLED,
    TYPE_RESPONSE_ERROR,
    TYPE_SESSION_OPEN,
    InputAudioEndPayload,
    InputTextCommitPayload,
    HeartbeatPayload,
    InternalVoiceEnvelope,
    ResponseCancelledPayload,
    ResponseErrorPayload,
    ResponseAudioPayload,
    SessionOpenPayload,
)


def test_internal_voice_envelope_session_open_round_trips():
    envelope = InternalVoiceEnvelope.create(
        TYPE_SESSION_OPEN,
        "rtc_1",
        payload=SessionOpenPayload(
            robot_id="test_01",
            client_type="rust",
            bot_id="xiaowen",
        ).to_dict(),
        timestamp_ms=123,
    )

    encoded = envelope.to_dict()

    assert encoded == {
        "version": PROTOCOL_VERSION,
        "type": "session.open",
        "session_id": "rtc_1",
        "timestamp_ms": 123,
        "payload": {
            "robot_id": "test_01",
            "client_type": "rust",
            "bot_id": "xiaowen",
        },
    }
    assert InternalVoiceEnvelope.from_dict(encoded) == envelope


def test_internal_voice_envelope_requires_audio_utterance_id():
    with pytest.raises(ValueError, match="utterance_id"):
        InternalVoiceEnvelope.create(
            TYPE_INPUT_AUDIO_END,
            "rtc_1",
            payload=InputAudioEndPayload(
                packet_count=10,
                payload_bytes=1000,
                duration_ms=200,
            ).to_dict(),
            timestamp_ms=123,
        )

    envelope = InternalVoiceEnvelope.create(
        TYPE_INPUT_AUDIO_END,
        "rtc_1",
        utterance_id="utt_1",
        payload=InputAudioEndPayload(
            packet_count=10,
            payload_bytes=1000,
            duration_ms=200,
            lossy=True,
        ).to_dict(),
        timestamp_ms=123,
    )

    assert envelope.to_dict()["utterance_id"] == "utt_1"
    assert envelope.to_dict()["payload"]["lossy"] is True


def test_internal_voice_envelope_requires_response_round_ids():
    with pytest.raises(ValueError, match="round_id and playback_id"):
        InternalVoiceEnvelope.create(
            TYPE_RESPONSE_AUDIO,
            "rtc_1",
            trace_id="trace_1",
            payload=ResponseAudioPayload(
                chunk_seq=1,
                codec="opus",
                sample_rate=48000,
                channels=1,
            ).to_dict(),
            timestamp_ms=123,
        )

    envelope = InternalVoiceEnvelope.create(
        TYPE_RESPONSE_AUDIO,
        "rtc_1",
        trace_id="trace_1",
        round_id="round_1",
        playback_id="round_1:playback",
        payload=ResponseAudioPayload(
            chunk_seq=1,
            codec="opus",
            sample_rate=48000,
            channels=1,
            payload_bytes=320,
        ).to_dict(),
        timestamp_ms=123,
    )

    encoded = envelope.to_dict()
    assert encoded["round_id"] == "round_1"
    assert encoded["playback_id"] == "round_1:playback"
    assert encoded["payload"]["payload_bytes"] == 320


def test_internal_voice_text_commit_requires_utterance_id_and_keeps_candidate_metadata():
    payload = InputTextCommitPayload(
        content="好，开始吧",
        source="turn_gate_candidate_asr",
        asr_time_ms=88.5,
        candidate_seq=3,
        speech_epoch=2,
        audio_watermark=9600,
    ).to_dict()
    with pytest.raises(ValueError, match="utterance_id"):
        InternalVoiceEnvelope.create(
            TYPE_INPUT_TEXT_COMMIT,
            "rtc_1",
            payload=payload,
            timestamp_ms=123,
        )

    envelope = InternalVoiceEnvelope.create(
        TYPE_INPUT_TEXT_COMMIT,
        "rtc_1",
        trace_id="trace_1",
        utterance_id="utt_1",
        payload=payload,
        timestamp_ms=123,
    )
    assert envelope.payload["candidate_seq"] == 3
    assert envelope.payload["audio_watermark"] == 9600


def test_internal_voice_envelope_allows_protocol_error_without_round_ids():
    envelope = InternalVoiceEnvelope.create(
        TYPE_PROTOCOL_ERROR,
        "rtc_1",
        payload={"code": "INVALID_ENVELOPE", "message": "bad payload"},
        timestamp_ms=123,
    )

    assert envelope.to_dict()["type"] == "protocol.error"
    assert envelope.to_dict()["payload"]["code"] == "INVALID_ENVELOPE"


def test_internal_voice_protocol_v1_cross_language_fixtures():
    fixture_path = (
        Path(__file__).parent / "fixtures" / "internal_voice_protocol_v1.json"
    )
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))

    ping = InternalVoiceEnvelope.from_dict(fixtures["heartbeat_ping"])
    assert ping.type == TYPE_HEARTBEAT_PING
    assert ping.payload == HeartbeatPayload(nonce="ping-1").to_dict()

    cancelled = InternalVoiceEnvelope.from_dict(fixtures["response_cancelled"])
    assert cancelled.type == TYPE_RESPONSE_CANCELLED
    assert cancelled.payload == ResponseCancelledPayload(
        reason="wake_interrupt",
        cancelled_stage="tts",
        expected=True,
    ).to_dict()

    response_error = InternalVoiceEnvelope.from_dict(fixtures["response_error"])
    assert response_error.type == TYPE_RESPONSE_ERROR
    assert response_error.payload == ResponseErrorPayload(
        origin="python_gateway",
        stage="tts",
        code="TTS_UPSTREAM_UNAVAILABLE",
        message="TTS upstream unavailable",
        cause_code="UNAVAILABLE",
        retryable=True,
        fatal=False,
    ).to_dict()
