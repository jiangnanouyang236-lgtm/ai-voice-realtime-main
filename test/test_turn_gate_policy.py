import pytest

from gateway.turn_gate_policy import (
    TurnCandidateTracker,
    TurnGateAction,
    TurnGateConfig,
    decide_turn_gate,
)


@pytest.mark.parametrize(
    ("smart_end", "eou_end"),
    [(False, False), (False, True), (True, False)],
)
def test_any_false_continues_without_judge(smart_end, eou_end):
    assert (
        decide_turn_gate(smart_end=smart_end, eou_end=eou_end)
        is TurnGateAction.CONTINUE
    )


def test_double_true_is_eligible_for_early_commit():
    assert decide_turn_gate(smart_end=True, eou_end=True) is TurnGateAction.EARLY_COMMIT


def test_legacy_fallback_commits_regardless_of_model_state():
    assert (
        decide_turn_gate(
            smart_end=False,
            eou_end=False,
            fallback_timeout=True,
        )
        is TurnGateAction.FALLBACK_COMMIT
    )


def test_config_enforces_distinct_candidate_fallback_and_force_deadlines():
    assert TurnGateConfig() == TurnGateConfig(
        candidate_silence_ms=300,
        legacy_fallback_silence_ms=500,
    )
    with pytest.raises(ValueError, match="must exceed candidate"):
        TurnGateConfig(legacy_fallback_silence_ms=300)


def test_speech_resume_invalidates_inflight_candidate():
    tracker = TurnCandidateTracker("utt-1")
    candidate = tracker.start_candidate(
        candidate_seq=1,
        audio_watermark=16_000,
        silence_ms=300,
    )
    assert tracker.is_current(candidate)

    tracker.speech_resumed()

    assert not tracker.is_current(candidate)
    assert not tracker.commit(candidate)


def test_only_latest_candidate_can_commit():
    tracker = TurnCandidateTracker("utt-1")
    first = tracker.start_candidate(
        candidate_seq=1,
        audio_watermark=16_000,
        silence_ms=300,
    )
    tracker.speech_resumed()
    second = tracker.start_candidate(
        candidate_seq=2,
        audio_watermark=24_000,
        silence_ms=300,
    )

    assert not tracker.commit(first)
    assert tracker.commit(second)
    assert not tracker.fallback_commit()


def test_candidate_sequence_and_audio_watermark_are_monotonic():
    tracker = TurnCandidateTracker("utt-1")
    tracker.start_candidate(
        candidate_seq=1,
        audio_watermark=16_000,
        silence_ms=300,
    )
    with pytest.raises(ValueError, match="candidate_seq"):
        tracker.start_candidate(
            candidate_seq=1,
            audio_watermark=16_000,
            silence_ms=300,
        )
    with pytest.raises(ValueError, match="audio_watermark"):
        tracker.start_candidate(
            candidate_seq=2,
            audio_watermark=8_000,
            silence_ms=300,
        )
