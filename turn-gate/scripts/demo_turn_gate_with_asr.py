#!/usr/bin/env python3
"""Run an isolated Smart Turn + real ASR + EOU + Router 4B turn-gate demo."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
import time
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import grpc
import httpx
import numpy as np
import onnxruntime as ort
from openai import OpenAI
from transformers import AutoTokenizer, WhisperFeatureExtractor

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
from stt import stt_service_pb2, stt_service_pb2_grpc  # noqa: E402
from test_livekit_eou import MAX_HISTORY_TOKENS, format_chat  # noqa: E402
from test_llm_turn_judge import SYSTEM_PROMPT, parse_finished, private_host  # noqa: E402
from test_smart_turn import MAX_SAMPLES, SAMPLE_RATE  # noqa: E402


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_pcm16(path: Path) -> tuple[bytes, np.ndarray]:
    with wave.open(str(path), "rb") as handle:
        if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f"{path}: expected 16kHz mono PCM16 WAV")
        pcm = handle.readframes(handle.getnframes())
    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return pcm, audio


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(char for char in text if char.isalnum())


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        current = [index]
        for other_index, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[other_index] + 1,
                    previous[other_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def latency_summary(values: list[float]) -> dict[str, float]:
    return {
        "mean": round(statistics.mean(values), 3),
        "p50": round(percentile(values, 0.50), 3),
        "p90": round(percentile(values, 0.90), 3),
        "p95": round(percentile(values, 0.95), 3),
        "p99": round(percentile(values, 0.99), 3),
        "max": round(max(values), 3),
    }


def classification(truth: list[bool], prediction: list[bool]) -> dict[str, float | int]:
    tp = sum(t and p for t, p in zip(truth, prediction))
    fp = sum(not t and p for t, p in zip(truth, prediction))
    tn = sum(not t and not p for t, p in zip(truth, prediction))
    fn = sum(t and not p for t, p in zip(truth, prediction))
    return {
        "accuracy": round((tp + tn) / len(truth), 4),
        "false_end": fp,
        "false_end_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
        "missed_end": fn,
        "missed_end_rate": round(fn / (tp + fn), 4) if tp + fn else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smart-dataset", type=Path, required=True)
    parser.add_argument("--context-dataset", type=Path, required=True)
    parser.add_argument("--smart-model", type=Path, required=True)
    parser.add_argument("--eou-model-dir", type=Path, required=True)
    parser.add_argument("--stt-target", default="127.0.0.1:50054")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    smart_cases = {row["id"]: row for row in read_jsonl(args.smart_dataset)}
    context_cases = {row["id"]: row for row in read_jsonl(args.context_dataset)}
    if set(smart_cases) != set(context_cases):
        raise ValueError("Smart and context datasets are not aligned")

    smart_options = ort.SessionOptions()
    smart_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    smart_options.inter_op_num_threads = 1
    smart_options.intra_op_num_threads = 1
    smart_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    smart_session = ort.InferenceSession(
        str(args.smart_model), sess_options=smart_options, providers=["CPUExecutionProvider"]
    )
    smart_extractor = WhisperFeatureExtractor(chunk_length=8)

    eou_options = ort.SessionOptions()
    eou_options.intra_op_num_threads = 4
    eou_options.inter_op_num_threads = 1
    eou_options.add_session_config_entry("session.dynamic_block_base", "4")
    eou_session = ort.InferenceSession(
        str(args.eou_model_dir / "onnx" / "model_q8.onnx"),
        sess_options=eou_options,
        providers=["CPUExecutionProvider"],
    )
    eou_tokenizer = AutoTokenizer.from_pretrained(
        args.eou_model_dir, local_files_only=True, truncation_side="left"
    )

    router_base_url = config.LLM_ROUTER_BASE_URL or config.LLM_BASE_URL
    router_api_key = config.LLM_ROUTER_API_KEY or config.LLM_API_KEY
    router_model = config.LLM_ROUTER_MODEL_NAME or config.LLM_MODEL_NAME
    client_kwargs: dict[str, Any] = {
        "base_url": router_base_url,
        "api_key": router_api_key,
        "timeout": 8.0,
        "max_retries": 0,
    }
    if private_host(router_base_url):
        client_kwargs["http_client"] = httpx.Client(trust_env=False)
    llm_client = OpenAI(**client_kwargs)

    stt_channel = grpc.insecure_channel(args.stt_target)
    grpc.channel_ready_future(stt_channel).result(timeout=5)
    stt_stub = stt_service_pb2_grpc.STTServiceStub(stt_channel)

    def run_smart(audio: np.ndarray) -> tuple[float, float]:
        started = time.perf_counter()
        if len(audio) > MAX_SAMPLES:
            audio = audio[-MAX_SAMPLES:]
        inputs = smart_extractor(
            audio,
            sampling_rate=SAMPLE_RATE,
            return_tensors="np",
            padding="max_length",
            max_length=MAX_SAMPLES,
            truncation=True,
            do_normalize=True,
        )
        features = np.expand_dims(inputs.input_features.squeeze(0).astype(np.float32), axis=0)
        probability = float(smart_session.run(None, {"input_features": features})[0][0].item())
        return probability, (time.perf_counter() - started) * 1000

    def run_asr(pcm: bytes) -> tuple[str, float, float, float]:
        started = time.perf_counter()
        response = stt_stub.RecognizeSpeech(
            stt_service_pb2.AudioRequest(
                audio_data=pcm,
                format="pcm",
                sample_rate=16000,
                language="zh",
            ),
            timeout=args.timeout,
        )
        total_ms = (time.perf_counter() - started) * 1000
        metadata = json.loads(response.metadata_json or "{}")
        return (
            response.text.strip(),
            total_ms,
            float(metadata.get("asr_queue_wait_ms") or 0.0),
            float(metadata.get("asr_inference_ms") or metadata.get("elapsed_ms") or 0.0),
        )

    def run_eou(context: list[dict[str, str]], text: str) -> tuple[float, float]:
        started = time.perf_counter()
        messages = [{"role": item["role"], "content": item["text"]} for item in context]
        messages.append({"role": "user", "content": text})
        formatted = format_chat(eou_tokenizer, messages)
        inputs = eou_tokenizer(
            formatted,
            add_special_tokens=False,
            return_tensors="np",
            max_length=MAX_HISTORY_TOKENS,
            truncation=True,
        )
        probability = float(
            eou_session.run(None, {"input_ids": inputs["input_ids"].astype(np.int64)})[0]
            .flatten()[-1]
        )
        return probability, (time.perf_counter() - started) * 1000

    def run_llm(context: list[dict[str, str]], text: str) -> tuple[bool, float, str]:
        started = time.perf_counter()
        response = llm_client.chat.completions.create(
            model=router_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"conversation_context": context, "current_user_utterance": text},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            max_tokens=32,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = (response.choices[0].message.content or "").strip()
        return parse_finished(raw), (time.perf_counter() - started) * 1000, raw

    fields = [
        "id", "category", "ground_truth", "expected_text", "asr_text", "normalized_match",
        "char_error_rate", "smart_probability", "eou_probability", "llm_prediction",
        "smart_ms", "asr_total_ms", "asr_queue_ms", "asr_inference_ms", "parallel_ms",
        "eou_ms", "llm_ms", "all_stages_ms", "llm_raw",
    ]
    rows: list[dict[str, Any]] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index, item_id in enumerate(smart_cases, 1):
            case = smart_cases[item_id]
            context_case = context_cases[item_id]
            audio_path = Path(case["audio_path"])
            if not audio_path.is_absolute():
                audio_path = args.smart_dataset.parent / audio_path
            pcm, audio = read_pcm16(audio_path)

            parallel_started = time.perf_counter()
            smart_future = pool.submit(run_smart, audio)
            asr_future = pool.submit(run_asr, pcm)
            smart_probability, smart_ms = smart_future.result()
            asr_text, asr_total_ms, asr_queue_ms, asr_inference_ms = asr_future.result()
            parallel_ms = (time.perf_counter() - parallel_started) * 1000
            if not asr_text:
                raise RuntimeError(f"ASR returned empty text for {item_id}")

            eou_probability, eou_ms = run_eou(context_case.get("context", []), asr_text)
            llm_finished, llm_ms, llm_raw = run_llm(context_case.get("context", []), asr_text)
            all_stages_ms = parallel_ms + eou_ms + llm_ms

            expected_norm = normalize_text(case["text"])
            actual_norm = normalize_text(asr_text)
            cer = edit_distance(expected_norm, actual_norm) / max(1, len(expected_norm))
            row = {
                "id": item_id,
                "category": case["category"],
                "ground_truth": case["ground_truth"],
                "expected_text": case["text"],
                "asr_text": asr_text,
                "normalized_match": str(expected_norm == actual_norm).lower(),
                "char_error_rate": f"{cer:.6f}",
                "smart_probability": f"{smart_probability:.9f}",
                "eou_probability": f"{eou_probability:.9f}",
                "llm_prediction": "END" if llm_finished else "CONTINUE",
                "smart_ms": f"{smart_ms:.3f}",
                "asr_total_ms": f"{asr_total_ms:.3f}",
                "asr_queue_ms": f"{asr_queue_ms:.3f}",
                "asr_inference_ms": f"{asr_inference_ms:.3f}",
                "parallel_ms": f"{parallel_ms:.3f}",
                "eou_ms": f"{eou_ms:.3f}",
                "llm_ms": f"{llm_ms:.3f}",
                "all_stages_ms": f"{all_stages_ms:.3f}",
                "llm_raw": llm_raw,
            }
            rows.append(row)
            print(
                f"{index}/{len(smart_cases)} {item_id} asr={asr_total_ms:.1f}ms "
                f"parallel={parallel_ms:.1f}ms text={asr_text[:28]}"
            )

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    truth = [row["ground_truth"] == "END" for row in rows]
    smart_end = [float(row["smart_probability"]) > 0.5 for row in rows]
    llm_end = [row["llm_prediction"] == "END" for row in rows]
    metrics: dict[str, Any] = {}
    cascade_latency: dict[str, Any] = {}
    for label, threshold in (("official_0.0066", 0.0066), ("explore_0.03", 0.03)):
        eou_end = [float(row["eou_probability"]) >= threshold for row in rows]
        majority = [sum((s, e, l)) >= 2 for s, e, l in zip(smart_end, eou_end, llm_end)]
        all_three = [s and e and l for s, e, l in zip(smart_end, eou_end, llm_end)]
        metrics[label] = {
            "eou_only": classification(truth, eou_end),
            "llm_only": classification(truth, llm_end),
            "majority_2_of_3": classification(truth, majority),
            "all_three_end": classification(truth, all_three),
        }
        calls = [s != e for s, e in zip(smart_end, eou_end)]
        latency = [
            float(row["parallel_ms"]) + float(row["eou_ms"]) + (float(row["llm_ms"]) if call else 0.0)
            for row, call in zip(rows, calls)
        ]
        cascade_latency[label] = {
            "llm_calls": sum(calls),
            "llm_call_rate": round(sum(calls) / len(calls), 4),
            **latency_summary(latency),
        }

    summary = {
        "schema_version": "turn-gate-asr-demo/v1",
        "cases": len(rows),
        "router_model": router_model,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "asr": {
            "normalized_match_rate": round(
                sum(row["normalized_match"] == "true" for row in rows) / len(rows), 4
            ),
            "mean_char_error_rate": round(
                statistics.mean(float(row["char_error_rate"]) for row in rows), 6
            ),
            "total_ms": latency_summary([float(row["asr_total_ms"]) for row in rows]),
            "inference_ms": latency_summary([float(row["asr_inference_ms"]) for row in rows]),
        },
        "stage_latency_ms": {
            "smart": latency_summary([float(row["smart_ms"]) for row in rows]),
            "parallel_smart_asr": latency_summary([float(row["parallel_ms"]) for row in rows]),
            "eou": latency_summary([float(row["eou_ms"]) for row in rows]),
            "llm": latency_summary([float(row["llm_ms"]) for row in rows]),
            "all_stages": latency_summary([float(row["all_stages_ms"]) for row in rows]),
        },
        "metrics": metrics,
        "cascade_latency_ms": cascade_latency,
        "evidence_boundary": "real ASR and Router 4B; local Smart/EOU; no VAD runtime, WebRTC, Gateway, hardware, or LLM response",
    }
    summary_path = args.output.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"output={args.output} summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
