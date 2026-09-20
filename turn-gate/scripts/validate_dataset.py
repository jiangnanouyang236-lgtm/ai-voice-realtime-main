#!/usr/bin/env python3
"""Validate turn-gate JSONL manifests without third-party dependencies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


LABELS = {"END", "CONTINUE"}
SOURCES = {"synthetic_tts", "human_recorded"}
CONTEXT_MODES = {"with_context", "without_context"}


def fail(path: Path, line_no: int, message: str) -> None:
    raise ValueError(f"{path}:{line_no}: {message}")


def validate_common(path: Path, line_no: int, row: dict[str, Any]) -> None:
    for field in ("id", "category", "text", "ground_truth"):
        if field not in row:
            fail(path, line_no, f"missing field: {field}")
    if row["ground_truth"] not in LABELS:
        fail(path, line_no, f"ground_truth must be one of {sorted(LABELS)}")


def validate_smart_turn(path: Path, line_no: int, row: dict[str, Any]) -> None:
    if row.get("source") not in SOURCES:
        fail(path, line_no, f"source must be one of {sorted(SOURCES)}")
    segments = row.get("segments")
    if not isinstance(segments, list) or not segments:
        fail(path, line_no, "segments must be a non-empty list")
    candidate = row.get("candidate_after_segment")
    if not isinstance(candidate, int) or not 0 <= candidate < len(segments):
        fail(path, line_no, "candidate_after_segment is outside segments")
    silence = row.get("trailing_silence_ms")
    if not isinstance(silence, int) or silence < 0:
        fail(path, line_no, "trailing_silence_ms must be a non-negative integer")
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) not in ({"text"}, {"silence_ms"}):
            fail(path, line_no, "each segment must contain exactly text or silence_ms")


def validate_livekit(path: Path, line_no: int, row: dict[str, Any]) -> None:
    mode = row.get("context_mode")
    if mode not in CONTEXT_MODES:
        fail(path, line_no, f"context_mode must be one of {sorted(CONTEXT_MODES)}")
    context = row.get("context")
    if not isinstance(context, list):
        fail(path, line_no, "context must be a list")
    if mode == "with_context" and not context:
        fail(path, line_no, "with_context requires at least one context message")
    if mode == "without_context" and context:
        fail(path, line_no, "without_context requires an empty context list")
    for message in context:
        if not isinstance(message, dict) or message.get("role") not in {"assistant", "user"}:
            fail(path, line_no, "context message has invalid role")
        if not isinstance(message.get("text"), str) or not message["text"].strip():
            fail(path, line_no, "context message has empty text")


def validate(path: Path) -> tuple[int, dict[str, int]]:
    kind = "smart_turn" if "smart_turn" in path.parts else "livekit_eou" if "livekit_eou" in path.parts else None
    if kind is None:
        raise ValueError(f"cannot infer dataset kind from path: {path}")
    seen: set[str] = set()
    counts = {"END": 0, "CONTINUE": 0}
    total = 0
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                fail(path, line_no, f"invalid JSON: {exc.msg}")
            if not isinstance(row, dict):
                fail(path, line_no, "row must be an object")
            validate_common(path, line_no, row)
            if row["id"] in seen:
                fail(path, line_no, f"duplicate id: {row['id']}")
            seen.add(row["id"])
            if kind == "smart_turn":
                validate_smart_turn(path, line_no, row)
            else:
                validate_livekit(path, line_no, row)
            counts[row["ground_truth"]] += 1
            total += 1
    if not all(counts.values()):
        raise ValueError(f"{path}: dataset must contain both END and CONTINUE")
    return total, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.paths:
        total, counts = validate(path)
        print(f"PASS {path}: total={total} END={counts['END']} CONTINUE={counts['CONTINUE']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
