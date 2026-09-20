"""Pure Turn Gate policy and provisional-candidate lifecycle primitives.

This module intentionally has no model, network, or Gateway side effects.  It
locks the v1 decision semantics before the transport/runtime path is wired.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TurnGateAction(str, Enum):
    CONTINUE = "continue"
    EARLY_COMMIT = "early_commit"
    FALLBACK_COMMIT = "fallback_commit"


@dataclass(frozen=True)
class TurnGateConfig:
    """V1 timings; active runtime wiring remains feature-gated."""

    candidate_silence_ms: int = 300
    legacy_fallback_silence_ms: int = 500

    def __post_init__(self) -> None:
        if self.candidate_silence_ms < 200:
            raise ValueError("candidate_silence_ms must be at least 200")
        if self.legacy_fallback_silence_ms <= self.candidate_silence_ms:
            raise ValueError("legacy_fallback_silence_ms must exceed candidate_silence_ms")


def decide_turn_gate(
    *,
    smart_end: bool,
    eou_end: bool,
    fallback_timeout: bool = False,
) -> TurnGateAction:
    """Return the V1 action without mutating session or conversation state."""

    if fallback_timeout:
        return TurnGateAction.FALLBACK_COMMIT
    if not (smart_end and eou_end):
        return TurnGateAction.CONTINUE
    return TurnGateAction.EARLY_COMMIT


@dataclass(frozen=True)
class TurnCandidate:
    utterance_id: str
    candidate_seq: int
    speech_epoch: int
    audio_watermark: int
    silence_ms: int

    def __post_init__(self) -> None:
        if not self.utterance_id.strip():
            raise ValueError("utterance_id is required")
        if self.candidate_seq <= 0:
            raise ValueError("candidate_seq must be positive")
        if self.speech_epoch < 0:
            raise ValueError("speech_epoch must not be negative")
        if self.audio_watermark <= 0:
            raise ValueError("audio_watermark must be positive")
        if self.silence_ms < 0:
            raise ValueError("silence_ms must not be negative")


class TurnCandidateTracker:
    """Track one logical utterance and reject stale asynchronous decisions."""

    def __init__(self, utterance_id: str) -> None:
        if not utterance_id.strip():
            raise ValueError("utterance_id is required")
        self.utterance_id = utterance_id
        self.speech_epoch = 0
        self.last_candidate_seq = 0
        self.last_audio_watermark = 0
        self.active_candidate: TurnCandidate | None = None
        self.committed = False

    def start_candidate(
        self,
        *,
        candidate_seq: int,
        audio_watermark: int,
        silence_ms: int,
    ) -> TurnCandidate:
        if self.committed:
            raise ValueError("utterance is already committed")
        if candidate_seq <= self.last_candidate_seq:
            raise ValueError("candidate_seq must increase monotonically")
        if audio_watermark < self.last_audio_watermark:
            raise ValueError("audio_watermark must not move backwards")
        candidate = TurnCandidate(
            utterance_id=self.utterance_id,
            candidate_seq=candidate_seq,
            speech_epoch=self.speech_epoch,
            audio_watermark=audio_watermark,
            silence_ms=silence_ms,
        )
        self.last_candidate_seq = candidate_seq
        self.last_audio_watermark = audio_watermark
        self.active_candidate = candidate
        return candidate

    def speech_resumed(self) -> None:
        if self.committed:
            return
        self.speech_epoch += 1
        self.active_candidate = None

    def is_current(self, candidate: TurnCandidate) -> bool:
        return (
            not self.committed
            and candidate.utterance_id == self.utterance_id
            and candidate.speech_epoch == self.speech_epoch
            and candidate == self.active_candidate
        )

    def commit(self, candidate: TurnCandidate) -> bool:
        if not self.is_current(candidate):
            return False
        self.committed = True
        self.active_candidate = None
        return True

    def fallback_commit(self) -> bool:
        if self.committed:
            return False
        self.committed = True
        self.active_candidate = None
        return True
