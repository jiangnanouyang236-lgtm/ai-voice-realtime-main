#!/usr/bin/env python3
"""Run repeatable TTS-Base user-simulation cases through the live voice chain."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]

CASES = (
    {
        "id": "weather_switch",
        "text": "先别说这个了，帮我查一下明天青岛的天气。",
        "tts_profile_id": "wzk-luoli",
        "expected_fragments": ("天气", "青岛"),
    },
    {
        "id": "topic_story",
        "text": "换个话题，给我讲一个关于太空的小故事。",
        "tts_profile_id": "wzk-yudazui",
        "expected_fragments": ("故事", "太空"),
    },
    {
        "id": "singing_switch",
        "text": "别唱了，换一首更轻快的歌。",
        "tts_profile_id": "wzk-shandong",
        "expected_fragments": ("唱", "歌", "换一首"),
    },
    {
        "id": "followup_question",
        "text": "请先停一下，我还有一个问题想问。",
        "tts_profile_id": "wzk-bt-7274",
        "expected_fragments": ("问题", "停一下"),
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize-connectivity", action="store_true")
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--case", action="append", choices=[item["id"] for item in CASES])
    parser.add_argument("--stt-port", type=int, default=56054)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.authorize_connectivity:
        print("Matrix refused: 需要 --authorize-connectivity")
        return 2
    selected = [case for case in CASES if not args.case or case["id"] in args.case]
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    output_dir = args.output_dir or Path("reports") / f"natural-barge-in-matrix-{run_id}"
    output_dir = output_dir if output_dir.is_absolute() else ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, object]] = []
    for index, case in enumerate(selected, start=1):
        case_dir = output_dir / f"{index:02d}-{case['id']}"
        case_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(
            {
                "NATURAL_BARGE_IN_ENABLED": "true",
                "GO_VOICE_GATEWAY_BARGE_IN_ENABLED": "true",
                "GATEWAY_BARGE_IN_ENABLED": "true",
                "RUST_LIVE_SMOKE_BARGE_IN_AFTER_PLAYBACK_MS": "200",
                "RUST_LIVE_SMOKE_QUIET_AUDIO_FRAMES": "true",
                "RUST_LIVE_SMOKE_BARGE_IN_TEXT": str(case["text"]),
                "RUST_LIVE_SMOKE_BARGE_IN_TTS_PROFILE_ID": str(case["tts_profile_id"]),
                "RUST_LIVE_SMOKE_BARGE_IN_EXPECTED_FRAGMENTS": "|".join(
                    case["expected_fragments"]
                ),
            }
        )
        command = [
            sys.executable,
            "scripts/run_dev_harness.py",
            "--scenario",
            "voice-m1-e2e",
            "--stt-port",
            str(args.stt_port),
            "--output-dir",
            str(case_dir),
            "--authorize-connectivity",
            "--authorization-reference",
            args.authorization_reference,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (case_dir / "matrix-runner.log").write_text(completed.stdout, encoding="utf-8")
        case_result: dict[str, object] = {
            **case,
            "expected_fragments": list(case["expected_fragments"]),
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "exit_code": completed.returncode,
        }
        observation_path = case_dir / "observation.json"
        if observation_path.is_file():
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            round_report = observation["stability_report"]["reports"][0]
            case_result.update(
                {
                    "classification": observation["classification"],
                    "commit": observation["commit"],
                    "dirty": observation["dirty"],
                    "observation": str(observation_path.relative_to(ROOT)),
                    "barge_in_asr_text": round_report.get("barge_in_asr_text"),
                    "barge_in_decision": round_report.get("barge_in_decision"),
                    "playback_cancel_ms": round_report.get("playback_cancel_ms"),
                    "replacement_playback_start_ms": round_report.get(
                        "replacement_playback_start_ms"
                    ),
                    "replacement_audio_frame_count": round_report.get(
                        "replacement_audio_frame_count"
                    ),
                    "done_ms": round_report.get("done_ms"),
                    "stale_audio_frame_count": round_report.get("stale_audio_frame_count"),
                    "assertions": observation.get("assertions"),
                }
            )
        results.append(case_result)
        print(f"case={case['id']} status={case_result['status']}")

    summary = {
        "schema_version": "natural-barge-in-matrix/v1",
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cases_requested": len(selected),
        "cases_passed": sum(item["status"] == "PASS" for item in results),
        "cases_failed": sum(item["status"] != "PASS" for item in results),
        "results": results,
        "limitations": [
            "TTS-Base 生成语音，不是真人现场录音",
            "本地 Rust/Go/Python 连接 LAN 服务，不是实体机器人部署验证",
            "不评测硬件 AEC，生产讯飞语音硬件模组 AEC 作为已满足前置条件",
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"matrix_summary={summary_path.relative_to(ROOT)} "
        f"passed={summary['cases_passed']}/{summary['cases_requested']}"
    )
    return 0 if summary["cases_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
