"""Session-state helper for the M1 internal voice protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gateway.internal_voice_protocol import (
    TYPE_CLIENT_EVENT,
    TYPE_INPUT_AUDIO_CANCEL,
    TYPE_INPUT_AUDIO_END,
    TYPE_INPUT_AUDIO_START,
    TYPE_INPUT_TEXT_COMMIT,
    TYPE_INTERRUPT,
    TYPE_PLAYBACK_REPORT,
    TYPE_RESPONSE_AUDIO,
    TYPE_RESPONSE_CANCELLED,
    TYPE_RESPONSE_DONE,
    TYPE_RESPONSE_ERROR,
    TYPE_SESSION_CLOSE,
    TYPE_SESSION_OPEN,
    InternalVoiceEnvelope,
)


STATE_OPENING = "opening"
STATE_IDLE = "idle"
STATE_RECEIVING_AUDIO = "receiving_audio"
STATE_ORCHESTRATING = "orchestrating"
STATE_SPEAKING = "speaking"
STATE_CLOSING = "closing"
STATE_CLOSED = "closed"


@dataclass
class InternalVoiceSessionStateTracker:
    session_id: str
    state: str = STATE_OPENING
    sequence: int = 0
    active_utterance_id: str | None = None
    active_round_id: str | None = None
    active_playback_id: str | None = None

    def apply(self, envelope: InternalVoiceEnvelope) -> dict[str, Any]:
        if envelope.session_id != self.session_id:
            raise ValueError(
                f"internal voice session mismatch: expected={self.session_id} "
                f"actual={envelope.session_id}"
            )

        previous_state = self.state
        event_type = envelope.type

        if event_type == TYPE_SESSION_OPEN:
            self.state = STATE_IDLE
        elif event_type == TYPE_INPUT_AUDIO_START:
            self.state = STATE_RECEIVING_AUDIO
            self.active_utterance_id = envelope.utterance_id
        elif event_type == TYPE_INPUT_AUDIO_END:
            self.state = STATE_ORCHESTRATING
            self.active_utterance_id = envelope.utterance_id
        elif event_type == TYPE_INPUT_AUDIO_CANCEL:
            self.state = STATE_IDLE
            self.active_utterance_id = None
        elif event_type == TYPE_CLIENT_EVENT:
            self.state = STATE_SPEAKING
            self.active_round_id = envelope.round_id
            self.active_playback_id = envelope.playback_id
        elif event_type == TYPE_INPUT_TEXT_COMMIT:
            self.state = STATE_ORCHESTRATING
            self.active_utterance_id = envelope.utterance_id
            self.active_round_id = envelope.round_id
            self.active_playback_id = envelope.playback_id
        elif event_type == TYPE_INTERRUPT:
            self.state = STATE_IDLE
            self.active_utterance_id = None
            self.active_round_id = None
            self.active_playback_id = None
        elif event_type == TYPE_PLAYBACK_REPORT:
            self.state = STATE_IDLE
            self.active_round_id = None
            self.active_playback_id = None
        elif event_type == TYPE_RESPONSE_AUDIO:
            self.state = STATE_SPEAKING
            self.active_round_id = envelope.round_id
            self.active_playback_id = envelope.playback_id
        elif event_type in {
            TYPE_RESPONSE_DONE,
            TYPE_RESPONSE_CANCELLED,
            TYPE_RESPONSE_ERROR,
        }:
            self.state = STATE_IDLE
            self.active_utterance_id = None
            self.active_round_id = None
            self.active_playback_id = None
        elif event_type == TYPE_SESSION_CLOSE:
            self.state = STATE_CLOSED
            self.active_utterance_id = None
            self.active_round_id = None
            self.active_playback_id = None
        else:
            self.state = previous_state

        self.sequence += 1
        return _compact(
            {
                "previous_state": previous_state,
                "state": self.state,
                "transition": event_type,
                "sequence": self.sequence,
                "active_utterance_id": self.active_utterance_id,
                "active_round_id": self.active_round_id,
                "active_playback_id": self.active_playback_id,
            }
        )


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
