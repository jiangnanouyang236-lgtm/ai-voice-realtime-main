#!/usr/bin/env python3
"""Run a dated voice latency benchmark batch.

The runner keeps raw JSON/CSV/Markdown outputs from the existing benchmark
scripts and writes a small manifest/README so future optimization rounds can be
compared against the same dated baseline.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def _safe_label(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")


def _wait_for_port(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def _run_command(
    name: str,
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    log_dir: Path,
) -> dict[str, Any]:
    started = datetime.now().astimezone().isoformat()
    stdout_path = log_dir / f"{name}.stdout.txt"
    stderr_path = log_dir / f"{name}.stderr.txt"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    return {
        "name": name,
        "command": _redact_command(command),
        "started_at": started,
        "finished_at": datetime.now().astimezone().isoformat(),
        "returncode": process.returncode,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "ok": process.returncode == 0,
    }


def _redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    for item in command:
        if skip_next:
            redacted.append("<redacted>")
            skip_next = False
            continue
        redacted.append(item)
        if item in {"--api-key", "--tts-api-key", "--local-llm-api-key", "--local-tts-api-key"}:
            skip_next = True
    return redacted


def _resolve_secret(explicit: str, env_names: tuple[str, ...], fallback: str) -> str:
    if explicit and not config.is_missing_secret(explicit):
        return explicit.strip()
    for env_name in env_names:
        if not env_name:
            continue
        value = os.getenv(env_name)
        if value is not None and not config.is_missing_secret(value):
            return value.strip()
    return "" if config.is_missing_secret(fallback) else fallback.strip()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _metric(report: dict[str, Any] | None, metric: str, field: str = "p50") -> Any:
    if not report:
        return None
    return report.get("summary", {}).get("metrics", {}).get(metric, {}).get(field)


def _write_readme(batch_dir: Path, manifest: dict[str, Any]) -> None:
    def fmt(value: Any) -> Any:
        return "-" if value is None else value

    lines = [
        f"# Voice Latency Baseline - {manifest['date']}",
        "",
        f"- Label: `{manifest['label']}`",
        f"- Generated: `{manifest['generated_at']}`",
        f"- Rounds: `{manifest['rounds']}`",
        f"- Random seed: `{manifest['random_seed']}`",
        f"- Local LLM: `{manifest['env']['llm_base_url']}` model `{manifest['env']['llm_model']}`",
        f"- Local TTS: `{manifest['env']['tts_ws_url']}` model `{manifest['env']['tts_model']}`",
        f"- TTS submit policy: `{manifest['env']['tts_submit_policy']}`",
        "",
        "## Results",
        "",
        "| case | status | success | first audio p50 | first audio p95 | llm first p50 | tts setup p50 | bridge buffer p50 | provider after send p50 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for case in manifest["cases"]:
        summary = case.get("summary") or {}
        status = "ok" if case.get("ok") else ("skipped" if case.get("skipped") else "failed")
        lines.append(
            "| "
            f"`{case['name']}` | {status} | "
            f"{summary.get('ok', '-')}/{summary.get('total', '-')} | "
            f"{fmt(case.get('first_audio_p50_ms', '-'))} | "
            f"{fmt(case.get('first_audio_p95_ms', '-'))} | "
            f"{fmt(case.get('llm_first_p50_ms', '-'))} | "
            f"{fmt(case.get('tts_setup_p50_ms', '-'))} | "
            f"{fmt(case.get('bridge_buffer_p50_ms', '-'))} | "
            f"{fmt(case.get('provider_after_send_p50_ms', '-'))} |"
        )
    lines.extend([
        "",
        "## Notes",
        "",
        "- Raw benchmark JSON/CSV/Markdown files are under this directory.",
        "- Cloud TTS cases are skipped unless the requested API key env var is set.",
        "- Cloud realtime TTS chain cases record `tts_setup_ms` separately; "
        "`llm first p50` is measured from local LLM request start, `provider after send p50` is first audio after text reaches TTS, "
        "and `first audio p50` is full request-to-audio latency.",
        "- Local service logs are under `logs/` when `--start-services` is used.",
        "",
    ])
    (batch_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def _case_from_report(name: str, report_path: Path, *, command_result: dict[str, Any]) -> dict[str, Any]:
    report = _load_json(report_path)
    summary = (report or {}).get("summary", {})
    return {
        "name": name,
        "ok": command_result["ok"] and bool(report),
        "skipped": False,
        "report": str(report_path),
        "summary": summary,
        "command_result": command_result,
        "first_audio_p50_ms": _metric(report, "tts_first_audio_ms") or _metric(report, "ttfb_ms"),
        "first_audio_p95_ms": _metric(report, "tts_first_audio_ms", "p95") or _metric(report, "ttfb_ms", "p95"),
        "llm_first_p50_ms": _metric(report, "llm_first_text_ms"),
        "tts_setup_p50_ms": _metric(report, "tts_setup_ms"),
        "bridge_buffer_p50_ms": _metric(report, "tts_bridge_buffer_ms"),
        "provider_after_send_p50_ms": _metric(report, "tts_provider_first_pcm_after_send_ms"),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Run dated voice latency baseline cases.")
    parser.add_argument("--date", default=today)
    parser.add_argument("--label", default="company-lan-local-vllm")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--concurrency-rounds", type=int, default=10)
    parser.add_argument("--random-seed", type=int, default=623500)
    parser.add_argument("--out-dir", default="tmp/voice_latency_baselines")
    parser.add_argument("--start-services", action="store_true")
    parser.add_argument("--include-concurrency", action="store_true")
    parser.add_argument("--include-cloud-tts", action="store_true")
    parser.add_argument("--cloud-tts-api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--local-llm-api-key", default="")
    parser.add_argument("--local-llm-api-key-env", default="LLM_API_KEY")
    parser.add_argument("--local-tts-api-key", default="")
    parser.add_argument("--local-tts-api-key-env", default="QWEN3_TTS_CUSTOM_VOICE_API_KEY")
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    label = _safe_label(args.label)
    batch_dir = ROOT / args.out_dir / f"{args.date}_{label}"
    tts_out = batch_dir / "tts"
    chain_out = batch_dir / "chain"
    log_dir = batch_dir / "logs"
    for directory in (tts_out, chain_out, log_dir):
        directory.mkdir(parents=True, exist_ok=True)

    local_llm_api_key = _resolve_secret(
        args.local_llm_api_key,
        (args.local_llm_api_key_env,),
        config.LLM_API_KEY,
    )
    local_tts_api_key = _resolve_secret(
        args.local_tts_api_key,
        (args.local_tts_api_key_env,),
        config.QWEN3_TTS_CUSTOM_VOICE_API_KEY,
    )
    missing_secrets: list[str] = []
    if config.is_missing_secret(local_llm_api_key):
        missing_secrets.append(
            f"local LLM key (--local-llm-api-key or {args.local_llm_api_key_env})"
        )
    if config.is_missing_secret(local_tts_api_key):
        missing_secrets.append(
            f"local TTS key (--local-tts-api-key or {args.local_tts_api_key_env})"
        )
    if missing_secrets:
        raise SystemExit("Missing required benchmark secret(s): " + "; ".join(missing_secrets))

    env = os.environ.copy()
    env.update(
        {
            "LLM_BASE_URL": config.LLM_BASE_URL,
            "LLM_API_KEY": local_llm_api_key,
            "QWEN3_TTS_CUSTOM_VOICE_WS_URL": config.QWEN3_TTS_CUSTOM_VOICE_WS_URL,
            "QWEN3_TTS_CUSTOM_VOICE_API_KEY": local_tts_api_key,
            "QWEN3_TTS_CUSTOM_VOICE_MODEL": config.QWEN3_TTS_CUSTOM_VOICE_MODEL,
            "LOCAL_QWEN3_TTS_VOICE": config.LOCAL_QWEN3_TTS_VOICE,
            "LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE": str(config.LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE),
            "MCP_ENABLED": "false",
            "LLM_TOOL_LATENCY_EXPERIMENT": "true",
            "LLM_TOOL_ROUTER_CLASSIFIER": "true",
        }
    )

    services: list[subprocess.Popen] = []
    try:
        if args.start_services:
            services = _start_services(env, log_dir)

        cases: list[dict[str, Any]] = []

        provider_cases = [
            (
                "tts-local-direct-ws-c1",
                [
                    sys.executable,
                    "scripts/benchmark_tts_providers.py",
                    "--provider",
                    "vllm-ws-stream",
                    "--label",
                    "tts-local-direct-ws-c1",
                    "--rounds",
                    str(args.rounds),
                    "--concurrency",
                    "1",
                    "--seed",
                    str(args.random_seed),
                    "--min-chars",
                    "35",
                    "--max-chars",
                    "80",
                    "--input-chunk-chars",
                    "999",
                    "--input-chunk-delay-ms",
                    "0",
                    "--ws-url",
                    config.QWEN3_TTS_CUSTOM_VOICE_WS_URL,
                    "--model",
                    config.QWEN3_TTS_CUSTOM_VOICE_MODEL,
                    "--voice",
                    config.LOCAL_QWEN3_TTS_VOICE,
                    "--api-key-env",
                    "QWEN3_TTS_CUSTOM_VOICE_API_KEY",
                    "--vllm-sample-rate",
                    str(config.LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE),
                    "--out-dir",
                    str(tts_out),
                ],
                tts_out / "tts-local-direct-ws-c1-vllm-ws-stream.json",
            ),
            (
                "tts-local-grpc-stream-c1",
                [
                    sys.executable,
                    "scripts/benchmark_tts_providers.py",
                    "--provider",
                    "current-grpc",
                    "--label",
                    "tts-local-grpc-stream-c1",
                    "--rounds",
                    str(args.rounds),
                    "--concurrency",
                    "1",
                    "--seed",
                    str(args.random_seed),
                    "--min-chars",
                    "35",
                    "--max-chars",
                    "80",
                    "--input-chunk-chars",
                    "1",
                    "--input-chunk-delay-ms",
                    "10",
                    "--grpc-target",
                    "127.0.0.1:50052",
                    "--voice",
                    config.LOCAL_QWEN3_TTS_VOICE,
                    "--grpc-sample-rate",
                    "16000",
                    "--out-dir",
                    str(tts_out),
                ],
                tts_out / "tts-local-grpc-stream-c1-current-grpc.json",
            ),
        ]
        if args.include_concurrency:
            provider_cases.extend(
                [
                    (
                        "tts-local-direct-ws-c5",
                        [
                            sys.executable,
                            "scripts/benchmark_tts_providers.py",
                            "--provider",
                            "vllm-ws-stream",
                            "--label",
                            "tts-local-direct-ws-c5",
                            "--rounds",
                            str(args.concurrency_rounds),
                            "--concurrency",
                            "5",
                            "--seed",
                            str(args.random_seed + 10),
                            "--min-chars",
                            "35",
                            "--max-chars",
                            "80",
                            "--input-chunk-chars",
                            "999",
                            "--input-chunk-delay-ms",
                            "0",
                            "--ws-url",
                            config.QWEN3_TTS_CUSTOM_VOICE_WS_URL,
                            "--model",
                            config.QWEN3_TTS_CUSTOM_VOICE_MODEL,
                            "--voice",
                            config.LOCAL_QWEN3_TTS_VOICE,
                            "--api-key-env",
                            "QWEN3_TTS_CUSTOM_VOICE_API_KEY",
                            "--vllm-sample-rate",
                            str(config.LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE),
                            "--out-dir",
                            str(tts_out),
                        ],
                        tts_out / "tts-local-direct-ws-c5-vllm-ws-stream.json",
                    ),
                    (
                        "tts-local-grpc-stream-c5",
                        [
                            sys.executable,
                            "scripts/benchmark_tts_providers.py",
                            "--provider",
                            "current-grpc",
                            "--label",
                            "tts-local-grpc-stream-c5",
                            "--rounds",
                            str(args.concurrency_rounds),
                            "--concurrency",
                            "5",
                            "--seed",
                            str(args.random_seed + 10),
                            "--min-chars",
                            "35",
                            "--max-chars",
                            "80",
                            "--input-chunk-chars",
                            "1",
                            "--input-chunk-delay-ms",
                            "10",
                            "--grpc-target",
                            "127.0.0.1:50052",
                            "--voice",
                            config.LOCAL_QWEN3_TTS_VOICE,
                            "--grpc-sample-rate",
                            "16000",
                            "--out-dir",
                            str(tts_out),
                        ],
                        tts_out / "tts-local-grpc-stream-c5-current-grpc.json",
                    ),
                ]
            )

        for name, command, report_path in provider_cases:
            result = _run_command(name, command, env=env, cwd=ROOT, log_dir=log_dir)
            cases.append(_case_from_report(name, report_path, command_result=result))

        chain_cases = [
            (
                "llm-local-tts-local-c1",
                [
                    sys.executable,
                    "scripts/benchmark_llm_tts_chain.py",
                    "--tts-provider",
                    "local-grpc",
                    "--label",
                    "llm-local-tts-local-c1",
                    "--rounds",
                    str(args.rounds),
                    "--concurrency",
                    "1",
                    "--random-prompts",
                    "--random-seed",
                    str(args.random_seed + 100),
                    "--llm-target",
                    "127.0.0.1:50053",
                    "--tts-target",
                    "127.0.0.1:50052",
                    "--llm-model",
                    config.LLM_MODEL_NAME,
                    "--voice",
                    config.LOCAL_QWEN3_TTS_VOICE,
                    "--max-tokens",
                    "96",
                    "--timeout",
                    str(args.timeout),
                    "--out-dir",
                    str(chain_out),
                ],
                chain_out / "llm-local-tts-local-c1.json",
            ),
        ]
        if args.include_concurrency:
            chain_cases.append(
                (
                    "llm-local-tts-local-c5",
                    [
                        sys.executable,
                        "scripts/benchmark_llm_tts_chain.py",
                        "--tts-provider",
                        "local-grpc",
                        "--label",
                        "llm-local-tts-local-c5",
                        "--rounds",
                        str(args.concurrency_rounds),
                        "--concurrency",
                        "5",
                        "--random-prompts",
                        "--random-seed",
                        str(args.random_seed + 200),
                        "--llm-target",
                        "127.0.0.1:50053",
                        "--tts-target",
                        "127.0.0.1:50052",
                        "--llm-model",
                        config.LLM_MODEL_NAME,
                        "--voice",
                        config.LOCAL_QWEN3_TTS_VOICE,
                        "--max-tokens",
                        "64",
                        "--timeout",
                        str(args.timeout),
                        "--out-dir",
                        str(chain_out),
                    ],
                    chain_out / "llm-local-tts-local-c5.json",
                )
            )
        if args.include_cloud_tts and env.get(args.cloud_tts_api_key_env):
            cloud_modes = [
                ("llm-local-tts-cloud-server-commit-c1", "server_commit", args.random_seed + 300),
                ("llm-local-tts-cloud-commit-c1", "commit", args.random_seed + 310),
            ]
            for case_name, qwen_mode, seed in cloud_modes:
                chain_cases.append(
                    (
                        case_name,
                        [
                            sys.executable,
                            "scripts/benchmark_llm_tts_chain.py",
                            "--tts-provider",
                            "qwen-realtime-sdk",
                            "--label",
                            case_name,
                            "--rounds",
                            str(args.rounds),
                            "--concurrency",
                            "1",
                            "--random-prompts",
                            "--random-seed",
                            str(seed),
                            "--llm-target",
                            "127.0.0.1:50053",
                            "--llm-model",
                            config.LLM_MODEL_NAME,
                            "--cloud-tts-model",
                            "qwen3-tts-flash-realtime",
                            "--cloud-tts-voice",
                            "Cherry",
                            "--qwen-mode",
                            qwen_mode,
                            "--tts-api-key-env",
                            args.cloud_tts_api_key_env,
                            "--max-tokens",
                            "96",
                            "--timeout",
                            str(args.timeout),
                            "--out-dir",
                            str(chain_out),
                        ],
                        chain_out / f"{case_name}.json",
                    )
                )
        elif args.include_cloud_tts:
            for case_name in ("llm-local-tts-cloud-server-commit-c1", "llm-local-tts-cloud-commit-c1"):
                cases.append(
                    {
                        "name": case_name,
                        "ok": False,
                        "skipped": True,
                        "reason": f"{args.cloud_tts_api_key_env} is not set",
                        "summary": {},
                    }
                )

        for name, command, report_path in chain_cases:
            result = _run_command(name, command, env=env, cwd=ROOT, log_dir=log_dir)
            cases.append(_case_from_report(name, report_path, command_result=result))

        manifest = {
            "schema_version": "voice-latency-baseline/v1",
            "valid_for_comparison": True,
            "date": args.date,
            "label": label,
            "generated_at": datetime.now().astimezone().isoformat(),
            "rounds": args.rounds,
            "concurrency_rounds": args.concurrency_rounds,
            "random_seed": args.random_seed,
            "env": {
                "llm_base_url": config.LLM_BASE_URL,
                "llm_model": config.LLM_MODEL_NAME,
                "tts_ws_url": config.QWEN3_TTS_CUSTOM_VOICE_WS_URL,
                "tts_model": config.QWEN3_TTS_CUSTOM_VOICE_MODEL,
                "tts_profile_id": "default_tts_profile",
                "tts_submit_policy": "direct incremental input.text; no local split; no explicit WS split_granularity",
                "secrets": {
                    "local_llm_api_key": "set" if local_llm_api_key else "missing",
                    "local_tts_api_key": "set" if local_tts_api_key else "missing",
                    "cloud_tts_api_key": "set" if env.get(args.cloud_tts_api_key_env) else "missing",
                    "cloud_tts_api_key_env": args.cloud_tts_api_key_env,
                },
            },
            "cases": cases,
        }
        (batch_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _write_readme(batch_dir, manifest)
        print(json.dumps({"batch_dir": str(batch_dir), "cases": cases}, ensure_ascii=False, indent=2))
        return 0 if all(case.get("ok") or case.get("skipped") for case in cases) else 1
    finally:
        for service in services:
            service.terminate()
        for service in services:
            try:
                service.wait(timeout=5)
            except subprocess.TimeoutExpired:
                service.kill()


def _start_services(env: dict[str, str], log_dir: Path) -> list[subprocess.Popen]:
    services: list[subprocess.Popen] = []
    service_specs = [
        ("tts-service", [sys.executable, "tts/tts_grpc_server.py"], ("127.0.0.1", 50052)),
        ("llm-service", [sys.executable, "llm/llm_grpc_server.py"], ("127.0.0.1", 50053)),
    ]
    for name, command, (host, port) in service_specs:
        stdout = (log_dir / f"{name}.stdout.txt").open("w", encoding="utf-8")
        stderr = (log_dir / f"{name}.stderr.txt").open("w", encoding="utf-8")
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr, text=True)
        services.append(process)
        if not _wait_for_port(host, port, timeout=20):
            raise RuntimeError(f"{name} did not listen on {host}:{port}")
    return services


if __name__ == "__main__":
    raise SystemExit(main())
