#!/usr/bin/env python3
"""Analyze Smart Turn or LiveKit EOU result CSV files."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from statistics import mean


LABELS = {"END", "CONTINUE"}
COMMON_FIELDS = {"id", "category", "ground_truth", "probability", "inference_latency_ms"}
KIND_FIELDS = {
    "smart_turn": {"text", "audio_duration_ms", "trailing_silence_ms"},
    "livekit_eou": {"context", "text"},
}
DEFAULT_THRESHOLDS = {
    "smart_turn": "0.30,0.40,0.50,0.60,0.70,0.80",
    "livekit_eou": "0.001,0.003,0.0066,0.01,0.03,0.10,0.30,0.50,0.70,0.80",
}


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else math.nan


def load_rows(path: Path, kind: str) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = (COMMON_FIELDS | KIND_FIELDS[kind]) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing CSV fields: {', '.join(sorted(missing))}")
        rows = list(reader)
    if not rows:
        raise ValueError("result CSV is empty")
    for index, row in enumerate(rows, 2):
        if row["ground_truth"] not in LABELS:
            raise ValueError(f"line {index}: invalid ground_truth")
        probability = float(row["probability"])
        latency = float(row["inference_latency_ms"])
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f"line {index}: probability must be in [0, 1]")
        if latency < 0:
            raise ValueError(f"line {index}: inference_latency_ms must be non-negative")
    return rows


def metrics(rows: list[dict[str, str]], threshold: float) -> dict[str, float | int]:
    tp = fp = tn = fn = 0
    latencies: list[float] = []
    for row in rows:
        truth_end = row["ground_truth"] == "END"
        predicted_end = float(row["probability"]) >= threshold
        latencies.append(float(row["inference_latency_ms"]))
        if truth_end and predicted_end:
            tp += 1
        elif not truth_end and predicted_end:
            fp += 1
        elif not truth_end and not predicted_end:
            tn += 1
        else:
            fn += 1
    precision = safe_ratio(tp, tp + fp)
    recall = safe_ratio(tp, tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else math.nan
    return {
        "n": len(rows),
        "accuracy": safe_ratio(tp + tn, len(rows)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false_end_rate": safe_ratio(fp, fp + tn),
        "missed_end_rate": safe_ratio(fn, tp + fn),
        "latency_mean_ms": mean(latencies),
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p90_ms": percentile(latencies, 0.90),
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_p99_ms": percentile(latencies, 0.99),
    }


def fmt(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    return "NA" if math.isnan(value) else f"{value:.4f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=sorted(KIND_FIELDS), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--thresholds")
    args = parser.parse_args()
    threshold_text = args.thresholds or DEFAULT_THRESHOLDS[args.kind]
    thresholds = [float(item) for item in threshold_text.split(",")]
    if not thresholds or any(not 0.0 <= item <= 1.0 for item in thresholds):
        raise ValueError("thresholds must be in [0, 1]")
    rows = load_rows(args.input, args.kind)
    headers = [
        "threshold", "n", "accuracy", "precision", "recall", "f1",
        "false_end_rate", "missed_end_rate", "latency_p50_ms",
        "latency_p90_ms", "latency_p95_ms", "latency_p99_ms",
    ]
    print(",".join(headers))
    for threshold in thresholds:
        result = metrics(rows, threshold)
        values = {"threshold": threshold, **result}
        print(",".join(fmt(values[name]) for name in headers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
