#!/usr/bin/env python3
"""Summarize one or more latency CSV files."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--field", action="append", required=True)
    parser.add_argument("--skip-first-per-file", action="store_true")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    rows: list[dict[str, str]] = []
    for path in args.paths:
        with path.open(newline="", encoding="utf-8") as handle:
            file_rows = list(csv.DictReader(handle))
            rows.extend(file_rows[1:] if args.skip_first_per_file else file_rows)
    print("field,n,min,mean,p50,p90,p95,p99,max")
    for field in args.field:
        values = [float(row[field]) for row in rows if row.get(field)]
        if not values:
            raise ValueError(f"no values for field: {field}")
        result = [
            field,
            str(len(values)),
            f"{min(values):.3f}",
            f"{statistics.mean(values):.3f}",
            f"{percentile(values, 0.50):.3f}",
            f"{percentile(values, 0.90):.3f}",
            f"{percentile(values, 0.95):.3f}",
            f"{percentile(values, 0.99):.3f}",
            f"{max(values):.3f}",
        ]
        print(",".join(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
