#!/usr/bin/env python3
"""Run Smart Turn V3.2 CPU ONNX on manifest rows containing audio_path."""

from __future__ import annotations

import argparse
import csv
import json
import resource
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from transformers import WhisperFeatureExtractor


SAMPLE_RATE = 16000
MAX_SAMPLES = 8 * SAMPLE_RATE


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if __import__("sys").platform == "darwin" else rss / 1024


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if channels != 1 or width != 2 or rate != SAMPLE_RATE:
        raise ValueError(f"{path}: expected 16kHz mono PCM16 WAV")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    return audio, rate


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            row = json.loads(raw)
            if not row.get("audio_path"):
                raise ValueError(f"{path}:{line_no}: audio_path is required for inference")
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()

    load_started = time.perf_counter()
    options = ort.SessionOptions()
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    session = ort.InferenceSession(str(args.model), sess_options=options, providers=["CPUExecutionProvider"])
    extractor = WhisperFeatureExtractor(chunk_length=8)
    load_ms = (time.perf_counter() - load_started) * 1000

    cases = load_cases(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id", "category", "source", "text", "ground_truth", "prediction", "probability",
        "audio_duration_ms", "trailing_silence_ms", "preprocess_latency_ms",
        "inference_latency_ms", "total_latency_ms", "correct",
    ]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            audio_path = Path(case["audio_path"])
            if not audio_path.is_absolute():
                audio_path = args.dataset.parent / audio_path
            audio, _ = load_wav(audio_path)
            duration_ms = len(audio) / SAMPLE_RATE * 1000
            if len(audio) > MAX_SAMPLES:
                audio = audio[-MAX_SAMPLES:]
            total_started = time.perf_counter()
            preprocess_started = total_started
            inputs = extractor(
                audio,
                sampling_rate=SAMPLE_RATE,
                return_tensors="np",
                padding="max_length",
                max_length=MAX_SAMPLES,
                truncation=True,
                do_normalize=True,
            )
            input_features = np.expand_dims(inputs.input_features.squeeze(0).astype(np.float32), axis=0)
            preprocess_latency_ms = (time.perf_counter() - preprocess_started) * 1000
            started = time.perf_counter()
            outputs = session.run(None, {"input_features": input_features})
            latency_ms = (time.perf_counter() - started) * 1000
            total_latency_ms = (time.perf_counter() - total_started) * 1000
            probability = float(outputs[0][0].item())
            prediction = "END" if probability > args.threshold else "CONTINUE"
            writer.writerow(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "source": case["source"],
                    "text": case["text"],
                    "ground_truth": case["ground_truth"],
                    "prediction": prediction,
                    "probability": f"{probability:.9f}",
                    "audio_duration_ms": f"{duration_ms:.3f}",
                    "trailing_silence_ms": case["trailing_silence_ms"],
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
