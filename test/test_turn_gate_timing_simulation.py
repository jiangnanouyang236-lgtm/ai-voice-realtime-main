import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "turn-gate"
    / "scripts"
    / "simulate_turn_gate_timing.py"
)
SPEC = importlib.util.spec_from_file_location("simulate_turn_gate_timing", SCRIPT_PATH)
sim = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = sim
SPEC.loader.exec_module(sim)


def profile(**overrides):
    values = {
        "candidate_silence_ms": 300,
        "min_commit_silence_ms": 600,
        "active_deadline_ms": 300,
        "failure_fallback_ms": 1000,
        "hard_timeout_ms": 1600,
        "asr_ms": 150,
        "smart_ms": 50,
        "eou_ms": 20,
        "control_ms": 30,
    }
    values.update(overrides)
    return sim.TimingProfile(**values)


def decide(*, truth="CONTINUE", resume=800, both=True, timing=None, available=True):
    return sim.simulate_case(
        case_id="case-1",
        ground_truth=truth,
        resume_ms=resume,
        both_end=both,
        profile=timing or profile(),
        models_available=available,
    )


def test_online_target_budget_holds_double_true_until_600ms_stable_silence():
    timing = profile()
    assert timing.semantic_path_ms == 200
    assert timing.active_deadline_met
    assert timing.early_commit_ms == 600
    assert decide(truth="END", timing=timing).action == "early_commit"


def test_resume_before_decision_cancels_candidate_safely():
    result = decide(resume=400)
    assert result.outcome == "safe_continue"


def test_double_true_before_resume_is_a_false_end():
    result = decide(resume=800)
    assert result.commit_ms == 600
    assert result.outcome == "false_end"


def test_600ms_guard_protects_resume_between_old_and_new_commit_points():
    old_result = decide(resume=550, timing=profile(min_commit_silence_ms=500))
    guarded_result = decide(resume=550)

    assert old_result.commit_ms == 500
    assert old_result.outcome == "false_end"
    assert guarded_result.commit_ms == 600
    assert guarded_result.outcome == "safe_continue"


def test_equal_resume_and_commit_is_reported_as_boundary_risk():
    assert decide(resume=600).outcome == "boundary_risk"


def test_slow_semantic_result_does_not_add_a_second_wait():
    timing = profile(asr_ms=240, active_deadline_ms=400)
    assert timing.semantic_path_ms == 290
    assert timing.early_commit_ms == 600


def test_result_after_minimum_silence_commits_when_result_arrives():
    timing = profile(asr_ms=350, active_deadline_ms=500)
    assert timing.semantic_path_ms == 400
    assert timing.early_commit_ms == 700


def test_any_false_waits_for_hard_timeout_when_models_are_healthy():
    assert decide(resume=1000, both=False).outcome == "safe_continue"
    assert decide(resume=1700, both=False).outcome == "false_end"


@pytest.mark.parametrize("available", [False, True])
def test_unavailable_or_over_deadline_uses_failure_fallback(available):
    timing = profile(asr_ms=350) if available else profile()
    result = decide(resume=1200, timing=timing, available=available)
    assert result.action == "failure_fallback"
    assert result.commit_ms == 1000
    assert result.outcome == "false_end"


def test_invalid_timeout_order_is_rejected():
    with pytest.raises(ValueError, match="hard timeout"):
        profile(failure_fallback_ms=1000, hard_timeout_ms=900)


def test_fixed_timeout_baseline_reports_false_end_and_boundary_separately():
    result = sim.simulate_fixed_timeout_baseline(
        cases={
            "short": {"ground_truth": "CONTINUE", "trailing_silence_ms": 400},
            "equal": {"ground_truth": "CONTINUE", "trailing_silence_ms": 500},
            "long": {"ground_truth": "CONTINUE", "trailing_silence_ms": 800},
            "end": {"ground_truth": "END", "trailing_silence_ms": 1000},
        },
        timeout_ms=500,
    )
    assert result["summary"]["false_end"] == 1
    assert result["summary"]["boundary_risk"] == 1
    assert result["summary"]["end_latency_ms"]["mean"] == 500


def test_single_duplex_hardware_candidate_profile_matches_simulation_baseline():
    path = (
        Path(__file__).resolve().parents[1]
        / "turn-gate"
        / "configs"
        / "single-duplex-v1-test.json"
    )
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config["status"] == "PROVISIONAL"
    assert config["timing_ms"] == {
        "candidate_silence": 300,
        "min_commit_silence": 600,
        "active_deadline": 300,
        "failure_fallback": 1000,
        "ambiguous_hard_timeout": 1600,
    }
    assert config["thresholds"] == {
        "smart_turn": 0.5,
        "livekit_eou": 0.1,
    }
    assert config["runtime_support"]["failure_fallback"] == "IMPLEMENTED"
    assert config["runtime_support"]["ambiguous_hard_timeout"] == "IMPLEMENTED"
