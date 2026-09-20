#!/usr/bin/env python3
"""Evaluate the configured Router 4B model as a deterministic turn-completion judge."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402


SYSTEM_PROMPT = """你是实时语音对话的用户轮次结束判断器。
判断当前用户内容是否已经语义完整，足以让助手现在开始回答。

判断为 false 的典型情况：
- 句子以“如果、因为、我觉得、我准备、应该、先把”等未完成结构结束；
- 列举、电话号码、地址、订单号等明显还没有说完；
- 用户正在犹豫、思考、重复开头、自我修正或改口；
- 当前内容需要用户继续补充才能形成完整意图。

判断为 true 的典型情况：
- 完整陈述、命令或问题；
- 结合上文后已经完整的短回答，例如“杭州”“八点”“可以”；
- 拖尾语气但语义已经结束。

只能输出一个 JSON 对象，不要解释，不要 Markdown：
{"finished":true}
或
{"finished":false}"""


def private_host(base_url: str) -> bool:
    host = urlparse(base_url).hostname or ""
    return host == "localhost" or host.startswith("127.") or host.startswith("10.")


def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def parse_finished(raw: str) -> bool:
    text = raw.strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict) and isinstance(value.get("finished"), bool):
            return value["finished"]
    except json.JSONDecodeError:
        pass
    match = re.search(r'"?finished"?\s*:\s*(true|false)', text, re.IGNORECASE)
    if match:
        return match.group(1).lower() == "true"
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    raise ValueError(f"invalid judge output: {text[:200]}")


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    base_url = config.LLM_ROUTER_BASE_URL or config.LLM_BASE_URL
    api_key = config.LLM_ROUTER_API_KEY or config.LLM_API_KEY
    model = config.LLM_ROUTER_MODEL_NAME or config.LLM_MODEL_NAME
    if not base_url or not api_key or not model:
        raise RuntimeError("Router model configuration is incomplete")
    client_kwargs: dict[str, Any] = {
        "base_url": base_url,
        "api_key": api_key,
        "timeout": args.timeout,
        "max_retries": 0,
    }
    if private_host(base_url):
        client_kwargs["http_client"] = httpx.Client(trust_env=False)
    client = OpenAI(**client_kwargs)

    cases = load_cases(args.dataset)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id", "category", "context_mode", "context", "text", "ground_truth",
        "prediction", "latency_ms", "correct", "raw", "error",
    ]
    latencies: list[float] = []
    errors = 0
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, case in enumerate(cases, 1):
            context = case.get("context", [])
            user_payload = {
                "conversation_context": context,
                "current_user_utterance": case["text"],
            }
            started = time.perf_counter()
            raw = ""
            error = ""
            prediction = "ERROR"
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                    ],
                    temperature=0,
                    max_tokens=32,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                raw = (response.choices[0].message.content or "").strip()
                prediction = "END" if parse_finished(raw) else "CONTINUE"
            except Exception as exc:
                errors += 1
                error = f"{type(exc).__name__}: {exc}"
            latency_ms = (time.perf_counter() - started) * 1000
            latencies.append(latency_ms)
            writer.writerow(
                {
                    "id": case["id"],
                    "category": case["category"],
                    "context_mode": case["context_mode"],
                    "context": json.dumps(context, ensure_ascii=False),
                    "text": case["text"],
                    "ground_truth": case["ground_truth"],
                    "prediction": prediction,
                    "latency_ms": f"{latency_ms:.3f}",
                    "correct": str(prediction == case["ground_truth"]).lower(),
                    "raw": raw,
                    "error": error,
                }
            )
            print(f"{index}/{len(cases)} {case['id']} {prediction} {latency_ms:.1f}ms")

    print(
        json.dumps(
            {
                "cases": len(cases),
                "errors": errors,
                "model": model,
                "temperature": 0,
                "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
                "latency_mean_ms": round(statistics.mean(latencies), 3),
                "latency_p50_ms": round(percentile(latencies, 0.50), 3),
                "latency_p95_ms": round(percentile(latencies, 0.95), 3),
                "latency_p99_ms": round(percentile(latencies, 0.99), 3),
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
