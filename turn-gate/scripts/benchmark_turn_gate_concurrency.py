#!/usr/bin/env python3
"""Benchmark shared Smart Turn and LiveKit EOU instances under concurrency."""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from gateway.turn_gate_shadow_models import get_turn_gate_shadow_models


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--text", default="帮我查一下今天青岛的天气")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=8)
    return parser.parse_args()


def benchmark(
    name: str,
    fn: Callable[[], dict[str, Any]],
    *,
    workers: int,
    repeats: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: fn(), range(repeats)))
    wall_ms = (time.perf_counter() - started) * 1000
    probabilities = [float(item["probability"]) for item in results if item.get("status") == "ok"]
    totals = [float(item.get("total_ms") or 0) for item in results]
    return {
        "name": name,
        "workers": workers,
        "repeats": repeats,
        "wall_ms": round(wall_ms, 3),
        "throughput_rps": round(repeats / (wall_ms / 1000), 3),
        "ok": len(probabilities),
        "errors": [item.get("error") for item in results if item.get("status") != "ok"],
        "probability_min": min(probabilities) if probabilities else None,
        "probability_max": max(probabilities) if probabilities else None,
        "total_ms_mean": round(statistics.mean(totals), 3),
        "total_ms_max": round(max(totals), 3),
    }


def main() -> int:
    args = parse_args()
    if args.workers <= 0 or args.repeats <= 0:
        raise SystemExit("--workers and --repeats must be positive")
    wav_data = args.wav.read_bytes()
    history = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，请问有什么可以帮你？"},
    ]
    models = get_turn_gate_shadow_models()
    warmup = {
        "smart": models.run_smart(wav_data),
        "eou": models.run_eou(history, args.text),
    }
    runs = []
    for workers in (1, args.workers):
        runs.append(
            benchmark(
                "smart",
                lambda: models.run_smart(wav_data),
                workers=workers,
                repeats=args.repeats,
            )
        )
        runs.append(
            benchmark(
                "eou",
                lambda: models.run_eou(history, args.text),
                workers=workers,
                repeats=args.repeats,
            )
        )
    valid = all(
        run["ok"] == args.repeats
        and run["probability_min"] == run["probability_max"]
        for run in runs
    )
    print(
        json.dumps(
            {
                "schema_version": "turn-gate-concurrency/v1",
                "platform": platform.platform(),
                "wav": str(args.wav),
                "workers": args.workers,
                "repeats": args.repeats,
                "warmup": warmup,
                "runs": runs,
                "deterministic_and_error_free": valid,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
