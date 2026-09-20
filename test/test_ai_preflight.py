from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts.ai_preflight import (
    _endpoint_from_definition,
    build_environment,
    exit_code,
    main,
    render_text,
    run_preflight,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _scaffold(tmp_path: Path) -> Path:
    (tmp_path / ".ai").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_placeholder.py").write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
    (tmp_path / ".ai" / "harness.json").write_text(
        (REPO_ROOT / ".ai" / "harness.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    for relative in ("AGENTS.md", "docs/project-context-for-ai.md", "docs/ai-runtime-environment.md"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("test\n", encoding="utf-8")
    return tmp_path


def _write_runtime_context(root: Path) -> None:
    path = root / ".ai" / "runtime.local.md"
    path.write_text(
        """# Runtime
- Active profile: `lan`
- Machine role: test
- Last configuration inventory: `2026-08-08`
- Live connectivity: `UNKNOWN`

## Server registry

Test only.

## Access and authorization

No external writes.
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _lan_env() -> str:
    return "\n".join(
        [
            "CONFIG_DATABASE_URL=mysql://user:secret@10.0.0.2:3306/db",
            "LLM_BASE_URL=http://10.0.0.2:15101/v1",
            "LLM_API_KEY=secret-llm",
            "LLM_ROUTER_BASE_URL=http://10.0.0.2:15100/v1",
            "LLM_ROUTER_API_KEY=secret-router",
            "QWEN_ASR_BASE_URL=http://10.0.0.2:15110/v1",
            "QWEN_ASR_API_KEY=secret-asr",
            "QWEN3_TTS_CUSTOM_VOICE_WS_URL=ws://10.0.0.2:15120/v1/audio/speech/stream",
            "QWEN3_TTS_CUSTOM_VOICE_API_KEY=secret-tts",
            "LLM_VISION_GATEWAY_BASE_URL=http://10.0.0.2:15010",
            "LLM_VISION_GATEWAY_TOKEN=secret-vision",
            "VISION_INTERNAL_TOKEN=${LLM_VISION_GATEWAY_TOKEN}",
        ]
    )


def test_offline_profile_needs_no_private_environment(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    checks = run_preflight(root, "offline", process_env={})
    assert exit_code(checks) == 0
    assert "test-root" in render_text(checks)


def test_lan_profile_fails_when_private_env_is_missing(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    checks = run_preflight(root, "lan", process_env={})
    assert exit_code(checks) == 1
    assert any(item.id == "env-file:.env" and item.status == "FAIL" for item in checks)


def test_compose_profile_accepts_an_exact_variable_reference(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text(_lan_env(), encoding="utf-8")
    os.chmod(root / ".env", 0o600)
    checks = run_preflight(root, "compose-v3", process_env={})
    assert exit_code(checks) == 0
    assert any(item.id == "env:VISION_INTERNAL_TOKEN" and item.status == "PASS" for item in checks)


def test_compose_v4_profile_accepts_an_exact_variable_reference(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text(_lan_env(), encoding="utf-8")
    os.chmod(root / ".env", 0o600)
    checks = run_preflight(root, "compose-v4", process_env={})
    assert exit_code(checks) == 0
    assert any(item.id == "env:VISION_INTERNAL_TOKEN" and item.status == "PASS" for item in checks)


def test_lan_profile_requires_machine_local_context(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text(_lan_env(), encoding="utf-8")
    os.chmod(root / ".env", 0o600)
    (root / ".env.example").write_text(_lan_env(), encoding="utf-8")
    checks = run_preflight(root, "lan", process_env={})
    assert exit_code(checks) == 1
    assert any(item.id == "runtime-local" and item.required and item.status == "FAIL" for item in checks)


def test_secrets_are_never_rendered(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text(_lan_env(), encoding="utf-8")
    os.chmod(root / ".env", 0o600)
    (root / ".env.example").write_text(_lan_env(), encoding="utf-8")
    _write_runtime_context(root)
    checks = run_preflight(root, "lan", process_env={})
    output = render_text(checks)
    assert exit_code(checks) == 0
    assert "secret-llm" not in output
    assert "secret-vision" not in output
    assert "user:secret" not in output
    assert "10.0.0.2:15101" in output
    json_output = json.dumps([asdict(item) for item in checks])
    assert "secret-llm" not in json_output
    assert "secret-vision" not in json_output
    assert "user:secret" not in json_output


def test_secret_env_permissions_are_a_hard_gate(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text(_lan_env(), encoding="utf-8")
    os.chmod(root / ".env", 0o644)
    checks = run_preflight(root, "lan", process_env={})
    assert exit_code(checks) == 1
    assert any(item.id == "env-permission:.env" and item.status == "FAIL" for item in checks)


def test_vision_token_aliases_must_match(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    env_text = _lan_env().replace(
        "VISION_INTERNAL_TOKEN=${LLM_VISION_GATEWAY_TOKEN}", "VISION_INTERNAL_TOKEN=different"
    )
    (root / ".env").write_text(env_text, encoding="utf-8")
    os.chmod(root / ".env", 0o600)
    (root / ".env.example").write_text(env_text, encoding="utf-8")
    _write_runtime_context(root)
    checks = run_preflight(root, "lan", process_env={})
    assert exit_code(checks) == 1
    assert any(item.id == "vision-token-contract" and item.status == "FAIL" for item in checks)


def test_unresolved_variable_reference_is_not_accepted(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text(_lan_env().replace("LLM_API_KEY=secret-llm", "LLM_API_KEY=${MISSING_KEY}"), encoding="utf-8")
    os.chmod(root / ".env", 0o600)
    (root / ".env.example").write_text(_lan_env(), encoding="utf-8")
    _write_runtime_context(root)
    checks = run_preflight(root, "lan", process_env={})
    assert exit_code(checks) == 1
    assert any(item.id == "env:LLM_API_KEY" and item.status == "FAIL" for item in checks)


def test_json_manifest_is_valid() -> None:
    data = json.loads((REPO_ROOT / ".ai" / "harness.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["test_root"] == "test"
    assert data["evidence_policy"]["schema_version"] == "ai-voice-evidence/v1"
    assert "HYBRID_VERIFIED" in data["evidence_policy"]["levels"]
    assert "robot_mcp" in data["evidence_policy"]["capabilities"]


def test_hybrid_profile_is_exposed_by_cli_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert "hybrid" in capsys.readouterr().out


def test_compose_v4_profile_is_exposed_by_cli_help(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert "compose-v4" in capsys.readouterr().out


def test_runtime_context_must_be_private_and_structured(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text(_lan_env(), encoding="utf-8")
    os.chmod(root / ".env", 0o600)
    (root / ".env.example").write_text(_lan_env(), encoding="utf-8")
    runtime = root / ".ai" / "runtime.local.md"
    runtime.write_text("test\n", encoding="utf-8")
    os.chmod(runtime, 0o644)
    checks = run_preflight(root, "lan", process_env={})
    assert exit_code(checks) == 1
    assert any(item.id == "runtime-local-content" and item.status == "FAIL" for item in checks)
    assert any(item.id == "runtime-local-permission" and item.status == "FAIL" for item in checks)


def test_hardware_uses_rust_env_file_after_parent_environment(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".env").write_text("GATEWAY_URL=wss://root.example/ws\n", encoding="utf-8")
    (root / "rust_client").mkdir()
    (root / "rust_client" / ".env.local").write_text(
        "GATEWAY_URL=wss://runtime.example/ws\n", encoding="utf-8"
    )
    os.chmod(root / ".env", 0o600)
    os.chmod(root / "rust_client" / ".env.local", 0o600)
    manifest = json.loads((root / ".ai" / "harness.json").read_text(encoding="utf-8"))
    values, _ = build_environment(
        root, manifest, "hardware", {"GATEWAY_URL": "wss://parent.example/ws"}
    )
    assert values["GATEWAY_URL"] == "wss://runtime.example/ws"


def test_endpoint_rejects_wrong_scheme_unsafe_host_and_invalid_port() -> None:
    assert _endpoint_from_definition(
        {"url_env": "URL", "schemes": ["http", "https"]}, {"URL": "ftp://example.com:15101"}
    ) is None
    assert _endpoint_from_definition(
        {"host_env": "HOST", "port_env": "PORT"}, {"HOST": "user:password@broker", "PORT": "1883"}
    ) is None
    assert _endpoint_from_definition(
        {"host_env": "HOST", "port_env": "PORT"}, {"HOST": "broker", "PORT": "70000"}
    ) is None


def test_invalid_manifest_is_reported_as_configuration_exit_two(tmp_path: Path) -> None:
    root = _scaffold(tmp_path)
    (root / ".ai" / "harness.json").write_text('{"schema_version": 1}', encoding="utf-8")
    assert main(["--profile", "offline", "--root", str(root)]) == 2
