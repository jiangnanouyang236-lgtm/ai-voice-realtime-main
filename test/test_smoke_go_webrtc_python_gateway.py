from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import smoke_go_webrtc_python_gateway as smoke


def _args(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "python_deps": Path("/missing/python-deps"),
        "opus_lib_dir": Path("/missing/opus-lib"),
        "require_robot_secret": True,
        "robot_id": "test_01",
        "go_addr": "127.0.0.1:8282",
        "gateway_ws_url": "ws://127.0.0.1:7860/ws",
        "bot_id": "xiaowen",
        "packets_path": Path("/private/tmp/packets.json"),
        "sample": Path("/private/tmp/sample.wav"),
        "client": "rust",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_load_private_robot_identity_ignores_unrelated_keys(tmp_path: Path) -> None:
    private_env = tmp_path / ".env.local"
    private_env.write_text(
        'ROBOT_ID="test_01"\nROBOT_SECRET="secret-value"\nMQTT_PASSWORD="ignored"\n',
        encoding="utf-8",
    )

    assert smoke.load_private_robot_identity(private_env) == {
        "ROBOT_ID": "test_01",
        "ROBOT_SECRET": "secret-value",
    }


def test_load_env_rejects_private_identity_for_another_robot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    private_dir = tmp_path / "rust_client"
    private_dir.mkdir()
    (private_dir / ".env.local").write_text(
        'ROBOT_ID="companion_01"\nROBOT_SECRET="secret-value"\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="does not match"):
        smoke.load_env(_args())


def test_load_env_requires_nonempty_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.delenv("ROBOT_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="needs ROBOT_SECRET"):
        smoke.load_env(_args())


def test_service_specs_use_isolated_core_ports() -> None:
    specs = smoke.service_specs(
        "127.0.0.1:58282",
        {
            "STT_GRPC_SERVER_PORT": "55054",
            "LLM_GRPC_SERVER_PORT": "55053",
            "TTS_GRPC_SERVER_PORT": "55052",
            "GATEWAY_BIND_PORT": "57860",
        },
    )

    assert {name: port for name, _cmd, _cwd, port, _timeout in specs} == {
        "mcp_robot": 5003,
        "mcp_singing": 5005,
        "mcp_utils": 5004,
        "stt": 55054,
        "llm": 55053,
        "tts": 55052,
        "python_gateway": 57860,
        "go_gateway": 58282,
    }


def test_barge_in_fixture_generation_is_opt_in(tmp_path: Path) -> None:
    assert smoke.generate_barge_in_wav({}, tmp_path / "barge.wav") is None
