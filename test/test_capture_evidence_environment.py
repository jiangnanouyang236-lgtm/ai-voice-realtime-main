from pathlib import Path
import subprocess
import sys

import pytest

from scripts.capture_evidence_environment import (
    _safe_output_path,
    build_inventory,
    classify_host_scope,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", "localhost"),
        ("127.0.0.1", "localhost"),
        ("::1", "localhost"),
        ("10.10.6.121", "lan"),
        ("192.168.1.10", "lan"),
        ("robot.local", "lan"),
        ("mqtt.example.net", "external"),
        ("8.8.8.8", "external"),
    ],
)
def test_classify_host_scope(host: str, expected: str) -> None:
    assert classify_host_scope(host) == expected


def test_robot_local_inventory_is_redacted_and_exposes_mixed_scope() -> None:
    inventory = build_inventory(
        root=ROOT,
        profile="local",
        capability="robot_mcp",
        claimed_level="LOCAL_VERIFIED",
        process_env={
            "CONFIG_DATABASE_URL": "mysql://user:secret@10.10.6.121:15501/config",
            "ROBOT_MQTT_HOST": "127.0.0.1",
            "ROBOT_MQTT_PORT": "1883",
        },
        allow_dirty=True,
    )

    assert inventory["endpoints"] == [
        {"component": "database", "scope": "lan"},
        {"component": "mqtt", "scope": "localhost"},
    ]
    rendered = str(inventory)
    assert "secret" not in rendered
    assert "10.10.6.121" not in rendered
    assert isinstance(inventory["dirty"], bool)


def test_robot_hybrid_inventory_captures_test_database_and_external_mqtt() -> None:
    inventory = build_inventory(
        root=ROOT,
        profile="hybrid",
        capability="robot_mcp",
        claimed_level="HYBRID_VERIFIED",
        process_env={
            "CONFIG_DATABASE_URL": "mysql://user:secret@10.10.6.121:15501/config",
            "ROBOT_MQTT_HOST": "mqtt.example.net",
            "ROBOT_MQTT_PORT": "1883",
        },
        allow_dirty=True,
    )

    assert inventory["endpoints"] == [
        {"component": "database", "scope": "lan"},
        {"component": "mqtt", "scope": "external"},
    ]


def test_output_must_stay_in_generated_artifact_directories(tmp_path: Path) -> None:
    assert _safe_output_path(ROOT, Path("reports/environment.json")) == (
        ROOT / "reports/environment.json"
    ).resolve()
    with pytest.raises(ValueError, match="reports/ or tmp"):
        _safe_output_path(ROOT, tmp_path / "environment.json")


def test_direct_cli_entrypoint_is_importable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/capture_evidence_environment.py", "--help"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
