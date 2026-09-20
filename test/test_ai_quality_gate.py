from __future__ import annotations

import json
import subprocess
import sys

from scripts import ai_quality_gate


def gate_ids(paths: list[str], mode: str = "changed") -> list[str]:
    gates, _manual = ai_quality_gate.select_gates(paths, mode)
    return [gate.gate_id for gate in gates]


def test_docs_only_uses_lightweight_repository_gates() -> None:
    assert gate_ids(["docs/example.md"]) == ["diff_check", "repo_hygiene"]


def test_python_go_and_rust_changes_select_existing_full_suites() -> None:
    selected = gate_ids(
        [
            "gateway/gateway_server.py",
            "go_voice_gateway/server.go",
            "rust_client/src/transport/webrtc.rs",
        ]
    )

    assert "python_full" in selected
    assert "go_full" in selected
    assert "rust_native" in selected


def test_llm_change_adds_eval_and_explicit_live_followup() -> None:
    gates, manual = ai_quality_gate.select_gates(["llm/tool_router.py"], "changed")

    assert "eval_gold" in [gate.gate_id for gate in gates]
    assert any("eval_acceptance" in item for item in manual)
    assert any("MQTT" in item for item in manual)


def test_release_mode_keeps_hardware_deferred_and_does_not_auto_run_live() -> None:
    gates, manual = ai_quality_gate.select_gates([], "release")
    selected = [gate.gate_id for gate in gates]

    assert selected == [
        "diff_check",
        "repo_hygiene",
        "python_full",
        "go_full",
        "rust_native",
        "eval_gold",
        "compose_static",
    ]
    assert all("live" not in gate_id for gate_id in selected)
    assert any("DEFERRED" in item for item in manual)


def test_dry_run_prints_compact_json(capsys) -> None:
    result = ai_quality_gate.main(
        ["--mode", "changed", "--paths", "docs/example.md", "--dry-run"]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gates"] == ["diff_check", "repo_hygiene"]
    assert payload["output_policy"]["failure_excerpt_lines"] == 40


def test_failure_excerpt_is_bounded() -> None:
    output = "\n".join(f"line-{index}-" + "x" * 200 for index in range(100))

    excerpt = ai_quality_gate._failure_excerpt(output)

    assert len(excerpt) <= ai_quality_gate.FAILURE_EXCERPT_CHARS
    assert len(excerpt.splitlines()) <= ai_quality_gate.FAILURE_EXCERPT_LINES
    assert "line-99" in excerpt


def test_output_redaction_covers_values_and_auth_fields() -> None:
    output = (
        'Authorization: Bearer abc.def.ghi credential="turn-password" '
        "database=mysql://user:db-password@example.test/db"
    )

    redacted = ai_quality_gate._redact_output(
        output, ["turn-password", "mysql://user:db-password@example.test/db"]
    )

    assert "abc.def.ghi" not in redacted
    assert "turn-password" not in redacted
    assert "db-password" not in redacted
    assert redacted.count("<redacted>") >= 3


def test_direct_script_entrypoint_can_build_dry_run_plan() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/ai_quality_gate.py", "--mode", "changed", "--dry-run"],
        cwd=ai_quality_gate.ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["schema_version"] == "ai-quality-gate/v1"
