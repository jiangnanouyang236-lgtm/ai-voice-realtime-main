from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from scripts.validate_evidence_report import validate_report


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / ".ai" / "harness.json").read_text(encoding="utf-8"))


def _base_report() -> dict:
    return {
        "schema_version": "ai-voice-evidence/v1",
        "objective": "验证一个有边界的仓库改动",
        "commit": "c309c5ac",
        "capability": "repository",
        "claimed_level": "IMPLEMENTED",
        "endpoint_scope": "localhost",
        "observed_endpoints": [],
        "authorization": {
            "connectivity": False,
            "external_write": False,
            "hardware_action": False,
            "reference": "",
        },
        "assertions": {
            "runtime_snapshot": False,
            "same_trace": False,
            "trace_ids": [],
            "bot_bindings": [],
        },
        "evidence": [
            {
                "id": "unit",
                "status": "PASS",
                "kind": "unit",
                "component": "repository",
                "source": "command",
                "observed_at": "2026-08-09T00:00:00+08:00",
                "summary": "定向单元测试通过",
            }
        ],
        "unknowns": ["未运行真实服务"],
    }


def _validate(report: dict, path: Path) -> list[str]:
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return validate_report(report, MANIFEST, report_path=path, root=ROOT)


def test_implemented_report_accepts_unit_evidence(tmp_path: Path) -> None:
    assert _validate(_base_report(), tmp_path / "report.json") == []


def test_implemented_report_remains_compatible_without_endpoint_inventory(tmp_path: Path) -> None:
    report = _base_report()
    report.pop("observed_endpoints")
    assert _validate(report, tmp_path / "report.json") == []


def test_localhost_cannot_be_claimed_as_live(tmp_path: Path) -> None:
    report = _base_report()
    report["claimed_level"] = "LIVE_VERIFIED"
    report["authorization"]["connectivity"] = True
    report["authorization"]["reference"] = "owner-authorized-test"
    report["assertions"]["runtime_snapshot"] = True
    report["evidence"][0]["kind"] = "live_service"
    problems = _validate(report, tmp_path / "report.json")
    assert any("endpoint_scope=localhost" in item for item in problems)


def test_robot_local_claim_requires_trace_multi_bot_components_and_hashes(tmp_path: Path) -> None:
    report = _base_report()
    report.update(capability="robot_mcp", claimed_level="LOCAL_VERIFIED")
    report["assertions"].update(runtime_snapshot=True, same_trace=False)
    report["evidence"][0].update(kind="local_integration", component="llm")
    problems = _validate(report, tmp_path / "report.json")
    assert any("assertions.same_trace" in item for item in problems)
    assert any("assertions.trace_ids" in item for item in problems)
    assert any("assertions.bot_bindings" in item for item in problems)
    assert any("missing PASS components" in item for item in problems)
    assert any("artifact" in item for item in problems)


def test_robot_live_claim_requires_full_chain_authorization_and_multi_bot(tmp_path: Path) -> None:
    report = _base_report()
    report.update(
        capability="robot_mcp",
        claimed_level="LIVE_VERIFIED",
        endpoint_scope="external",
    )
    report["authorization"]["connectivity"] = True
    report["authorization"]["reference"] = "owner-authorized-test"
    report["assertions"].update(
        runtime_snapshot=True,
        same_trace=False,
        trace_ids=[],
        bot_bindings=["bot-a"],
    )
    report["observed_endpoints"] = [
        {"component": component, "scope": "external"}
        for component in ("runtime_snapshot", "gateway", "router", "llm", "mcp", "mqtt_broker")
    ]
    report["evidence"][0].update(kind="live_service", component="llm")
    problems = _validate(report, tmp_path / "report.json")
    assert any("authorization.external_write=true" in item for item in problems)
    assert any("assertions.same_trace" in item for item in problems)
    assert any("assertions.trace_ids" in item for item in problems)
    assert any("assertions.bot_bindings" in item for item in problems)
    assert any("missing PASS components" in item for item in problems)


def test_previous_local_mqtt_pattern_cannot_be_reported_as_robot_live(tmp_path: Path) -> None:
    report = _base_report()
    report.update(
        capability="robot_mcp",
        claimed_level="LIVE_VERIFIED",
        endpoint_scope="localhost",
    )
    report["authorization"].update(
        connectivity=True,
        external_write=True,
        reference="authorized-local-fixture",
    )
    report["assertions"].update(
        runtime_snapshot=False,
        same_trace=True,
        trace_ids=["session-id-used-as-trace"],
        bot_bindings=["bot-a", "bot-b", "bot-c"],
    )
    report["evidence"][0].update(
        kind="local_integration",
        component="mqtt_broker",
        summary="三 Bot 向 localhost broker 发布成功",
    )

    problems = _validate(report, tmp_path / "report.json")

    assert any("missing PASS evidence kinds" in item for item in problems)
    assert any("endpoint_scope=localhost" in item for item in problems)
    assert any("runtime_snapshot=true" in item for item in problems)
    assert any("at least 2 unique trace IDs" in item for item in problems)
    assert any("must declare trace_ids" in item for item in problems)


def test_robot_local_claim_requires_component_level_trace_and_bot_linkage(tmp_path: Path) -> None:
    artifact = tmp_path / "robot-local.log"
    artifact.write_text("redacted robot local evidence\n", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    environment_artifact = tmp_path / "environment.json"
    environment_artifact.write_text(
        json.dumps(
            {
                "schema_version": "ai-voice-environment/v1",
                "captured_at": "2026-08-09T00:00:00+08:00",
                "commit": "c309c5ac",
                "dirty": False,
                "profile": "local",
                "capability": "robot_mcp",
                "claimed_level": "LOCAL_VERIFIED",
                "endpoints": [
                    {"component": "database", "scope": "localhost"},
                    {"component": "mqtt", "scope": "localhost"},
                ],
            }
        ),
        encoding="utf-8",
    )
    report = _base_report()
    report.update(capability="robot_mcp", claimed_level="LOCAL_VERIFIED")
    report["assertions"].update(
        runtime_snapshot=True,
        same_trace=True,
        trace_ids=["trace-a", "trace-b"],
        bot_bindings=["bot-a", "bot-b"],
    )
    report["observed_endpoints"] = [
        {"component": component, "scope": "localhost"}
        for component in ("database", "mqtt")
    ]
    report["environment_artifact"] = {
        "artifact": str(environment_artifact),
        "sha256": hashlib.sha256(environment_artifact.read_bytes()).hexdigest(),
    }
    report["evidence"] = [
        {
            "id": component,
            "status": "PASS",
            "kind": "local_integration",
            "component": component,
            "source": "artifact",
            "observed_at": "2026-08-09T00:00:00+08:00",
            "summary": f"{component} observed both turns and bots",
            "trace_ids": ["trace-a", "trace-b"],
            "bot_bindings": ["bot-a", "bot-b"],
            "artifact": str(artifact),
            "sha256": digest,
        }
        for component in ("runtime_snapshot", "llm", "mcp", "mqtt_broker")
    ]

    assert _validate(report, tmp_path / "report.json") == []

    report["evidence"][-1]["trace_ids"] = ["trace-a"]
    problems = _validate(report, tmp_path / "bad-report.json")
    assert any("present in every required component" in item for item in problems)
    assert any("share at least 2 trace IDs" in item for item in problems)

    report["observed_endpoints"][0]["scope"] = "lan"
    problems = _validate(report, tmp_path / "bad-environment-report.json")
    assert any("exactly match environment_artifact" in item for item in problems)


def test_local_claim_rejects_hidden_lan_dependency(tmp_path: Path) -> None:
    report = _base_report()
    report.update(claimed_level="LOCAL_VERIFIED")
    report["observed_endpoints"] = [
        {"component": "llm_process", "scope": "localhost"},
        {"component": "config_database", "scope": "lan"},
    ]
    report["evidence"][0]["kind"] = "local_integration"

    problems = _validate(report, tmp_path / "report.json")

    assert any("must be within ['localhost']" in item and "lan" in item for item in problems)


def test_robot_hybrid_claim_accepts_local_processes_with_authorized_remote_dependencies(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "robot-hybrid.log"
    artifact.write_text("redacted robot hybrid evidence\n", encoding="utf-8")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    environment_artifact = tmp_path / "environment.json"
    environment_artifact.write_text(
        json.dumps(
            {
                "schema_version": "ai-voice-environment/v1",
                "captured_at": "2026-08-09T00:00:00+08:00",
                "commit": "c309c5ac",
                "dirty": False,
                "profile": "hybrid",
                "capability": "robot_mcp",
                "claimed_level": "HYBRID_VERIFIED",
                "endpoints": [
                    {"component": "database", "scope": "lan"},
                    {"component": "mqtt", "scope": "external"},
                ],
            }
        ),
        encoding="utf-8",
    )
    report = _base_report()
    report.update(
        capability="robot_mcp",
        claimed_level="HYBRID_VERIFIED",
        endpoint_scope="external",
    )
    report["authorization"].update(
        connectivity=True,
        external_write=True,
        reference="owner-authorized-test01-hybrid-run",
    )
    report["assertions"].update(
        runtime_snapshot=True,
        same_trace=True,
        trace_ids=["trace-greet", "trace-cheer"],
        bot_bindings=["test_01:wzk-test-bot"],
    )
    report["observed_endpoints"] = [
        {"component": "database", "scope": "lan"},
        {"component": "mqtt", "scope": "external"},
    ]
    report["environment_artifact"] = {
        "artifact": str(environment_artifact),
        "sha256": hashlib.sha256(environment_artifact.read_bytes()).hexdigest(),
    }
    report["evidence"] = [
        {
            "id": component,
            "status": "PASS",
            "kind": "hybrid_integration",
            "component": component,
            "source": "artifact",
            "observed_at": "2026-08-09T00:00:00+08:00",
            "summary": f"{component} observed both test_01 turns",
            "trace_ids": ["trace-greet", "trace-cheer"],
            "bot_bindings": ["test_01:wzk-test-bot"],
            "artifact": str(artifact),
            "sha256": digest,
        }
        for component in ("runtime_snapshot", "llm", "mcp", "mqtt_broker")
    ]

    assert _validate(report, tmp_path / "report.json") == []


def test_robot_hybrid_claim_rejects_missing_remote_scope(tmp_path: Path) -> None:
    report = _base_report()
    report.update(claimed_level="HYBRID_VERIFIED", endpoint_scope="localhost")
    report["authorization"].update(connectivity=True, reference="owner-authorized-test")
    report["observed_endpoints"] = [
        {"component": "llm_process", "scope": "localhost"},
    ]
    report["evidence"][0]["kind"] = "hybrid_integration"

    problems = _validate(report, tmp_path / "report.json")

    assert any("requires at least one observed endpoint scope" in item for item in problems)
    assert any("cannot use endpoint_scope=localhost" in item for item in problems)


def test_live_evidence_must_bind_existing_artifact_hash(tmp_path: Path) -> None:
    artifact = tmp_path / "live.log"
    artifact.write_text("live evidence\n", encoding="utf-8")
    report = _base_report()
    report.update(claimed_level="LIVE_VERIFIED", endpoint_scope="lan")
    report["observed_endpoints"] = [{"component": "runtime_snapshot", "scope": "lan"}]
    report["authorization"]["connectivity"] = True
    report["authorization"]["reference"] = "owner-authorized-test"
    report["assertions"]["runtime_snapshot"] = True
    report["evidence"] = [
        {
            "id": "runtime",
            "status": "PASS",
            "kind": "live_service",
            "component": "runtime_snapshot",
            "source": "runtime_status",
            "observed_at": "2026-08-09T00:00:00+08:00",
            "summary": "运行态快照已保存",
            "artifact": str(artifact),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    ]
    assert _validate(report, tmp_path / "report.json") == []

    report["evidence"][0]["sha256"] = "0" * 64
    problems = _validate(report, tmp_path / "bad-report.json")
    assert any("does not match artifact" in item for item in problems)


def test_failed_or_not_run_evidence_cannot_satisfy_claim(tmp_path: Path) -> None:
    report = _base_report()
    report["evidence"][0]["status"] = "NOT_RUN"
    problems = _validate(report, tmp_path / "report.json")
    assert any("requires at least one PASS" in item for item in problems)


def test_report_requires_existing_commit_and_timezone_timestamp(tmp_path: Path) -> None:
    report = _base_report()
    report["commit"] = "deadbee"
    report["evidence"][0]["observed_at"] = "2026-08-09T00:00:00"
    problems = _validate(report, tmp_path / "report.json")
    assert any("commit does not exist" in item for item in problems)
    assert any("with timezone" in item for item in problems)


def test_unit_component_cannot_satisfy_live_component_requirement(tmp_path: Path) -> None:
    artifact = tmp_path / "live.log"
    artifact.write_text("live evidence\n", encoding="utf-8")
    report = _base_report()
    report.update(claimed_level="LIVE_VERIFIED", endpoint_scope="lan")
    report["observed_endpoints"] = [{"component": "runtime_snapshot", "scope": "lan"}]
    report["authorization"].update(connectivity=True, reference="owner-authorized-test")
    report["assertions"]["runtime_snapshot"] = True
    report["evidence"] = [
        {
            "id": "runtime-unit",
            "status": "PASS",
            "kind": "unit",
            "component": "runtime_snapshot",
            "source": "command",
            "observed_at": "2026-08-09T00:00:00+08:00",
            "summary": "只有单元测试",
        },
        {
            "id": "other-live",
            "status": "PASS",
            "kind": "live_service",
            "component": "repository",
            "source": "log",
            "observed_at": "2026-08-09T00:00:00+08:00",
            "summary": "无关的 live 证据",
            "artifact": str(artifact),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        },
    ]
    problems = _validate(report, tmp_path / "report.json")
    assert any("runtime_snapshot" in item and "missing PASS components" in item for item in problems)


def test_example_report_is_valid() -> None:
    path = ROOT / ".ai" / "evidence-report.example.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    assert validate_report(report, MANIFEST, report_path=path, root=ROOT) == []
