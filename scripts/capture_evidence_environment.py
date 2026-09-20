#!/usr/bin/env python3
"""Capture a redacted, hashable endpoint inventory for an evidence report."""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ai_preflight import (  # noqa: E402
    ROOT,
    _endpoint_from_definition,
    build_environment,
    load_manifest,
)


SCHEMA_VERSION = "ai-voice-environment/v1"


def classify_host_scope(host: str) -> str:
    normalized = host.strip().strip("[]").lower().rstrip(".")
    if normalized in {"localhost", "localhost.localdomain"}:
        return "localhost"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        if normalized.endswith(".local") or ".lan" in normalized:
            return "lan"
        return "external"
    if address.is_loopback:
        return "localhost"
    if address.is_private or address.is_link_local:
        return "lan"
    return "external"


def build_inventory(
    *,
    root: Path,
    profile: str,
    capability: str,
    claimed_level: str,
    process_env: dict[str, str] | None = None,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    manifest = load_manifest(root)
    if profile not in manifest["profiles"]:
        raise ValueError(f"unknown profile: {profile}")
    policy = manifest["evidence_policy"]
    capability_policy = policy["capabilities"].get(capability)
    if not isinstance(capability_policy, dict):
        raise ValueError(f"unknown capability: {capability}")
    level_policy = capability_policy.get(claimed_level)
    if not isinstance(level_policy, dict):
        raise ValueError(f"capability {capability} does not define {claimed_level}")
    endpoint_ids = level_policy.get("environment_endpoint_ids", [])
    if not endpoint_ids:
        raise ValueError(f"{capability}/{claimed_level} does not define environment_endpoint_ids")

    values, checks = build_environment(root, manifest, profile, process_env)
    failed_checks = [item.id for item in checks if item.required and item.status == "FAIL"]
    if failed_checks:
        raise ValueError(f"environment preflight failed: {failed_checks}")
    definitions = {item["id"]: item for item in manifest["endpoints"]}
    endpoints = []
    for endpoint_id in endpoint_ids:
        endpoint = _endpoint_from_definition(definitions[endpoint_id], values)
        if endpoint is None:
            raise ValueError(f"cannot derive configured endpoint: {endpoint_id}")
        host, _port = endpoint
        endpoints.append(
            {
                "component": endpoint_id,
                "scope": classify_host_scope(host),
            }
        )

    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            text=True,
        ).strip()
    )
    if dirty and not allow_dirty:
        raise ValueError("worktree is dirty; commit the collector and target code before capture")
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now().astimezone().isoformat(),
        "commit": commit,
        "dirty": dirty,
        "profile": profile,
        "capability": capability,
        "claimed_level": claimed_level,
        "endpoints": endpoints,
    }


def _safe_output_path(root: Path, output: Path) -> Path:
    resolved = output.resolve() if output.is_absolute() else (root / output).resolve()
    allowed_roots = ((root / "reports").resolve(), (root / "tmp").resolve())
    if not any(resolved.is_relative_to(parent) for parent in allowed_roots):
        raise ValueError("output must be under reports/ or tmp/")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--claimed-level", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="capture for diagnosis only; dirty artifacts cannot satisfy evidence validation",
    )
    args = parser.parse_args(argv)
    try:
        payload = build_inventory(
            root=ROOT,
            profile=args.profile,
            capability=args.capability,
            claimed_level=args.claimed_level,
            allow_dirty=args.allow_dirty,
        )
        output = _safe_output_path(ROOT, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Environment evidence capture failed: {exc}")
        return 1
    print(f"Environment evidence captured: {output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
