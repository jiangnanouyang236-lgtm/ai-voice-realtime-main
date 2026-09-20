"""Typed Go-Python M1 internal voice protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any, Mapping


PROTOCOL_VERSION = 1

TYPE_SESSION_OPEN = "session.open"
TYPE_SESSION_OPENED = "session.opened"
TYPE_SESSION_CLOSE = "session.close"
TYPE_SESSION_CLOSED = "session.closed"
TYPE_HEARTBEAT_PING = "heartbeat.ping"
TYPE_HEARTBEAT_PONG = "heartbeat.pong"
TYPE_INPUT_AUDIO_START = "input_audio.start"
TYPE_INPUT_AUDIO_BATCH = "input_audio.batch"
TYPE_INPUT_AUDIO_END = "input_audio.end"
TYPE_INPUT_AUDIO_CANCEL = "input_audio.cancel"
TYPE_INPUT_TEXT_COMMIT = "input_text.commit"
TYPE_CLIENT_EVENT = "client_event"
TYPE_INTERRUPT = "interrupt"
TYPE_PLAYBACK_REPORT = "playback.report"
TYPE_ORCHESTRATOR_STATUS = "orchestrator.status"
TYPE_PROTOCOL_ERROR = "protocol.error"
TYPE_RESPONSE_ASR = "response.asr"
TYPE_RESPONSE_AUDIO = "response.audio"
TYPE_RESPONSE_DONE = "response.done"
TYPE_RESPONSE_CANCELLED = "response.cancelled"
TYPE_RESPONSE_ERROR = "response.error"
TYPE_TRACE_EVENT = "trace.event"


@dataclass(frozen=True)
class InternalVoiceEnvelope:
    version: int
    type: str
    session_id: str
    timestamp_ms: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    utterance_id: str | None = None
    round_id: str | None = None
    playback_id: str | None = None

    @classmethod
    def create(
        cls,
        event_type: str,
        session_id: str,
        *,
        payload: Mapping[str, Any] | None = None,
        trace_id: str | None = None,
        utterance_id: str | None = None,
        round_id: str | None = None,
        playback_id: str | None = None,
        timestamp_ms: int | None = None,
    ) -> "InternalVoiceEnvelope":
        envelope = cls(
            version=PROTOCOL_VERSION,
            type=str(event_type or "").strip(),
            session_id=str(session_id or "").strip(),
            trace_id=_optional_str(trace_id),
            utterance_id=_optional_str(utterance_id),
            round_id=_optional_str(round_id),
            playback_id=_optional_str(playback_id),
            timestamp_ms=timestamp_ms if timestamp_ms is not None else now_millis(),
            payload=dict(payload or {}),
        )
        envelope.validate()
        return envelope

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InternalVoiceEnvelope":
        envelope = cls(
            version=int(data.get("version") or 0),
            type=str(data.get("type") or "").strip(),
            session_id=str(data.get("session_id") or "").strip(),
            trace_id=_optional_str(data.get("trace_id")),
            utterance_id=_optional_str(data.get("utterance_id")),
            round_id=_optional_str(data.get("round_id")),
            playback_id=_optional_str(data.get("playback_id")),
            timestamp_ms=int(data.get("timestamp_ms") or 0),
            payload=dict(data.get("payload") or {}),
        )
        envelope.validate()
        return envelope

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "version": self.version,
            "type": self.type,
            "session_id": self.session_id,
            "timestamp_ms": self.timestamp_ms,
            "payload": dict(self.payload),
        }
        for key in ("trace_id", "utterance_id", "round_id", "playback_id"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload

    def validate(self) -> None:
        if self.version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported internal voice protocol version: {self.version}")
        if not self.type:
            raise ValueError("internal voice event type is required")
        if "." not in self.type and self.type not in {TYPE_CLIENT_EVENT, TYPE_INTERRUPT}:
            raise ValueError(f"internal voice event type must be dotted: {self.type}")
        if not self.session_id:
            raise ValueError("internal voice session_id is required")
        if (self.type.startswith("input_audio.") or self.type == TYPE_INPUT_TEXT_COMMIT) and not self.utterance_id:
            raise ValueError(f"internal voice utterance_id is required for {self.type}")
        if self.type in {TYPE_RESPONSE_ASR, TYPE_RESPONSE_AUDIO, TYPE_RESPONSE_DONE} and (
            not self.round_id or not self.playback_id
        ):
            raise ValueError(
                f"internal voice round_id and playback_id are required for {self.type}"
            )


@dataclass(frozen=True)
class SessionOpenPayload:
    robot_id: str | None = None
    client_type: str | None = None
    bot_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class InputAudioStartPayload:
    codec: str
    packet_format: str
    sample_rate: int
    channels: int
    frame_ms: int | None = None
    ice_route: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class InputAudioEndPayload:
    packet_count: int
    payload_bytes: int
    duration_ms: int
    lossy: bool = False
    dropped_packets: int = 0
    duplicate_packets: int = 0
    sequence_gaps: int = 0
    timestamp_regressions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class InputTextCommitPayload:
    content: str
    source: str | None = None
    bot_id: str | None = None
    asr_time_ms: float | None = None
    candidate_seq: int | None = None
    speech_epoch: int | None = None
    audio_watermark: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class ClientEventPayload:
    event: str
    event_id: str | None = None
    source: str | None = None
    bot_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class PlaybackReportPayload:
    report_type: str
    reason: str | None = None
    first_audio_to_playback_start_ms: float | None = None
    playback_start_to_complete_ms: float | None = None
    pushed_chunks: int | None = None
    pushed_samples: int | None = None
    underrun_callbacks: int | None = None
    zero_filled_samples: int | None = None
    max_buffered_samples: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class ResponseAudioPayload:
    chunk_seq: int
    codec: str
    sample_rate: int
    channels: int
    duration_ms: int | None = None
    is_final: bool = False
    payload_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class HeartbeatPayload:
    nonce: str

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class ResponseDonePayload:
    exit: bool
    reason: str | None = None
    audio_chunks: int | None = None
    audio_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class ResponseCancelledPayload:
    reason: str
    cancelled_stage: str | None = None
    expected: bool = True

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


@dataclass(frozen=True)
class ResponseErrorPayload:
    origin: str
    stage: str
    code: str
    message: str
    cause_code: str | None = None
    retryable: bool = False
    fatal: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _compact(asdict(self))


def now_millis() -> int:
    return int(time.time() * 1000)


def _optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _compact(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
