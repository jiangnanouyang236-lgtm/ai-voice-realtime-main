#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GO_GATEWAY_DIR = ROOT / "go_voice_gateway"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run repeatable WebRTC weak-network checks. By default this runs "
            "offline unit-level checks only; live STUN/TURN probes are opt-in."
        )
    )
    parser.add_argument(
        "--skip-offline",
        action="store_true",
        help="Skip offline Go/Rust tests and run only requested live probes.",
    )
    parser.add_argument(
        "--skip-rust",
        action="store_true",
        help="Skip Rust native WebRTC tests in the offline matrix.",
    )
    parser.add_argument(
        "--live-route-probe",
        action="store_true",
        help="Run rtc_route_probe against a live Go Gateway.",
    )
    parser.add_argument(
        "--live-turn-only",
        action="store_true",
        help="Also force a TURN-only live probe. Implies --live-route-probe.",
    )
    parser.add_argument(
        "--gateway-ws-url",
        default=env_first(
            "RTC_ROUTE_PROBE_WS_URL",
            "GO_LIVE_SMOKE_WS_URL",
            fallback="ws://127.0.0.1:8282/ws",
        ),
        help="Go Gateway WebSocket URL for live probes.",
    )
    parser.add_argument(
        "--robot-id",
        default=env_first("RTC_ROUTE_PROBE_ROBOT_ID", "ROBOT_ID", fallback="test_01"),
        help="Robot ID for live probes.",
    )
    parser.add_argument(
        "--robot-secret",
        default=env_first("RTC_ROUTE_PROBE_ROBOT_SECRET", "ROBOT_SECRET", fallback=""),
        help="Robot secret for live probes, if Gateway auth requires it.",
    )
    parser.add_argument("--samples", type=int, default=3, help="Live route probe sample count.")
    parser.add_argument(
        "--sample-interval-sec",
        type=float,
        default=3.0,
        help="Seconds between selected ICE pair samples.",
    )
    parser.add_argument(
        "--timeout-sec",
        type=float,
        default=30.0,
        help="Overall live route probe timeout in seconds.",
    )
    return parser.parse_args()


def env_first(*names: str, fallback: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return fallback


def duration_arg(seconds: float) -> str:
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{seconds:.3f}s"


def command_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GOCACHE", "/private/tmp/dify_stream_test_go_cache")
    return env


def run_case(name: str, cmd: list[str], cwd: Path, env: dict[str, str]) -> bool:
    print(f"\n== {name}", flush=True)
    print(f"cwd: {cwd}", flush=True)
    print("+ " + " ".join(cmd), flush=True)
    started = time.monotonic()
    completed = subprocess.run(cmd, cwd=cwd, env=env)
    elapsed = time.monotonic() - started
    if completed.returncode == 0:
        print(f"PASS {name} ({elapsed:.1f}s)", flush=True)
        return True
    print(f"FAIL {name} ({elapsed:.1f}s, exit={completed.returncode})", flush=True)
    return False


def offline_cases(skip_rust: bool) -> list[tuple[str, list[str], Path]]:
    go_weak_tests = (
        "Test("
        "ASRPrototypeSinkDeduplicatesOrdersAndMarksLossyRTP|"
        "ASRPrototypeSinkEndGraceKeepsLateRTPInSameHandoff|"
        "QueuedASREncodedAudioHandoffSinkDropsWhenQueueFull|"
        "SelectedICECandidateRouteFromStatsClassifiesTURNRelay|"
        "SelectedICECandidateRouteFromStatsClassifiesSTUNOrDirectFallbackPair|"
        "GatewaySessionCloseClosesClientSessionBridge|"
        "ServerCloseClosesRegisteredSessions|"
        "SessionCloseCancelsActiveASRPrototypeSegment"
        ")$"
    )
    cases: list[tuple[str, list[str], Path]] = [
        (
            "go weak-network unit matrix",
            ["go", "test", ".", "-run", go_weak_tests],
            GO_GATEWAY_DIR,
        ),
        (
            "go rtc route probe unit tests",
            ["go", "test", "./cmd/rtc_route_probe", "-run", "TestClassifyICERoute"],
            GO_GATEWAY_DIR,
        ),
    ]
    if not skip_rust:
        cases.extend(
            [
                (
                    "rust native ICE URL handling",
                    [
                        "cargo",
                        "test",
                        "--manifest-path",
                        str(ROOT / "rust_client/Cargo.toml"),
                        "--features",
                        "native-webrtc",
                        "native_ice",
                    ],
                    ROOT,
                ),
                (
                    "rust downlink RTP adapter",
                    [
                        "cargo",
                        "test",
                        "--manifest-path",
                        str(ROOT / "rust_client/Cargo.toml"),
                        "--features",
                        "native-webrtc",
                        "native_downlink_rtp_packet_becomes_audio_frame",
                    ],
                    ROOT,
                ),
            ]
        )
    return cases


def live_probe_case(
    *,
    name: str,
    gateway_ws_url: str,
    robot_id: str,
    samples: int,
    sample_interval_sec: float,
    timeout_sec: float,
    force_relay: bool,
) -> tuple[str, list[str], Path]:
    cmd = [
        "go",
        "run",
        "./cmd/rtc_route_probe",
        "-ws-url",
        gateway_ws_url,
        "-robot-id",
        robot_id,
        "-client-type",
        "go_rtc_route_probe_weak_matrix",
        "-timeout",
        duration_arg(timeout_sec),
        "-connect-timeout",
        duration_arg(min(timeout_sec, 10.0)),
        "-samples",
        str(samples),
        "-sample-interval",
        duration_arg(sample_interval_sec),
    ]
    if force_relay:
        cmd.append("-force-relay")
    return name, cmd, GO_GATEWAY_DIR


def main() -> int:
    args = parse_args()
    env = command_env()
    if args.robot_secret:
        # 仅通过子进程环境传递，避免凭据出现在命令行和矩阵日志中。
        env["RTC_ROUTE_PROBE_ROBOT_SECRET"] = args.robot_secret
    cases: list[tuple[str, list[str], Path]] = []

    if not args.skip_offline:
        cases.extend(offline_cases(args.skip_rust))

    if args.live_turn_only:
        args.live_route_probe = True

    if args.live_route_probe:
        cases.append(
            live_probe_case(
                name="live route probe",
                gateway_ws_url=args.gateway_ws_url,
                robot_id=args.robot_id,
                samples=args.samples,
                sample_interval_sec=args.sample_interval_sec,
                timeout_sec=args.timeout_sec,
                force_relay=False,
            )
        )
        if args.live_turn_only:
            cases.append(
                live_probe_case(
                    name="live TURN-only route probe",
                    gateway_ws_url=args.gateway_ws_url,
                    robot_id=args.robot_id,
                    samples=args.samples,
                    sample_interval_sec=args.sample_interval_sec,
                    timeout_sec=args.timeout_sec,
                    force_relay=True,
                )
            )

    if not cases:
        print("No checks selected.", flush=True)
        return 2

    failures = 0
    for name, cmd, cwd in cases:
        if not run_case(name, cmd, cwd, env):
            failures += 1

    print(f"\nSummary: {len(cases) - failures}/{len(cases)} checks passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
