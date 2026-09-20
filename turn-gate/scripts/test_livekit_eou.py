#!/usr/bin/env python3
"""Run the official LiveKit multilingual EOU ONNX semantics on a JSONL dataset."""

from __future__ import annotations

import argparse
import csv
import json
import re
import resource
import time
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from transformers import AutoTokenizer


MAX_HISTORY_TOKENS = 128
MAX_HISTORY_TURNS = 6


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.lower())
    text = "".join(
        char
        for char in text
        if not (unicodedata.category(char).startswith("P") and char not in ["'", "-"])
    )
    return re.sub(r"\s+", " ", text).strip()


def format_chat(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    normalized: list[dict[str, str]] = []
    for original in messages[-MAX_HISTORY_TURNS:]:
        content = normalize_text(original["content"])
        if not content:
            continue
        if normalized and normalized[-1]["role"] == original["role"]:
            normalized[-1]["content"] += f" {content}"
        else:
            normalized.append({"role": original["role"], "content": content})
    text = tokenizer.apply_chat_template(
        normalized,
        add_generation_prompt=False,
        add_special_tokens=False,
        tokenize=False,
    )
    marker = text.rfind("<|im_end|>")
    if marker < 0:
        raise ValueError("tokenizer chat template did not produce <|im_end|>")
    return text[:marker]


def peak_rss_mb() -> float:
    # macOS reports ru_maxrss in bytes; Linux reports KiB.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if __import__("sys").platform == "darwin" else rss / 1024


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row.get("ground_truth") not in {"END", "CONTINUE"}:
                raise ValueError(f"{path}:{line_no}: invalid ground_truth")
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.0066)
    args = parser.parse_args()

    if not 0 <= args.threshold <= 1:
        raise ValueError("threshold must be in [0, 1]")
    model_path = args.model_dir / "onnx" / "model_q8.onnx"
    load_started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_dir,
        local_files_only=True,
        truncation_side="left",
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.dynamic_block_base", "4")
    session = ort.InferenceSession(
        str(model_path),
        providers=["CPUExecutionProvider"],
        sess_options=options,
    )
    load_ms = (time.perf_counter() - load_started) * 1000

    cases = load_cases(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id", "pair_id", "category", "context_mode", "context", "text",
        "ground_truth", "prediction", "probability", "preprocess_latency_ms",
        "inference_latency_ms", "total_latency_ms", "correct",
    ]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            total_started = time.perf_counter()
            preprocess_started = total_started
            messages = [
                {"role": item["role"], "content": item["text"]}
                for item in case.get("context", [])
            ]
            messages.append({"role": "user", "content": case["text"]})
            formatted = format_chat(tokenizer, messages)
            inputs = tokenizer(
                formatted,
                add_special_tokens=False,
                return_tensors="np",
                max_length=MAX_HISTORY_TOKENS,
                truncation=True,
            )
            preprocess_latency_ms = (time.perf_counter() - preprocess_started) * 1000
            started = time.perf_counter()
            outputs = session.run(None, {"input_ids": inputs["input_ids"].astype(np.int64)})
            latency_ms = (time.perf_counter() - started) * 1000
            total_latency_ms = (time.perf_counter() - total_started) * 1000
            probability = float(outputs[0].flatten()[-1])
            prediction = "END" if probability >= args.threshold else "CONTINUE"
            writer.writerow(
                {
                    "id": case["id"],
                    "pair_id": case.get("pair_id", ""),
                    "category": case["category"],
                    "context_mode": case["context_mode"],
                    "context": json.dumps(case.get("context", []), ensure_ascii=False),
                    "text": case["text"],
                    "ground_truth": case["ground_truth"],
                    "prediction": prediction,
                    "probability": f"{probability:.9f}",
                    "preprocess_latency_ms": f"{preprocess_latency_ms:.3f}",
                    "inference_latency_ms": f"{latency_ms:.3f}",
                    "total_latency_ms": f"{total_latency_ms:.3f}",
                    "correct": str(prediction == case["ground_truth"]).lower(),
                }
            )
    print(
        json.dumps(
            {
                "cases": len(cases),
                "threshold": args.threshold,
                "model_load_ms": round(load_ms, 3),
                "peak_rss_mb": round(peak_rss_mb(), 3),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
