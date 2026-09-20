from __future__ import annotations

from scripts import run_natural_barge_in_matrix as matrix


def test_matrix_cases_use_distinct_tts_base_user_profiles() -> None:
    ids = [case["id"] for case in matrix.CASES]
    profiles = [case["tts_profile_id"] for case in matrix.CASES]

    assert len(ids) == len(set(ids)) == 4
    assert len(profiles) == len(set(profiles)) == 4
    assert "wzk-base" not in profiles
    assert all(case["expected_fragments"] for case in matrix.CASES)


def test_matrix_cases_are_complete_user_expressions() -> None:
    assert all(len(str(case["text"])) >= 12 for case in matrix.CASES)
    assert all(str(case["text"])[-1] in "。！？" for case in matrix.CASES)
