from gateway.internal_voice_protocol import (
    TYPE_CLIENT_EVENT,
    TYPE_INPUT_AUDIO_CANCEL,
    TYPE_INPUT_AUDIO_END,
    TYPE_INPUT_AUDIO_START,
    TYPE_INPUT_TEXT_COMMIT,
    TYPE_INTERRUPT,
    TYPE_PLAYBACK_REPORT,
    TYPE_RESPONSE_CANCELLED,
    TYPE_RESPONSE_ERROR,
    TYPE_SESSION_CLOSE,
    TYPE_SESSION_OPEN,
    InternalVoiceEnvelope,
)
from gateway.internal_voice_session_state import (
    STATE_CLOSED,
    STATE_IDLE,
    STATE_OPENING,
    STATE_ORCHESTRATING,
    STATE_RECEIVING_AUDIO,
    STATE_SPEAKING,
    InternalVoiceSessionStateTracker,
)


def test_internal_voice_state_tracks_audio_lifecycle():
    tracker = InternalVoiceSessionStateTracker("rtc_1")

    opened = tracker.apply(
        InternalVoiceEnvelope.create(TYPE_SESSION_OPEN, "rtc_1", timestamp_ms=1)
    )
    assert opened == {
        "previous_state": STATE_OPENING,
        "state": STATE_IDLE,
        "transition": TYPE_SESSION_OPEN,
        "sequence": 1,
    }

    started = tracker.apply(
        InternalVoiceEnvelope.create(
            TYPE_INPUT_AUDIO_START,
            "rtc_1",
            utterance_id="utt_1",
            timestamp_ms=2,
        )
    )
    assert started["previous_state"] == STATE_IDLE
    assert started["state"] == STATE_RECEIVING_AUDIO
    assert started["active_utterance_id"] == "utt_1"

    ended = tracker.apply(
        InternalVoiceEnvelope.create(
            TYPE_INPUT_AUDIO_END,
            "rtc_1",
            utterance_id="utt_1",
            timestamp_ms=3,
        )
    )
    assert ended["previous_state"] == STATE_RECEIVING_AUDIO
    assert ended["state"] == STATE_ORCHESTRATING
    assert ended["active_utterance_id"] == "utt_1"

    cancelled = tracker.apply(
        InternalVoiceEnvelope.create(
            TYPE_INPUT_AUDIO_CANCEL,
            "rtc_1",
            utterance_id="utt_1",
            timestamp_ms=4,
        )
    )
    assert cancelled == {
        "previous_state": STATE_ORCHESTRATING,
        "state": STATE_IDLE,
        "transition": TYPE_INPUT_AUDIO_CANCEL,
        "sequence": 4,
    }


def test_internal_voice_state_tracks_control_and_playback():
    tracker = InternalVoiceSessionStateTracker("rtc_1")
    tracker.apply(InternalVoiceEnvelope.create(TYPE_SESSION_OPEN, "rtc_1", timestamp_ms=1))

    client_event = tracker.apply(
        InternalVoiceEnvelope.create(TYPE_CLIENT_EVENT, "rtc_1", timestamp_ms=2)
    )
    assert client_event["previous_state"] == STATE_IDLE
    assert client_event["state"] == STATE_SPEAKING

    interrupted = tracker.apply(
        InternalVoiceEnvelope.create(TYPE_INTERRUPT, "rtc_1", timestamp_ms=3)
    )
    assert interrupted["previous_state"] == STATE_SPEAKING
    assert interrupted["state"] == STATE_IDLE

    playback = tracker.apply(
        InternalVoiceEnvelope.create(
            TYPE_PLAYBACK_REPORT,
            "rtc_1",
            round_id="round_1",
            playback_id="playback_1",
            timestamp_ms=4,
        )
    )
    assert playback["previous_state"] == STATE_IDLE
    assert playback["state"] == STATE_IDLE

    closed = tracker.apply(
        InternalVoiceEnvelope.create(TYPE_SESSION_CLOSE, "rtc_1", timestamp_ms=5)
    )
    assert closed["previous_state"] == STATE_IDLE
    assert closed["state"] == STATE_CLOSED


def test_internal_voice_state_tracks_text_commit_as_same_session_orchestration():
    tracker = InternalVoiceSessionStateTracker("rtc_1")
    tracker.apply(InternalVoiceEnvelope.create(TYPE_SESSION_OPEN, "rtc_1", timestamp_ms=1))

    committed = tracker.apply(
        InternalVoiceEnvelope.create(
            TYPE_INPUT_TEXT_COMMIT,
            "rtc_1",
            trace_id="trace_1",
            utterance_id="utt_1",
            round_id="round_1",
            playback_id="round_1:playback",
            payload={"content": "好，开始吧"},
            timestamp_ms=2,
        )
    )

    assert committed["previous_state"] == STATE_IDLE
    assert committed["state"] == STATE_ORCHESTRATING
    assert committed["active_utterance_id"] == "utt_1"
    assert committed["active_round_id"] == "round_1"


def test_internal_voice_state_terminal_cancel_and_error_clear_active_turn():
    for terminal_type in (TYPE_RESPONSE_CANCELLED, TYPE_RESPONSE_ERROR):
        tracker = InternalVoiceSessionStateTracker("rtc_1")
        tracker.apply(
            InternalVoiceEnvelope.create(TYPE_SESSION_OPEN, "rtc_1", timestamp_ms=1)
        )
        tracker.apply(
            InternalVoiceEnvelope.create(
                TYPE_CLIENT_EVENT,
                "rtc_1",
                trace_id="trace_1",
                round_id="round_1",
                playback_id="round_1:playback",
                timestamp_ms=2,
            )
        )

        terminal = tracker.apply(
            InternalVoiceEnvelope.create(
                terminal_type,
                "rtc_1",
                trace_id="trace_1",
                round_id="round_1",
                playback_id="round_1:playback",
                timestamp_ms=3,
            )
        )

        assert terminal["previous_state"] == STATE_SPEAKING
        assert terminal["state"] == STATE_IDLE
        assert "active_round_id" not in terminal
        assert "active_playback_id" not in terminal
