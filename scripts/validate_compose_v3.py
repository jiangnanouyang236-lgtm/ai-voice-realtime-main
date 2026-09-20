#!/usr/bin/env python3
"""Validate v3 Docker Compose layers without starting containers."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / "deploy/env.v3.compose.example"


class ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ComposeCase:
    name: str
    files: tuple[str, ...]
    expected_services: tuple[str, ...]
    profiles: tuple[str, ...] = ()


CASES = (
    ComposeCase(
        name="root-core",
        files=("docker-compose.v3.yml",),
        expected_services=("admin", "go-gateway", "llm", "python-gateway", "stt", "tts"),
    ),
    ComposeCase(
        name="root-with-mcp",
        files=("docker-compose.v3.yml",),
        expected_services=(
            "admin",
            "go-gateway",
            "llm",
            "mcp-robot",
            "mcp-singing",
            "mcp-utils",
            "python-gateway",
            "stt",
            "tts",
        ),
        profiles=("mcp",),
    ),
    ComposeCase(
        name="split-core",
        files=("deploy/compose/business.yml", "deploy/compose/go-gateway.host.yml"),
        expected_services=("admin", "go-gateway", "llm", "python-gateway", "stt", "tts"),
    ),
    ComposeCase(
        name="business-only",
        files=("deploy/compose/business.yml",),
        expected_services=("admin", "llm", "python-gateway", "stt", "tts"),
    ),
    ComposeCase(
        name="go-gateway-host",
        files=("deploy/compose/go-gateway.host.yml",),
        expected_services=("go-gateway",),
    ),
    ComposeCase(
        name="mcp-profile",
        files=("deploy/compose/mcp.yml",),
        expected_services=("mcp-robot", "mcp-singing", "mcp-utils"),
        profiles=("mcp",),
    ),
)


def run_compose_config(case: ComposeCase, env_file: Path) -> dict[str, Any]:
    cmd = ["docker", "compose", "--env-file", str(env_file)]
    for file_name in case.files:
        cmd.extend(("-f", str(ROOT / file_name)))
    for profile in case.profiles:
        cmd.extend(("--profile", profile))
    cmd.extend(("config", "--format", "json"))

    try:
        completed = subprocess.run(
            cmd,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValidationError("docker compose is required but Docker was not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip()
        raise ValidationError(f"{case.name}: docker compose config failed: {detail}") from exc

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{case.name}: docker compose returned invalid JSON") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def service_ports(service: dict[str, Any]) -> list[dict[str, Any]]:
    ports = service.get("ports")
    if not ports:
        return []
    require(isinstance(ports, list), "compose service ports must render as a list")
    return ports


def assert_expected_services(case: ComposeCase, config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = config.get("services")
    require(isinstance(services, dict), f"{case.name}: services block is missing")

    missing = sorted(set(case.expected_services) - set(services))
    require(not missing, f"{case.name}: missing services: {', '.join(missing)}")

    unexpected_mcp = sorted(name for name in services if name.startswith("mcp-") and "mcp" not in case.profiles)
    require(not unexpected_mcp, f"{case.name}: MCP services should be profile-gated: {', '.join(unexpected_mcp)}")

    return services


def assert_go_gateway(case: ComposeCase, services: dict[str, dict[str, Any]]) -> None:
    service = services.get("go-gateway")
    if service is None:
        return

    require(service.get("network_mode") == "host", f"{case.name}: go-gateway must use host network")
    require(not service_ports(service), f"{case.name}: go-gateway host-network service must not publish ports")

    env = service.get("environment") or {}
    build = service.get("build") or {}
    require(
        service.get("image") == "ai-voice-go-gateway:ubuntu22-amd64",
        f"{case.name}: unexpected Go Gateway image",
    )
    require(not build, f"{case.name}: go-gateway compose must not invoke docker compose build")
    require(env.get("GO_VOICE_GATEWAY_ADDR") == "0.0.0.0:8282", f"{case.name}: unexpected Go Gateway listen addr")
    require(
        env.get("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL") == "ws://127.0.0.1:7860/ws",
        f"{case.name}: unexpected Python Gateway bridge URL",
    )
    require(env.get("GO_VOICE_GATEWAY_ICE_NETWORK_TYPES") == "udp4", f"{case.name}: ICE should default to udp4")
    require(env.get("GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT") == "35500", f"{case.name}: unexpected RTC min UDP port")
    require(env.get("GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT") == "35600", f"{case.name}: unexpected RTC max UDP port")


def assert_business_ports(case: ComposeCase, services: dict[str, dict[str, Any]]) -> None:
    for service_name in ("admin", "llm", "python-gateway", "stt", "tts"):
        service = services.get(service_name)
        if service is None:
            continue
        ports = service_ports(service)
        require(ports, f"{case.name}: {service_name} should publish a loopback host port")
        for port in ports:
            require(
                port.get("host_ip") == "127.0.0.1",
                f"{case.name}: {service_name} host port must bind to 127.0.0.1",
            )


def assert_python_gateway(services: dict[str, dict[str, Any]], case_name: str) -> None:
    service = services.get("python-gateway")
    if service is None:
        return

    env = service.get("environment") or {}
    require(env.get("STT_SERVICE_URL") == "grpc://stt:50054", f"{case_name}: unexpected STT service URL")
    require(env.get("LLM_SERVICE_URL") == "grpc://llm:50053", f"{case_name}: unexpected LLM service URL")
    require(env.get("TTS_SERVICE_URL") == "grpc://tts:50052", f"{case_name}: unexpected TTS service URL")
    require(env.get("SINGING_AUDIO_ROOT") == "/app/singing/audio", f"{case_name}: unexpected singing audio root")
    singing_mounts = [
        volume
        for volume in service.get("volumes") or []
        if volume.get("target") == "/app/singing/audio"
    ]
    require(len(singing_mounts) == 1, f"{case_name}: singing audio mount is required")
    require(singing_mounts[0].get("read_only") is True, f"{case_name}: singing audio mount must be read-only")


def assert_mcp_profile(case: ComposeCase, services: dict[str, dict[str, Any]]) -> None:
    require("mcp-weather" not in services, f"{case.name}: retired mcp-weather must not return")
    if "mcp" not in case.profiles:
        return
    for service_name in ("mcp-robot", "mcp-singing", "mcp-utils"):
        service = services.get(service_name)
        if service is None:
            continue
        require("mcp" in service.get("profiles", []), f"{case.name}: {service_name} must stay behind mcp profile")


def validate_case(case: ComposeCase, env_file: Path) -> str:
    config = run_compose_config(case, env_file)
    services = assert_expected_services(case, config)
    assert_go_gateway(case, services)
    assert_business_ports(case, services)
    assert_python_gateway(services, case.name)
    assert_mcp_profile(case, services)
    return f"{case.name}: {len(services)} services"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="Env file used only for compose rendering",
    )
    args = parser.parse_args()

    env_file = args.env_file
    if not env_file.is_absolute():
        env_file = ROOT / env_file

    try:
        require(env_file.exists(), f"env file does not exist: {env_file}")
        results = [validate_case(case, env_file) for case in CASES]
    except ValidationError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    for result in results:
        print(f"[OK] {result}")
    print("compose v3 validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
