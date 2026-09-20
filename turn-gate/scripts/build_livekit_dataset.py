#!/usr/bin/env python3
"""Build LiveKit EOU manifests aligned with Smart Turn candidate IDs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


CONTEXT_BY_ID = {
    "st-a-001": "你想查询什么信息？",
    "st-a-002": "你想控制哪个设备？",
    "st-a-003": "你希望我什么时候提醒你？",
    "st-b-001": "如果明天下雨，你准备怎么办？",
    "st-g-001": "请告诉我你的手机号。",
    "st-g-001-end": "请告诉我你的手机号。",
    "st-g-002-mid": "服务器地址是什么？",
    "st-g-002-end": "服务器地址是什么？",
    "st-g-003-mid": "请告诉我订单号。",
    "st-g-004-mid": "请告诉我你的地址。",
    "st-j-001": "这个方案可以继续吗？",
    "st-j-002": "还需要继续吗？",
    "st-j-003": "你准备什么时候处理？",
    "st-j-004": "闹钟要设置在几点？",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_row(case: dict[str, Any], *, context_text: str | None, row_id: str) -> dict[str, Any]:
    context = [{"role": "assistant", "text": context_text}] if context_text else []
    return {
        "id": row_id,
        "pair_id": case["id"] if context_text else "",
        "category": case["category"],
        "context": context,
        "text": case["text"],
        "ground_truth": case["ground_truth"],
        "context_mode": "with_context" if context else "without_context",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smart-dataset", type=Path, required=True)
    parser.add_argument("--aligned-output", type=Path, required=True)
    parser.add_argument("--pairs-output", type=Path, required=True)
    args = parser.parse_args()

    smart_cases = read_jsonl(args.smart_dataset)
    aligned: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for case in smart_cases:
        context_text = CONTEXT_BY_ID.get(case["id"])
        aligned.append(make_row(case, context_text=context_text, row_id=case["id"]))
        if context_text:
            without = make_row(case, context_text=None, row_id=f"{case['id']}-without")
            without["pair_id"] = case["id"]
            with_context = make_row(
                case, context_text=context_text, row_id=f"{case['id']}-with"
            )
            pairs.extend([without, with_context])

    write_jsonl(args.aligned_output, aligned)
    write_jsonl(args.pairs_output, pairs)
    print(
        f"aligned={len(aligned)} with_context={sum(bool(r['context']) for r in aligned)} "
        f"context_pair_rows={len(pairs)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
