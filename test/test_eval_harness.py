from __future__ import annotations

from types import SimpleNamespace

from scripts import (
    eval_acceptance,
    eval_bot_utils_matrix,
    eval_runner,
    eval_vision_e2e,
    validate_eval_gold,
)


def _tool_message(name: str, arguments: str):
    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(content="", tool_calls=[call])


def _client_with_message(message):
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
    completions = SimpleNamespace(create=lambda **kwargs: response)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_9b_eval_uses_full_candidate_set_and_checks_exact_args():
    message = _tool_message(
        "robot_remote.move_robot",
        '{"action":"forward"}',
    )
    result = eval_runner.run_9b_tool(
        _client_with_message(message),
        "case.jsonl",
        {
            "id": "tool-001",
            "text": "向前走",
            "expected_tool": "move_robot",
            "expected_args": {"action": "forward"},
        },
    )

    assert result.ok
    assert result.tool_ok
    assert result.args_ok
    assert result.case_id == "tool-001"
    assert len(result.candidate_tools) == len(eval_runner.EVAL_TOOL_SCHEMAS)

    summary = eval_runner.summarize_9b_results([result])
    assert summary["results"][0]["case_id"] == "tool-001"
    assert summary["results"][0]["actual_tool"] == "move_robot"
    assert summary["results"][0]["actual_args"] == {"action": "forward"}


def test_9b_eval_rejects_wrong_args_and_any_gold():
    wrong_args = eval_runner._validate_tool_prediction(
        expected_tool="move_robot",
        expected_args={"action": "forward", "steps": 2},
        actual_tool="move_robot",
        actual_args={"action": "forward", "steps": 1},
        has_tool_calls=True,
    )
    any_gold = eval_runner._validate_tool_prediction(
        expected_tool="any",
        expected_args={},
        actual_tool="move_robot",
        actual_args={},
        has_tool_calls=True,
    )

    assert wrong_args[0] is True
    assert wrong_args[1] is False
    assert "args_mismatch" in wrong_args[2][0]
    assert any_gold == (False, False, ["invalid_gold: expected_tool=any is forbidden"])


def test_time_fidelity_rejects_changed_weekday_and_holiday():
    facts = {
        "date_padded": "2026年08月09日",
        "date_unpadded": "2026年8月9日",
        "weekday": "周日",
        "holiday": "是，今天为：法定节假日",
    }

    errors = eval_bot_utils_matrix.validate_time_fidelity(
        "现在是 2026年8月9日，周一，而且不是法定节假日。",
        facts,
    )

    assert "missing_or_wrong_weekday" in errors
    assert "contradictory_holiday" in errors


def test_time_fidelity_accepts_authoritative_result():
    facts = {
        "date_padded": "2026年08月09日",
        "date_unpadded": "2026年8月9日",
        "weekday": "周日",
        "holiday": "是，今天为：法定节假日",
    }

    assert not eval_bot_utils_matrix.validate_time_fidelity(
        "现在是 2026年08月09日 16:37:52，周日。今天是法定节假日。",
        facts,
    )


def test_time_fidelity_rejects_tool_protocol_leakage():
    facts = {
        "date_padded": "2026年08月09日",
        "date_unpadded": "2026年8月9日",
        "weekday": "周日",
        "holiday": "是，今天为：法定节假日",
    }

    errors = eval_bot_utils_matrix.validate_time_fidelity(
        '现在是 2026年08月09日，周日，法定节假日。{"name":"tool"}',
        facts,
    )

    assert "tool_protocol_leakage" in errors


def test_vision_eval_requires_supported_real_image(tmp_path):
    unsupported = tmp_path / "fixture.txt"
    unsupported.write_text("not an image")

    try:
        eval_vision_e2e.load_image_data_url(unsupported)
    except ValueError as exc:
        assert "JPEG、PNG 或 WebP" in str(exc)
    else:
        raise AssertionError("unsupported fixture must be rejected")


def test_independent_gold_is_owner_approved_and_includes_real_vision_fixture():
    report = validate_eval_gold.validate_gold()

    assert report["valid"] is True
    assert report["acceptance_ready"] is True
    assert report["review_status"] == "owner_approved"
    assert report["router_cases"] == 94
    assert report["tool_cases"] == 21
    assert report["vision_cases"] == 1


def test_dance_gold_has_no_unsupported_style_or_duration():
    rows = validate_eval_gold._load_jsonl(
        validate_eval_gold.GOLD_DIR / "9b_tool.jsonl", []
    )
    dance = next(row for row in rows if row["expected_tool"] == "dance")

    assert dance["text"] == "跳个舞"
    assert dance["expected_args"] == {}


def test_router_gold_includes_multiturn_and_high_risk_boundaries():
    rows = validate_eval_gold._load_jsonl(
        validate_eval_gold.GOLD_DIR / "4b_router.jsonl", []
    )
    scenarios = {row.get("scenario") for row in rows}

    assert sum(bool(row.get("history")) for row in rows) >= 8
    assert {
        "chat_capability",
        "chat_negated_action",
        "chat_unsupported_home",
        "complex_same_tool_sequence",
        "multiturn_robot_confirmation",
        "multiturn_task_reference",
    } <= scenarios
