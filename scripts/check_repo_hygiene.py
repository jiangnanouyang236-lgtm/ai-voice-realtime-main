#!/usr/bin/env python3
"""Fail when generated, secret, or misplaced files become tracked."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = {
    "AGENTS.md",
    ".ai/harness.json",
    ".ai/evidence-report.example.json",
    "scripts/capture_evidence_environment.py",
    "scripts/local_mqtt_fixture_broker.py",
    ".ai/runtime.local.example.md",
    "docs/ai-runtime-environment.md",
    "docs/repository-hygiene.md",
}
FORBIDDEN_EXACT = {
    ".env",
    ".DS_Store",
    "router_eval.json",
    "rust_client/.env.local",
}
FORBIDDEN_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "target",
}
FORBIDDEN_PREFIXES = (
    "reports/",
    "tmp/",
    "tests/",
    "rust_client/dist/",
    "go_voice_gateway/dist/",
    "gateway/dist/",
)
VARIABLE_REFERENCE_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
DATABASE_EXAMPLE = "mysql://user:password@127.0.0.1:3306/wzk_ai_voice?charset=utf8mb4"


def is_forbidden(path: str) -> str | None:
    if path in FORBIDDEN_EXACT:
        return "machine-local or generated root artifact"
    parts = set(Path(path).parts)
    if parts & FORBIDDEN_PARTS:
        return "cache or build directory"
    if path.startswith(FORBIDDEN_PREFIXES):
        return "generated output or misplaced test documentation"
    if path.endswith(".variants.jsonl") or ".variants.variants." in path:
        return "generated evaluation variant is not loaded by the canonical runner"
    if path.startswith("deploy/vllm/") and "/.env" in path and not path.endswith(".example"):
        return "real model environment file"
    return None


def tracked_files(root: Path = ROOT) -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    return sorted(item.decode("utf-8") for item in output.split(b"\0") if item)


def untracked_files(root: Path = ROOT) -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root
    )
    return sorted(item.decode("utf-8") for item in output.split(b"\0") if item)


def candidate_files(root: Path, *, index: bool) -> list[str]:
    tracked = tracked_files(root)
    if index:
        return tracked
    existing_tracked = [path for path in tracked if (root / path).is_file()]
    return sorted(set(existing_tracked) | set(untracked_files(root)))


def candidate_text(root: Path, relative_path: str, *, index: bool) -> str:
    if index:
        return subprocess.check_output(
            ["git", "show", f":{relative_path}"], cwd=root, text=True
        )
    return (root / relative_path).read_text(encoding="utf-8")


def template_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def scan(root: Path = ROOT, *, index: bool = False) -> list[str]:
    candidates = candidate_files(root, index=index)
    candidate_set = set(candidates)
    problems = [f"{path}: {reason}" for path in candidates if (reason := is_forbidden(path))]
    problems.extend(
        f"{path}: required repository contract is missing"
        for path in sorted(REQUIRED_FILES)
        if path not in candidate_set
    )
    manifest_path = ".ai/harness.json"
    secret_keys: set[str] = set()
    if manifest_path in candidate_set:
        try:
            manifest = json.loads(candidate_text(root, manifest_path, index=index))
            raw_secret_keys = manifest.get("secret_env", [])
            if not isinstance(raw_secret_keys, list) or not all(isinstance(item, str) for item in raw_secret_keys):
                raise ValueError("secret_env must be an array of strings")
            secret_keys = set(raw_secret_keys)
        except (json.JSONDecodeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
            problems.append(f"{manifest_path}: invalid secret contract: {exc}")
    for relative_path in (".env.example", "deploy/env.v3.compose.example"):
        if relative_path not in candidate_set:
            problems.append(f"{relative_path}: required environment template is missing")
            continue
        values = template_values(candidate_text(root, relative_path, index=index))
        for key in sorted(secret_keys):
            value = values.get(key, "")
            if key == "CONFIG_DATABASE_URL" and value == DATABASE_EXAMPLE:
                continue
            if value and not VARIABLE_REFERENCE_RE.fullmatch(value):
                problems.append(f"{relative_path}: sensitive template key {key} must be empty or reference another variable")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        action="store_true",
        help="inspect the exact staged tree; required for commit/CI gates",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        problems = scan(args.root.resolve(), index=args.index)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Repository hygiene check could not inspect Git state: {exc}")
        return 2
    if problems:
        print("Repository hygiene check failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
