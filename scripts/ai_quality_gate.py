#!/usr/bin/env python3
"""Token-aware quality-gate dispatcher for AI repository maintenance.

The dispatcher reuses existing tests, captures full output under tmp/, and only
prints a compact machine-readable summary. Live, external-write, and hardware
checks are never started implicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT_SEC = 300
FAILURE_EXCERPT_LINES = 40
FAILURE_EXCERPT_CHARS = 6000
AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,}\]]+")
CREDENTIAL_RE = re.compile(r'(?i)(["\']?(?:credential|password|token|secret)["\']?\s*[:=]\s*["\']?)[^"\'\s,}\]]+')


@dataclass(frozen=True)
class Gate:
    gate_id: str
    command: tuple[str, ...]
    cwd: str = "."
    timeout_sec: int = DEFAULT_TIMEOUT_SEC


@dataclass
class GateResult:
    gate_id: str
    status: str
    duration_ms: int
    exit_code: int | None
    log: str
    failure_excerpt: str = ""


BASE_GATES = (
    Gate("diff_check", ("git", "diff", "--check"), timeout_sec=30),
    Gate("repo_hygiene", (sys.executable, "scripts/check_repo_hygiene.py"), timeout_sec=60),
)
PYTHON_GATE = Gate("python_full", (sys.executable, "-m", "pytest", "-q"), timeout_sec=300)
GO_GATE = Gate(
    "go_full",
    ("go", "test", "./..."),
    cwd="go_voice_gateway",
    timeout_sec=300,
)
RUST_GATE = Gate(
    "rust_native",
    (
        "cargo",
        "test",
        "--manifest-path",
        "rust_client/Cargo.toml",
        "--features",
        "native-webrtc",
    ),
    timeout_sec=600,
)
EVAL_GOLD_GATE = Gate(
    "eval_gold",
    (sys.executable, "scripts/validate_eval_gold.py"),
    timeout_sec=120,
)
COMPOSE_GATE = Gate(
    "compose_static",
    (sys.executable, "scripts/validate_compose_v3.py"),
    timeout_sec=120,
)


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def changed_paths(base: str | None = None) -> list[str]:
    paths: set[str] = set()
    if base:
        paths.update(_git_lines("diff", "--name-only", f"{base}...HEAD"))
    else:
        paths.update(_git_lines("diff", "--name-only"))
        paths.update(_git_lines("diff", "--cached", "--name-only"))
        paths.update(_git_lines("ls-files", "--others", "--exclude-standard"))
    return sorted(paths)


def _matches_prefix(paths: Iterable[str], prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefixes) for path in paths)


def select_gates(paths: list[str], mode: str) -> tuple[list[Gate], list[str]]:
    gates = list(BASE_GATES)
    manual: list[str] = []
    if mode == "quick":
        return gates, manual

    full = mode in {"full", "release"}
    python_changed = full or any(path.endswith(".py") for path in paths)
    go_changed = full or _matches_prefix(paths, ("go_voice_gateway/",))
    rust_changed = full or _matches_prefix(paths, ("rust_client/",))
    eval_changed = full or _matches_prefix(
        paths,
        ("data/eval_gold/", "llm/", "scripts/eval_", "scripts/validate_eval_"),
    )
    compose_changed = full or _matches_prefix(paths, ("deploy/", "docker-compose"))

    if python_changed:
        gates.append(PYTHON_GATE)
    if go_changed:
        gates.append(GO_GATE)
    if rust_changed:
        gates.append(RUST_GATE)
    if eval_changed:
        gates.append(EVAL_GOLD_GATE)
        manual.append(
            "授权连接 Router/LLM 后运行 eval_acceptance.py，并把输出写入 reports/"
        )
    if compose_changed:
        gates.append(COMPOSE_GATE)

    protocol_paths = (
        "gateway/",
        "go_voice_gateway/",
        "rust_client/src/protocol",
        "rust_client/src/transport",
        "proto/",
    )
    if _matches_prefix(paths, protocol_paths):
        manual.append("根据改动能力选择 voice-m1-e2e、Vision 或 TURN 定向真实链路")
    if _matches_prefix(paths, ("mcp_servers/robot", "gateway/", "llm/")):
        manual.append("涉及外部写入时，需显式授权后再跑 test_01 MQTT 门禁")
    if mode == "release":
        manual.extend(
            [
                "发布前按能力生成并校验 HYBRID/LIVE evidence report",
                "授权连接 Router/LLM 后运行 eval_acceptance.py，并把输出写入 reports/",
                "硬件门禁保持 DEFERRED，除非另有明确授权和设备条件",
            ]
        )
    return _dedupe_gates(gates), list(dict.fromkeys(manual))


def _dedupe_gates(gates: Iterable[Gate]) -> list[Gate]:
    result: list[Gate] = []
    seen: set[str] = set()
    for gate in gates:
        if gate.gate_id not in seen:
            result.append(gate)
            seen.add(gate.gate_id)
    return result


def _failure_excerpt(output: str) -> str:
    lines = output.splitlines()
    excerpt = "\n".join(lines[-FAILURE_EXCERPT_LINES:])
    return excerpt[-FAILURE_EXCERPT_CHARS:]


def _secret_values() -> list[str]:
    try:
        from scripts.ai_preflight import load_dotenv
    except ModuleNotFoundError:
        # Direct script execution adds scripts/, not the repository root.
        from ai_preflight import load_dotenv

    manifest = json.loads((ROOT / ".ai/harness.json").read_text(encoding="utf-8"))
    secret_names = set(manifest.get("secret_env", []))
    values: set[str] = {
        value for key, value in os.environ.items() if key in secret_names and len(value) >= 6
    }
    for relative_path in manifest.get("private_env_files", []):
        path = ROOT / relative_path
        if not path.is_file():
            continue
        parsed = load_dotenv(path)
        for key in secret_names:
            value = parsed.get(key, "")
            if len(value) >= 6:
                values.add(value)
            if key == "GO_VOICE_GATEWAY_ICE_SERVERS" and value:
                try:
                    for entry in json.loads(value):
                        for nested_key in ("username", "credential"):
                            nested_value = str(entry.get(nested_key) or "")
                            if len(nested_value) >= 4:
                                values.add(nested_value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
    return sorted(values, key=len, reverse=True)


def _redact_output(output: str, secrets: Iterable[str]) -> str:
    redacted = output
    for value in secrets:
        redacted = redacted.replace(value, "<redacted>")
    redacted = AUTH_RE.sub(r"\1<redacted>", redacted)
    redacted = CREDENTIAL_RE.sub(r"\1<redacted>", redacted)
    return redacted


def run_gate(gate: Gate, log_dir: Path, secrets: Iterable[str] = ()) -> GateResult:
    started = time.monotonic()
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(ROOT))
    if gate.gate_id == "go_full":
        env.setdefault("GOCACHE", "/private/tmp/dify_stream_test_go_build_cache")
    cwd = ROOT / gate.cwd
    try:
        result = subprocess.run(
            list(gate.command),
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=gate.timeout_sec,
            check=False,
        )
        output = _redact_output(result.stdout, secrets)
        status = "PASS" if result.returncode == 0 else "FAIL"
        exit_code: int | None = result.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += f"\nTIMEOUT after {gate.timeout_sec}s\n"
        output = _redact_output(output, secrets)
        status = "TIMEOUT"
        exit_code = None
    log_path = log_dir / f"{gate.gate_id}.log"
    log_path.write_text(output, encoding="utf-8")
    return GateResult(
        gate_id=gate.gate_id,
        status=status,
        duration_ms=round((time.monotonic() - started) * 1000),
        exit_code=exit_code,
        log=str(log_path.relative_to(ROOT)),
        failure_excerpt="" if status == "PASS" else _failure_excerpt(output),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("quick", "changed", "full", "release"), default="changed")
    parser.add_argument("--base", help="Compare committed paths from BASE...HEAD")
    parser.add_argument("--paths", nargs="*", help="Explicit changed paths, mainly for review/CI")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    paths = sorted(set(args.paths if args.paths is not None else changed_paths(args.base)))
    gates, manual = select_gates(paths, args.mode)
    plan = {
        "schema_version": "ai-quality-gate/v1",
        "mode": args.mode,
        "changed_paths": paths,
        "gates": [gate.gate_id for gate in gates],
        "manual_gates": manual,
        "output_policy": {
            "terminal": "compact_summary_and_failure_excerpt_only",
            "full_logs": "tmp/ai-quality-gate/",
            "failure_excerpt_lines": FAILURE_EXCERPT_LINES,
            "failure_excerpt_chars": FAILURE_EXCERPT_CHARS,
        },
    }
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))
        return 0

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = args.output_dir or Path("tmp") / "ai-quality-gate" / run_id
    log_dir = ROOT / output_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    results: list[GateResult] = []
    secrets = _secret_values()
    for gate in gates:
        result = run_gate(gate, log_dir, secrets)
        results.append(result)
        if result.status != "PASS":
            break
    summary = {
        **plan,
        "status": "PASS" if all(item.status == "PASS" for item in results) else "FAIL",
        "passed": sum(item.status == "PASS" for item in results),
        "executed": len(results),
        "planned": len(gates),
        "results": [asdict(item) for item in results],
    }
    summary_path = log_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    compact = {
        "status": summary["status"],
        "mode": args.mode,
        "passed": summary["passed"],
        "executed": summary["executed"],
        "planned": summary["planned"],
        "summary": str(summary_path.relative_to(ROOT)),
        "manual_gates": manual,
        "failures": [
            {"gate_id": item.gate_id, "excerpt": item.failure_excerpt}
            for item in results
            if item.status != "PASS"
        ],
    }
    print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
