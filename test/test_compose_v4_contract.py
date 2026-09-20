from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_v4_service_names_and_gateway_dns_are_stable() -> None:
    config = _load("docker-compose.v4.yml")
    services = config["services"]

    assert set(services) == {
        "wzk-stt-grpc",
        "wzk-llm-grpc",
        "wzk-tts-grpc",
        "wzk-python-gateway",
        "wzk-admin-api",
        "wzk-mcp-utils",
        "wzk-mcp-robot",
        "wzk-mcp-singing",
    }
    gateway_env = services["wzk-python-gateway"]["environment"]
    assert gateway_env["STT_SERVICE_URL"] == "grpc://wzk-stt-grpc:50054"
    assert gateway_env["LLM_SERVICE_URL"] == "grpc://wzk-llm-grpc:50053"
    assert gateway_env["TTS_SERVICE_URL"] == "grpc://wzk-tts-grpc:50052"
    assert gateway_env["SINGING_AUDIO_ROOT"] == "/app/singing/audio"
    assert services["wzk-python-gateway"]["volumes"] == [
        "${SINGING_AUDIO_V4_HOST_DIR:-./singing/audio}:/app/singing/audio:ro"
    ]

    for service in services.values():
        assert service["logging"] == {
            "driver": "json-file",
            "options": {"max-size": "100m", "max-file": "5"},
        }


def test_v4_mcp_services_do_not_receive_database_credentials() -> None:
    services = _load("docker-compose.v4.yml")["services"]

    assert "CONFIG_DATABASE_URL" not in services["wzk-mcp-utils"]["environment"]
    assert "CONFIG_DATABASE_URL" not in services["wzk-mcp-robot"]["environment"]
    assert "CONFIG_DATABASE_URL" not in services["wzk-mcp-singing"]["environment"]
    assert all("weather" not in name for name in services)


def test_compatibility_mcp_compose_has_no_retired_weather_service() -> None:
    services = _load("deploy/compose/mcp.yml")["services"]
    assert set(services) == {"mcp-utils", "mcp-robot", "mcp-singing"}


def test_v4_dev_override_never_mounts_root_env() -> None:
    config = _load("docker-compose.v4.dev.yml")
    volumes = config["x-python-source-volumes"]

    assert volumes
    assert all(".env" not in volume for volume in volumes)
    assert all(volume.endswith(":ro") for volume in volumes)


def test_real_singing_audio_is_excluded_from_python_image_context() -> None:
    patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "singing/audio/" in patterns
