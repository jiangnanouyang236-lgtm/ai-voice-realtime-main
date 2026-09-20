#!/usr/bin/env python3
"""Smoke-check the v3 Docker Compose deployment from the host."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, value = line.split("=", 1)
        key = key.strip()
        values[key] = value.strip().strip("'").strip('"')
    return values


def env_port(name: str, default: int, dotenv_values: dict[str, str]) -> int:
    raw = os.getenv(name) or dotenv_values.get(name) or str(default)
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name}={raw!r} is not a valid port") from None


def listen_port_from_addr(name: str, default: int, dotenv_values: dict[str, str]) -> int:
    raw = os.getenv(name) or dotenv_values.get(name)
    if not raw:
        return default
    value = str(raw).strip()
    if not value:
        return default
    if value.isdigit():
        return int(value)
    if value.startswith("[") and "]:" in value:
        value = value.rsplit("]:", 1)[1]
    elif ":" in value:
        value = value.rsplit(":", 1)[1]
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name}={raw!r} does not include a valid listen port") from None


def http_check(url: str, timeout: float) -> tuple[bool, str, Any]:
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(65536)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            payload: Any = None
            content_type = response.headers.get("content-type", "")
            if "json" in content_type.lower() and body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except json.JSONDecodeError:
                    payload = body.decode("utf-8", errors="replace")[:200]
            return True, f"{response.status} {elapsed_ms:.0f}ms", payload
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}", None
    except Exception as exc:
        return False, exc.__class__.__name__ + ": " + str(exc), None


def tcp_check(host: str, port: int, timeout: float) -> tuple[bool, str]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return True, f"connected {elapsed_ms:.0f}ms"
    except Exception as exc:
        return False, exc.__class__.__name__ + ": " + str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Host where compose ports are published")
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="Optional .env file for *_HOST_PORT values")
    parser.add_argument("--timeout", type=float, default=3.0, help="Per-check timeout in seconds")
    parser.add_argument("--skip-admin", action="store_true", help="Skip Admin API health check")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    dotenv_values = load_dotenv_values(Path(args.env_file))
    ports = {
        "go_gateway": listen_port_from_addr("GO_VOICE_GATEWAY_ADDR", 8282, dotenv_values),
        "python_gateway": env_port("PYTHON_GATEWAY_HOST_PORT", 7860, dotenv_values),
        "stt": env_port("STT_GRPC_HOST_PORT", 50054, dotenv_values),
        "llm": env_port("LLM_GRPC_HOST_PORT", 50053, dotenv_values),
        "tts": env_port("TTS_GRPC_HOST_PORT", 50052, dotenv_values),
        "admin": env_port("ADMIN_API_HOST_PORT", 18100, dotenv_values),
    }

    checks: list[dict[str, Any]] = []

    http_targets = [
        ("go_gateway_health", f"http://{args.host}:{ports['go_gateway']}/healthz"),
        ("go_gateway_status", f"http://{args.host}:{ports['go_gateway']}/internal/status"),
        ("python_gateway_health", f"http://{args.host}:{ports['python_gateway']}/healthz"),
    ]
    if not args.skip_admin:
        http_targets.append(("admin_health", f"http://{args.host}:{ports['admin']}/health"))

    for name, url in http_targets:
        ok, detail, payload = http_check(url, args.timeout)
        checks.append({"name": name, "ok": ok, "target": url, "detail": detail, "payload": payload})

    for name in ("stt", "llm", "tts"):
        ok, detail = tcp_check(args.host, ports[name], args.timeout)
        checks.append({"name": name + "_grpc_tcp", "ok": ok, "target": f"{args.host}:{ports[name]}", "detail": detail})

    success = all(check["ok"] for check in checks)
    result = {"success": success, "checks": checks}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for check in checks:
            mark = "OK" if check["ok"] else "FAIL"
            print(f"[{mark}] {check['name']} {check['target']} - {check['detail']}")
        if not success:
            print("compose v3 smoke failed", file=sys.stderr)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
