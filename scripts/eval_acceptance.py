#!/usr/bin/env python3
"""Run only independently authored acceptance Gold; never reads provisional cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import eval_runner, eval_vision_e2e, validate_eval_gold

GOLD_DIR = ROOT / "data" / "eval_gold"


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _checkout_state() -> dict[str, str]:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    diff = subprocess.check_output(["git", "diff", "--binary"], cwd=ROOT)
    return {"commit": commit, "working_tree_diff_sha256": _sha256_bytes(diff)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立 Gold 验收评测")
    parser.add_argument("--allow-owner-pending", action="store_true", help="仅诊断；报告不得作为发布验收")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    # Successful request-per-case logs are preserved by the structured report;
    # repeating them on stderr adds noise and consumes AI review context.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    validation = validate_eval_gold.validate_gold()
    if not validation["valid"]:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 1
    if not validation["acceptance_ready"] and not args.allow_owner_pending:
        print("ERROR: Gold 尚未 owner_approved；如仅诊断可显式使用 --allow-owner-pending")
        return 3

    router_client = eval_runner._openai_client(
        eval_runner.ROUTER_BASE_URL, eval_runner.ROUTER_API_KEY, max_retries=0
    )
    tool_client = eval_runner._openai_client(
        eval_runner.GEN_BASE_URL, eval_runner.GEN_API_KEY, max_retries=0
    )
    router_results = [
        eval_runner.run_4b_router(router_client, "data/eval_gold/4b_router.jsonl", case)
        for case in _load(GOLD_DIR / "4b_router.jsonl")
    ]
    tool_results = [
        eval_runner.run_9b_tool(tool_client, "data/eval_gold/9b_tool.jsonl", case)
        for case in _load(GOLD_DIR / "9b_tool.jsonl")
    ]
    vision_results = []
    for case in _load(GOLD_DIR / "vision" / "cases.jsonl"):
        image_path = GOLD_DIR / case["image_path"]
        vision_results.append(
            eval_vision_e2e.run_vision_e2e(
                router_client,
                tool_client,
                case,
                eval_vision_e2e.load_image_data_url(image_path),
            )
        )
    vision_summary = {
        "name": "vision",
        "total": len(vision_results),
        "passed": sum(1 for item in vision_results if item["ok"]),
        "failed": sum(1 for item in vision_results if not item["ok"]),
        "results": vision_results,
    }
    report = {
        "schema_version": "eval-acceptance-report/v1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "acceptance_ready": validation["acceptance_ready"],
        "diagnostic_only": not validation["acceptance_ready"],
        "checkout": _checkout_state(),
        "runtime": {
            "router_model": eval_runner.ROUTER_MODEL,
            "tool_vision_model": eval_runner.GEN_MODEL,
            "router_endpoint": eval_runner.ROUTER_BASE_URL,
            "tool_vision_endpoint": eval_runner.GEN_BASE_URL,
            "tool_schema_sha256": _sha256_bytes(
                json.dumps(
                    eval_runner.EVAL_TOOL_SCHEMAS,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            "harness_source_sha256": _sha256_bytes(
                b"".join(
                    path.read_bytes()
                    for path in (
                        ROOT / "scripts" / "eval_acceptance.py",
                        ROOT / "scripts" / "eval_runner.py",
                        ROOT / "scripts" / "eval_vision_e2e.py",
                        ROOT / "scripts" / "validate_eval_gold.py",
                    )
                )
            ),
        },
        "gold": validation,
        "reports": [
            eval_runner.summarize_4b_results(router_results),
            eval_runner.summarize_9b_results(tool_results),
            vision_summary,
        ],
    }
    output_path = Path(args.output)
    resolved = output_path.resolve() if output_path.is_absolute() else (ROOT / output_path).resolve()
    if not resolved.is_relative_to((ROOT / "reports").resolve()):
        print("ERROR: output 必须位于 reports/")
        return 2
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"router={report['reports'][0]['passed']}/{report['reports'][0]['total']} "
        f"tool={report['reports'][1]['passed']}/{report['reports'][1]['total']} "
        f"vision={report['reports'][2]['passed']}/{report['reports'][2]['total']} "
        f"diagnostic_only={report['diagnostic_only']}"
    )
    return 0 if all(item["failed"] == 0 for item in report["reports"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
