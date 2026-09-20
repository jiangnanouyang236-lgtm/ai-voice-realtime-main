#!/usr/bin/env python3
"""Read-only, redacted environment preflight for humans and coding agents."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import socket
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
    "grpc": 50051,
    "mysql": 3306,
}


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    required: bool
    detail: str
    remediation: str = ""


def _check(
    check_id: str,
    status: str,
    detail: str,
    *,
    required: bool = False,
    remediation: str = "",
) -> Check:
    return Check(check_id, status, required, detail, remediation)


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / ".ai" / "harness.json"
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("harness manifest root must be an object")
    if data.get("schema_version") != 1:
        raise ValueError(f"unsupported harness schema: {data.get('schema_version')!r}")
    profiles = data.get("profiles")
    endpoints = data.get("endpoints")
    if not isinstance(profiles, dict) or not isinstance(endpoints, list):
        raise ValueError("harness manifest requires object profiles and array endpoints")
    endpoint_ids: set[str] = set()
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("id"), str):
            raise ValueError("every harness endpoint requires a string id")
        endpoint_id = endpoint["id"]
        if endpoint_id in endpoint_ids:
            raise ValueError(f"duplicate harness endpoint id: {endpoint_id}")
        endpoint_ids.add(endpoint_id)
        is_url = isinstance(endpoint.get("url_env"), str)
        is_host_port = isinstance(endpoint.get("host_env"), str) and isinstance(endpoint.get("port_env"), str)
        if is_url == is_host_port:
            raise ValueError(f"endpoint {endpoint_id} must define exactly one URL or host/port source")
    for profile_name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ValueError(f"profile {profile_name} must be an object")
        connectivity = profile.get("connectivity", [])
        required_env = profile.get("required_env", [])
        if not isinstance(connectivity, list) or not all(isinstance(item, str) for item in connectivity):
            raise ValueError(f"profile {profile_name} connectivity must be an array of strings")
        if not isinstance(required_env, list) or not all(isinstance(item, str) for item in required_env):
            raise ValueError(f"profile {profile_name} required_env must be an array of strings")
        unknown = set(connectivity) - endpoint_ids
        if unknown:
            raise ValueError(f"profile {profile_name} references unknown endpoints: {sorted(unknown)}")
    evidence_policy = data.get("evidence_policy")
    if not isinstance(evidence_policy, dict):
        raise ValueError("harness manifest requires object evidence_policy")
    if evidence_policy.get("schema_version") != "ai-voice-evidence/v1":
        raise ValueError("unsupported evidence policy schema")
    levels = evidence_policy.get("levels")
    capabilities = evidence_policy.get("capabilities")
    if not isinstance(levels, dict) or not levels:
        raise ValueError("evidence_policy requires non-empty object levels")
    if not isinstance(capabilities, dict) or not capabilities:
        raise ValueError("evidence_policy requires non-empty object capabilities")
    for level_name, level in levels.items():
        if not isinstance(level_name, str) or not isinstance(level, dict):
            raise ValueError("every evidence level requires a string name and object policy")
        for field in (
            "required_any_kinds",
            "required_all_kinds",
            "forbidden_endpoint_scopes",
            "allowed_observed_endpoint_scopes",
            "forbidden_observed_endpoint_scopes",
            "required_any_observed_endpoint_scopes",
        ):
            values = level.get(field, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"evidence level {level_name} field {field} must be an array of strings")
    for capability_name, capability in capabilities.items():
        if not isinstance(capability_name, str) or not isinstance(capability, dict):
            raise ValueError("every evidence capability requires a string name and object policy")
        unknown_levels = set(capability) - set(levels)
        if unknown_levels:
            raise ValueError(
                f"evidence capability {capability_name} references unknown levels: {sorted(unknown_levels)}"
            )
        for level_name, override in capability.items():
            if not isinstance(override, dict):
                raise ValueError(
                    f"evidence capability {capability_name}/{level_name} policy must be an object"
                )
            required_components = override.get("required_components", [])
            if not isinstance(required_components, list) or not all(
                isinstance(item, str) and item for item in required_components
            ):
                raise ValueError(
                    f"evidence capability {capability_name}/{level_name} required_components must be strings"
                )
            environment_endpoint_ids = override.get("environment_endpoint_ids", [])
            if not isinstance(environment_endpoint_ids, list) or not all(
                isinstance(item, str) and item in endpoint_ids for item in environment_endpoint_ids
            ):
                raise ValueError(
                    f"evidence capability {capability_name}/{level_name} environment_endpoint_ids must reference known endpoints"
                )
            required_assertions = override.get("required_assertions", {})
            if not isinstance(required_assertions, dict):
                raise ValueError(
                    f"evidence capability {capability_name}/{level_name} required_assertions must be an object"
                )
    return data


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse literal KEY=value pairs without sourcing or executing the file."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Match the repository's literal Python env loader. Shell `export` and
        # variable interpolation are intentionally not emulated here.
        if line.startswith("export "):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _profile_env_files(manifest: Mapping[str, Any], profile: str) -> list[str]:
    return [
        path
        for path, profiles in manifest.get("env_files", {}).items()
        if profile in profiles
    ]


def build_environment(
    root: Path,
    manifest: Mapping[str, Any],
    profile: str,
    process_env: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[Check]]:
    checks: list[Check] = []
    values: dict[str, str] = {}
    file_values: dict[str, dict[str, str]] = {}
    for relative_path in _profile_env_files(manifest, profile):
        path = root / relative_path
        if not path.is_file():
            checks.append(
                _check(
                    f"env-file:{relative_path}",
                    "FAIL",
                    f"required environment file is missing: {relative_path}",
                    required=True,
                    remediation=f"create {relative_path} from its tracked example; do not invent secrets",
                )
            )
            continue
        checks.append(
            _check(
                f"env-file:{relative_path}",
                "PASS",
                f"environment file present: {relative_path}",
                required=True,
            )
        )
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                checks.append(
                    _check(
                        f"env-permission:{relative_path}",
                        "FAIL",
                        f"{relative_path} permission is {mode:04o}; secret env must be 0600",
                        required=True,
                        remediation=f"chmod 600 {relative_path}",
                    )
                )
            else:
                checks.append(
                    _check(
                        f"env-permission:{relative_path}",
                        "PASS",
                        f"{relative_path} permission is private",
                        required=True,
                    )
                )
        parsed_values = load_dotenv(path)
        file_values[relative_path] = parsed_values
        values.update(parsed_values)
    values.update(dict(os.environ if process_env is None else process_env))
    # rust_client/run.audio_frontend_tcp.sh sources this file after process
    # startup, so these values really do override the parent shell.
    if profile == "hardware":
        values.update(file_values.get("rust_client/.env.local", {}))
    return values, checks


def _endpoint_from_definition(definition: Mapping[str, Any], values: Mapping[str, str]) -> tuple[str, int] | None:
    if "url_env" in definition:
        raw = values.get(definition["url_env"], "").strip()
        if not raw:
            return None
        parsed = urlsplit(raw)
        allowed_schemes = definition.get("schemes", [])
        if not parsed.hostname or (allowed_schemes and parsed.scheme not in allowed_schemes):
            return None
        try:
            port = parsed.port or DEFAULT_PORTS.get(parsed.scheme)
        except ValueError:
            return None
        if not port or not 1 <= port <= 65535:
            return None
        return parsed.hostname, port
    host = values.get(definition.get("host_env", ""), "").strip()
    port_text = values.get(definition.get("port_env", ""), "").strip()
    if not host or not port_text.isdigit() or any(char in host for char in "@/?#") or "://" in host or any(char.isspace() for char in host):
        return None
    if ":" in host:
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return None
        host = host.strip("[]")
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return host, port


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _has_placeholder(value: str, fragments: list[str]) -> bool:
    lowered = value.lower()
    return bool(VAR_REF_RE.search(value)) or any(fragment.lower() in lowered for fragment in fragments)


def run_preflight(
    root: Path,
    profile: str,
    *,
    connectivity: bool = False,
    timeout: float = 1.5,
    process_env: Mapping[str, str] | None = None,
) -> list[Check]:
    manifest = load_manifest(root)
    if profile not in manifest["profiles"]:
        raise ValueError(f"unknown profile: {profile}")

    checks: list[Check] = []
    for relative_path in manifest.get("required_read", []):
        exists = (root / relative_path).is_file()
        checks.append(
            _check(
                f"required-read:{relative_path}",
                "PASS" if exists else "FAIL",
                f"required context {'present' if exists else 'missing'}: {relative_path}",
                required=True,
                remediation=f"restore {relative_path}" if not exists else "",
            )
        )

    test_root = root / str(manifest.get("test_root", "test"))
    checks.append(
        _check(
            "test-root",
            "PASS" if test_root.is_dir() else "FAIL",
            f"pytest root is {test_root.relative_to(root)}",
            required=True,
            remediation="use test/ for pytest; keep documents under docs/" if not test_root.is_dir() else "",
        )
    )
    pytest_ready = importlib.util.find_spec("pytest") is not None
    checks.append(
        _check(
            "pytest-import",
            "PASS" if pytest_ready else "FAIL",
            "pytest is importable" if pytest_ready else "pytest is not installed in this Python environment",
            required=True,
            remediation="install requirements-dev.txt in the active development environment" if not pytest_ready else "",
        )
    )
    test_files = list(test_root.rglob("test_*.py")) if test_root.is_dir() else []
    checks.append(
        _check(
            "pytest-files",
            "PASS" if test_files else "FAIL",
            f"found {len(test_files)} pytest source files under {test_root.name}/",
            required=True,
            remediation="restore the canonical test/ tree" if not test_files else "",
        )
    )
    version_ok = sys.version_info >= (3, 11)
    checks.append(
        _check(
            "python-version",
            "PASS" if version_ok else "FAIL",
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            required=True,
            remediation="use Python 3.11 or newer" if not version_ok else "",
        )
    )

    values, env_checks = build_environment(root, manifest, profile, process_env)
    checks.extend(env_checks)
    checked_env_paths = set(_profile_env_files(manifest, profile))
    if profile != "offline" and os.name != "nt":
        for relative_path in manifest.get("private_env_files", []):
            if relative_path in checked_env_paths:
                continue
            path = root / relative_path
            if not path.is_file():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            checks.append(
                _check(
                    f"env-permission:{relative_path}",
                    "PASS" if not mode & 0o077 else "WARN",
                    f"{relative_path} permission is {'private' if not mode & 0o077 else f'{mode:04o}; expected 0600'}",
                    required=False,
                    remediation=f"chmod 600 {relative_path}" if mode & 0o077 else "",
                )
            )
    profile_config = manifest["profiles"][profile]
    placeholders = list(manifest.get("placeholder_fragments", []))
    for key in profile_config.get("required_env", []):
        value = values.get(key, "").strip()
        if profile in {"compose-v3", "compose-v4"}:
            reference = VAR_REF_RE.fullmatch(value)
            if reference:
                value = values.get(reference.group(1), "").strip()
        if not value:
            checks.append(
                _check(
                    f"env:{key}",
                    "FAIL",
                    f"required variable is missing: {key}",
                    required=True,
                    remediation=f"set {key} in the profile environment; do not copy a fake value",
                )
            )
        elif _has_placeholder(value, placeholders):
            checks.append(
                _check(
                    f"env:{key}",
                    "FAIL",
                    f"required variable still contains a placeholder: {key}",
                    required=True,
                    remediation=f"replace the placeholder for {key} in the private environment",
                )
            )
        else:
            checks.append(_check(f"env:{key}", "PASS", f"{key}=<set>", required=True))

    if profile in {"local", "lan", "hybrid", "hardware"}:
        runtime_local = root / ".ai" / "runtime.local.md"
        runtime_required = profile in {"lan", "hybrid", "hardware"}
        runtime_exists = runtime_local.is_file()
        checks.append(
            _check(
                "runtime-local",
                "PASS" if runtime_exists else ("FAIL" if runtime_required else "WARN"),
                "machine-local runtime context present" if runtime_exists else "machine-local runtime context is missing",
                required=runtime_required,
                remediation="copy .ai/runtime.local.example.md to .ai/runtime.local.md and record current facts"
                if not runtime_exists
                else "",
            )
        )
        if runtime_exists:
            runtime_text = runtime_local.read_text(encoding="utf-8")
            marker_groups = (
                ("Active profile:", "## Current role"),
                ("Machine role:",),
                ("Last configuration inventory:", "Last verified:"),
                ("Live connectivity:", "Last live check"),
                ("## Server registry",),
                ("## Access",),
            )
            missing_markers = [group[0] for group in marker_groups if not any(marker in runtime_text for marker in group)]
            if re.search(r"<[^>\n]+>", runtime_text):
                missing_markers.append("unresolved <placeholder>")
            checks.append(
                _check(
                    "runtime-local-content",
                    "PASS" if not missing_markers else ("FAIL" if runtime_required else "WARN"),
                    "runtime context contains role, inventory, connectivity, server and access facts"
                    if not missing_markers
                    else f"runtime context is incomplete; missing: {', '.join(missing_markers)}",
                    required=runtime_required,
                    remediation="refresh .ai/runtime.local.md from its example without copying secret values" if missing_markers else "",
                )
            )
            if os.name != "nt":
                runtime_mode = stat.S_IMODE(runtime_local.stat().st_mode)
                private = not runtime_mode & 0o077
                checks.append(
                    _check(
                        "runtime-local-permission",
                        "PASS" if private else ("FAIL" if runtime_required else "WARN"),
                        "runtime context permission is private" if private else f"runtime context permission is {runtime_mode:04o}; expected 0600",
                        required=runtime_required,
                        remediation="chmod 600 .ai/runtime.local.md" if not private else "",
                    )
                )
            inventory_match = re.search(
                r"(?:Last configuration inventory|Last verified):[^\n]*(\d{4}-\d{2}-\d{2})",
                runtime_text,
            )
            if inventory_match:
                inventory_date = date.fromisoformat(inventory_match.group(1))
                age_days = (date.today() - inventory_date).days
                checks.append(
                    _check(
                        "runtime-local-freshness",
                        "PASS" if age_days <= 30 else "WARN",
                        f"runtime context inventory age is {age_days} days",
                        remediation="refresh runtime facts before live work" if age_days > 30 else "",
                    )
                )
        llm_token = values.get("LLM_VISION_GATEWAY_TOKEN", "").strip()
        compose_token = values.get("VISION_INTERNAL_TOKEN", "").strip()
        if not llm_token and not compose_token and profile == "local":
            checks.append(_check("vision-token-contract", "SKIP", "Vision token is not configured for the local profile"))
        elif llm_token and not compose_token:
            checks.append(_check("vision-token-contract", "PASS", "bare-service Vision token is configured; Compose alias is not selected"))
        elif compose_token and not llm_token:
            checks.append(_check("vision-token-contract", "WARN", "Compose Vision token is configured without the bare-service alias"))
        elif compose_token == llm_token or compose_token == "${LLM_VISION_GATEWAY_TOKEN}":
            checks.append(_check("vision-token-contract", "PASS", "Vision token aliases are consistent", required=True))
        else:
            checks.append(
                _check(
                    "vision-token-contract",
                    "FAIL",
                    "LLM_VISION_GATEWAY_TOKEN and VISION_INTERNAL_TOKEN are missing or inconsistent",
                    required=True,
                    remediation="define one token and map the second variable to it; never print the token",
                )
            )

        proxy_value = next(
            (
                values.get(name, "").strip()
                for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
                if values.get(name, "").strip()
            ),
            "",
        )
        checks.append(
            _check(
                "download-proxy",
                "PASS" if proxy_value else "WARN",
                "download proxy is configured" if proxy_value else "download proxy is not enabled in the current process",
                remediation="read docs/ai-runtime-environment.md before an external download" if not proxy_value else "",
            )
        )

    if profile == "model-host":
        ports: dict[int, str] = {}
        for key in ("VLLM_STT_HOST_PORT", "VLLM_LLM_HOST_PORT", "VLLM_ROUTER_HOST_PORT", "VLLM_TTS_HOST_PORT", "VLLM_LLM_MINI_HOST_PORT"):
            raw = values.get(key, "").strip()
            if not raw:
                continue
            if not raw.isdigit() or not 1 <= int(raw) <= 65535:
                checks.append(_check(f"port:{key}", "FAIL", f"{key} is not a valid port in 1..65535", required=True))
                continue
            port = int(raw)
            if port in ports:
                checks.append(
                    _check(
                        f"port:{key}",
                        "FAIL",
                        f"model host port collision: {key} conflicts with {ports[port]} on {port}",
                        required=True,
                    )
                )
            else:
                ports[port] = key

    endpoint_map = {definition["id"]: definition for definition in manifest.get("endpoints", [])}
    for endpoint_id in profile_config.get("connectivity", []):
        endpoint = _endpoint_from_definition(endpoint_map[endpoint_id], values)
        if endpoint is None:
            checks.append(
                _check(
                    f"endpoint:{endpoint_id}",
                    "FAIL",
                    f"cannot derive a host and port for {endpoint_id}",
                    required=True,
                )
            )
            continue
        host, port = endpoint
        display_host = _format_host(host)
        checks.append(_check(f"endpoint:{endpoint_id}", "PASS", f"{endpoint_id} configured at {display_host}:{port}", required=True))
        if connectivity:
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    pass
            except (OSError, OverflowError) as exc:
                checks.append(
                    _check(
                        f"connectivity:{endpoint_id}",
                        "FAIL",
                        f"TCP probe failed for {display_host}:{port}: {exc.__class__.__name__}",
                        required=True,
                        remediation="verify network path and target service before changing code",
                    )
                )
            else:
                checks.append(_check(f"connectivity:{endpoint_id}", "PASS", f"TCP probe passed for {display_host}:{port}", required=True))

    if profile in {"local", "lan", "hybrid", "hardware"} and (root / ".env").is_file() and (root / ".env.example").is_file():
        actual_keys = set(load_dotenv(root / ".env"))
        example_keys = set(load_dotenv(root / ".env.example"))
        undocumented = sorted(actual_keys - example_keys)
        checks.append(
            _check(
                "env-contract-drift",
                "WARN" if undocumented else "PASS",
                f"active env keys missing from .env.example: {', '.join(undocumented)}" if undocumented else "active env keys are represented in .env.example",
                remediation="document active keys in .env.example or remove obsolete local keys" if undocumented else "",
            )
        )
    return checks


def render_text(checks: list[Check]) -> str:
    lines = []
    for item in checks:
        suffix = f"; remediation: {item.remediation}" if item.remediation else ""
        lines.append(f"[{item.status}] {item.id}: {item.detail}{suffix}")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(item.required and item.status == "FAIL" for item in checks) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=("offline", "local", "compose-v3", "compose-v4", "lan", "hybrid", "hardware", "model-host"),
        required=True,
    )
    parser.add_argument("--connectivity", action="store_true", help="perform bounded TCP probes; never starts services")
    parser.add_argument("--timeout", type=float, default=1.5, help="per TCP probe timeout in seconds")
    parser.add_argument("--json", action="store_true", help="emit stable redacted JSON")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        checks = run_preflight(args.root.resolve(), args.profile, connectivity=args.connectivity, timeout=max(0.1, min(args.timeout, 3.0)))
    except (KeyError, TypeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"preflight configuration error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"profile": args.profile, "checks": [asdict(item) for item in checks]}, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(render_text(checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
