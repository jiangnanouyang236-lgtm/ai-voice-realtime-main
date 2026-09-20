import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.barge_in import (
    DECISION_IGNORE,
    DECISION_INTERRUPT,
    DECISION_NEW_INTENT,
    decide_barge_in,
)
from gateway import gateway_server as gateway


def test_barge_in_classifier_keeps_backchannels_without_hiding_new_intent():
    assert decide_barge_in("嗯").decision == DECISION_IGNORE
    assert decide_barge_in("好的。 ").decision == DECISION_IGNORE
    assert decide_barge_in("嗯，帮我查一下天气").decision == DECISION_NEW_INTENT


def test_barge_in_classifier_distinguishes_stop_from_followup_task():
    assert decide_barge_in("停").decision == DECISION_INTERRUPT
    assert decide_barge_in("别说了").decision == DECISION_INTERRUPT
    assert decide_barge_in("停一下吧").decision == DECISION_INTERRUPT
    assert decide_barge_in("别唱了，换一首").decision == DECISION_NEW_INTENT


def test_barge_in_classifier_fails_open_to_current_playback():
    assert decide_barge_in("").decision == DECISION_IGNORE
    assert decide_barge_in("帮我查天气", asr_ok=False).decision == DECISION_IGNORE


def _probe_payload():
    return {
        "type": "barge_in_probe",
        "audio_data": b"pcm",
        "audio_metadata": {"duration_ms": 800},
        "trace_id": "trace-1",
        "utterance_id": "barge-1",
        "round_id": "round-1",
        "playback_id": "playback-1",
        "candidate_seq": 1,
        "speech_epoch": 1,
        "audio_watermark": 12800,
    }


def test_barge_in_probe_is_asr_only_and_returns_identity():
    websocket = SimpleNamespace()
    sent = []

    async def fake_send_message(_websocket, message_type, **payload):
        sent.append({"type": message_type, **payload})

    async def run():
        with (
            patch.object(gateway, "NATURAL_BARGE_IN_ENABLED", True),
            patch.object(gateway, "ROBOT_SECRET_REQUIRED", False),
            patch.object(gateway, "_decode_audio_request_payload", return_value=SimpleNamespace(audio_data=b"pcm")),
            patch.object(gateway, "process_asr", AsyncMock(return_value=("帮我查天气", 24.0, {"provider": "test"}))) as asr,
            patch.object(gateway, "send_message", side_effect=fake_send_message),
        ):
            handled = await gateway._handle_barge_in_probe_message(
                websocket,
                session_id="session-1",
                data=_probe_payload(),
                msg_type="barge_in_probe",
            )
            assert handled is True
            asr.assert_awaited_once()

    asyncio.run(run())
    assert sent[0]["type"] == "barge_in_probe_result"
    assert sent[0]["decision"] == DECISION_NEW_INTENT
    assert sent[0]["playback_id"] == "playback-1"
    assert sent[1]["type"] == "done"


def test_barge_in_probe_disabled_rejects_before_asr():
    websocket = SimpleNamespace()

    async def run():
        with (
            patch.object(gateway, "NATURAL_BARGE_IN_ENABLED", False),
            patch.object(gateway, "send_error", AsyncMock()) as send_error,
            patch.object(gateway, "process_asr", AsyncMock()) as asr,
        ):
            handled = await gateway._handle_barge_in_probe_message(
                websocket,
                session_id="session-1",
                data=_probe_payload(),
                msg_type="barge_in_probe",
            )
            assert handled is True
            send_error.assert_awaited_once()
            asr.assert_not_awaited()

    asyncio.run(run())


def test_barge_in_probe_invalid_identity_rejects_before_asr():
    websocket = SimpleNamespace()
    payload = _probe_payload()
    payload["playback_id"] = ""

    async def run():
        with (
            patch.object(gateway, "NATURAL_BARGE_IN_ENABLED", True),
            patch.object(gateway, "send_error", AsyncMock()) as send_error,
            patch.object(gateway, "process_asr", AsyncMock()) as asr,
        ):
            handled = await gateway._handle_barge_in_probe_message(
                websocket,
                session_id="session-1",
                data=payload,
                msg_type="barge_in_probe",
            )
            assert handled is True
            send_error.assert_awaited_once()
            asr.assert_not_awaited()

    asyncio.run(run())
