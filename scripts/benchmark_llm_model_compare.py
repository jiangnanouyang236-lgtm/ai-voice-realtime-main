#!/usr/bin/env python3
"""Compare local vLLM LLM endpoints and optional direct LLM -> Qwen3-TTS WS.

The benchmark intentionally uses random prompt nonces and the same prompt set
for every model, so repeated runs are less likely to benefit from KV/cache
effects while still being comparable across endpoints.
"""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import queue
import random
import statistics
import sys
import threading
import time
import uuid
from typing import Any

from openai import OpenAI
import websocket

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.llm_client import QWEN3_NO_THINK_HINT, QWEN3_NO_THINKING_EXTRA_BODY  # noqa: E402
from tts.text_normalizer import clean_text_for_tts  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]

TOPICS = [
    "清晨的电梯",
    "一只会整理书架的机器人",
    "雨后的操场",
    "厨房里突然亮起的小灯",
    "迷路的风筝",
    "旧收音机里的陌生旋律",
    "一杯还没来得及喝的热茶",
    "夜晚办公室的窗户",
    "地铁站里的蓝色便利贴",
    "阳台上慢慢长大的薄荷",
    "抽屉里的备用钥匙",
    "一张没有寄出的明信片",
    "走廊尽头的绿灯",
    "正在充电的小机器人",
    "海边忽然停下来的风",
    "一本夹着车票的旧书",
]

STYLES = [
    "自然温柔",
    "轻松口语化",
    "简洁但有画面感",
    "略带悬念",
    "像朋友聊天一样",
    "安静克制",
]

TASKS = [
    "写一段一百二十到一百八十个中文字的小故事",
    "写一段一百二十到一百八十个中文字的陪伴式回应",
    "写一段一百二十到一百八十个中文字的场景描述",
    "写一段一百二十到一百八十个中文字的睡前短句",
]


@dataclass(frozen=True)
class ModelSpec:
    label: str
    base_url: str
    model: str
    api_key: str


@dataclass
class BenchResult:
    case: str
    model_label: str
    model: str
    request_id: int
    ok: bool
    prompt: str
    prompt_chars: int
    output_text: str
    output_chars: int
    first_raw_ms: float | None
    first_content_ms: float | None
    total_ms: float | None
    decode_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    end_to_end_tokens_per_s: float | None
    decode_tokens_per_s: float | None
    content_chunks: int
    reasoning_chunks: int
    raw_chunks: int
    status: str
    error: str | None = None
    tts_connect_ms: float | None = None
    tts_first_text_send_ms: float | None = None
    tts_input_done_ms: float | None = None
    tts_first_audio_ms: float | None = None
    tts_first_audio_after_text_ms: float | None = None
    tts_total_ms: float | None = None
    tts_audio_bytes: int = 0
    tts_audio_chunks: int = 0
    tts_audio_duration_ms: float | None = None
    tts_rtf: float | None = None
    first_audio_before_llm_done: bool | None = None
    first_audio_before_input_done: bool | None = None


def parse_model_spec(raw: str) -> ModelSpec:
    parts = raw.split("|")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "--model-spec must be label|base_url|model|api_key"
        )
    label, base_url, model, api_key = (part.strip() for part in parts)
    if not all((label, base_url, model, api_key)):
        raise argparse.ArgumentTypeError(
            "--model-spec fields cannot be empty: label|base_url|model|api_key"
        )
    return ModelSpec(label=label, base_url=base_url.rstrip("/"), model=model, api_key=api_key)


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, 1)


def summarize_numeric(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "avg": None, "p50": None, "p90": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "avg": round(statistics.fmean(values), 1),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "max": round(max(values), 1),
    }


def _metric_value(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize_results(results: list[BenchResult]) -> dict[str, Any]:
    grouped: dict[str, dict[str, list[BenchResult]]] = {}
    for result in results:
        grouped.setdefault(result.case, {}).setdefault(result.model_label, []).append(result)

    summary: dict[str, Any] = {}
    metrics = [
        "first_raw_ms",
        "first_content_ms",
        "total_ms",
        "decode_ms",
        "completion_tokens",
        "end_to_end_tokens_per_s",
        "decode_tokens_per_s",
        "tts_connect_ms",
        "tts_first_text_send_ms",
        "tts_input_done_ms",
        "tts_first_audio_ms",
        "tts_first_audio_after_text_ms",
        "tts_total_ms",
        "tts_audio_duration_ms",
        "tts_rtf",
    ]
    for case, by_model in grouped.items():
        summary[case] = {}
        for model_label, items in by_model.items():
            ok_items = [item for item in items if item.ok]

            def values(metric: str) -> list[float]:
                out: list[float] = []
                for item in ok_items:
                    value = _metric_value(getattr(item, metric))
                    if value is not None:
                        out.append(value)
                return out

            summary[case][model_label] = {
                "total": len(items),
                "ok": len(ok_items),
                "failed": len(items) - len(ok_items),
                "success_rate": round(len(ok_items) / len(items), 4) if items else 0.0,
                "metrics": {metric: summarize_numeric(values(metric)) for metric in metrics},
                "boolean_counts": {
                    "first_audio_before_llm_done": sum(1 for item in ok_items if item.first_audio_before_llm_done),
                    "first_audio_before_input_done": sum(1 for item in ok_items if item.first_audio_before_input_done),
                    "reasoning_seen": sum(1 for item in ok_items if item.reasoning_chunks > 0),
                },
                "errors": [item.error for item in items if item.error][:5],
            }
    return summary


def make_prompts(count: int, *, seed: int, min_chars: int, max_chars: int) -> list[str]:
    rng = random.Random(seed)
    prompts: list[str] = []
    for index in range(1, count + 1):
        topic = rng.choice(TOPICS)
        style = rng.choice(STYLES)
        task = rng.choice(TASKS)
        nonce = uuid.uuid4().hex[:10]
        prompts.append(
            f"随机性能测试 {index}-{nonce}：请用{style}的语气，围绕“{topic}”{task}。"
            f"长度控制在{min_chars}到{max_chars}个中文字。不要列清单，不要使用Markdown，直接输出正文。"
        )
    return prompts


def no_think_messages(prompt: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是用于本地模型性能测试的中文助手。请直接回答，不要解释测试过程。\n"
                f"{QWEN3_NO_THINK_HINT}"
            ),
        },
        {"role": "user", "content": prompt},
    ]


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        value = usage.get(name)
    else:
        value = getattr(usage, name, None)
    return int(value) if isinstance(value, (int, float)) else None


def _stream_llm(
    spec: ModelSpec,
    prompt: str,
    args: argparse.Namespace,
    *,
    on_text=None,
) -> dict[str, Any]:
    client = OpenAI(base_url=spec.base_url, api_key=spec.api_key, timeout=args.timeout)
    params = {
        "model": spec.model,
        "messages": no_think_messages(prompt),
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "stream": True,
        "extra_body": QWEN3_NO_THINKING_EXTRA_BODY,
        "stream_options": {"include_usage": True},
    }
    start = time.perf_counter()
    first_raw_ms = None
    first_content_ms = None
    content_parts: list[str] = []
    content_chunks = 0
    reasoning_chunks = 0
    raw_chunks = 0
    usage = None
    retried_without_stream_options = False

    try:
        try:
            stream = client.chat.completions.create(**params)
        except Exception:
            retried_without_stream_options = True
            params.pop("stream_options", None)
            stream = client.chat.completions.create(**params)

        for chunk in stream:
            raw_chunks += 1
            now_ms = (time.perf_counter() - start) * 1000.0
            if first_raw_ms is None:
                first_raw_ms = now_ms
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            reasoning = (
                getattr(delta, "reasoning", None)
                or getattr(delta, "reasoning_content", None)
                or ""
            )
            if reasoning:
                reasoning_chunks += 1
            content = getattr(delta, "content", None) or ""
            if not content:
                continue
            if first_content_ms is None:
                first_content_ms = now_ms
            content_chunks += 1
            content_parts.append(content)
            if on_text is not None:
                on_text(content)
    except Exception as exc:
        return {
            "ok": False,
            "error": repr(exc),
            "first_raw_ms": first_raw_ms,
            "first_content_ms": first_content_ms,
            "total_ms": (time.perf_counter() - start) * 1000.0,
            "output_text": "".join(content_parts),
            "content_chunks": content_chunks,
            "reasoning_chunks": reasoning_chunks,
            "raw_chunks": raw_chunks,
            "retried_without_stream_options": retried_without_stream_options,
        }

    total_ms = (time.perf_counter() - start) * 1000.0
    output_text = "".join(content_parts)
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    if completion_tokens is None and output_text:
        # Fallback approximation for servers that do not return streamed usage.
        completion_tokens = max(1, round(len(output_text) / 1.6))
    decode_ms = (
        max(0.0, total_ms - first_content_ms)
        if first_content_ms is not None
        else None
    )
    end_to_end_tps = (
        completion_tokens / (total_ms / 1000.0)
        if completion_tokens and total_ms > 0
        else None
    )
    decode_tps = (
        max(0, completion_tokens - 1) / (decode_ms / 1000.0)
        if completion_tokens and decode_ms and decode_ms > 0
        else None
    )
    return {
        "ok": bool(output_text),
        "error": None if output_text else "no content chunks",
        "first_raw_ms": first_raw_ms,
        "first_content_ms": first_content_ms,
        "total_ms": total_ms,
        "decode_ms": decode_ms,
        "output_text": output_text,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "end_to_end_tokens_per_s": end_to_end_tps,
        "decode_tokens_per_s": decode_tps,
        "content_chunks": content_chunks,
        "reasoning_chunks": reasoning_chunks,
        "raw_chunks": raw_chunks,
        "retried_without_stream_options": retried_without_stream_options,
    }


def run_llm_one(
    spec: ModelSpec,
    prompt: str,
    request_id: int,
    case: str,
    args: argparse.Namespace,
) -> BenchResult:
    data = _stream_llm(spec, prompt, args)
    return BenchResult(
        case=case,
        model_label=spec.label,
        model=spec.model,
        request_id=request_id,
        ok=bool(data.get("ok")),
        prompt=prompt,
        prompt_chars=len(prompt),
        output_text=data.get("output_text") or "",
        output_chars=len(data.get("output_text") or ""),
        first_raw_ms=_round(data.get("first_raw_ms")),
        first_content_ms=_round(data.get("first_content_ms")),
        total_ms=_round(data.get("total_ms")),
        decode_ms=_round(data.get("decode_ms")),
        prompt_tokens=data.get("prompt_tokens"),
        completion_tokens=data.get("completion_tokens"),
        total_tokens=data.get("total_tokens"),
        end_to_end_tokens_per_s=_round(data.get("end_to_end_tokens_per_s")),
        decode_tokens_per_s=_round(data.get("decode_tokens_per_s")),
        content_chunks=int(data.get("content_chunks") or 0),
        reasoning_chunks=int(data.get("reasoning_chunks") or 0),
        raw_chunks=int(data.get("raw_chunks") or 0),
        status="ok" if data.get("ok") else "error",
        error=data.get("error"),
    )


def pcm_duration_ms(audio_bytes: int, sample_rate: int) -> float:
    return audio_bytes / (sample_rate * 2) * 1000.0 if sample_rate > 0 else 0.0


def build_tts_ws_url(url: str) -> str:
    value = url.rstrip("/")
    if value.endswith("/v1/audio/speech/stream"):
        return value
    return value + "/v1/audio/speech/stream"


def run_chain_one(
    spec: ModelSpec,
    prompt: str,
    request_id: int,
    case: str,
    args: argparse.Namespace,
) -> BenchResult:
    start = time.perf_counter()
    audio = bytearray()
    audio_chunks = 0
    first_audio_ms = None
    recv_error: list[str] = []
    done = threading.Event()
    ws = None

    def elapsed_ms() -> float:
        return (time.perf_counter() - start) * 1000.0

    try:
        headers = [f"Authorization: Bearer {args.tts_api_key}"] if args.tts_api_key else []
        connect_start = time.perf_counter()
        ws = websocket.create_connection(
            build_tts_ws_url(args.tts_ws_url),
            timeout=args.tts_connect_timeout,
            header=headers,
        )
        ws.settimeout(args.timeout)
        tts_connect_ms = (time.perf_counter() - connect_start) * 1000.0
        session_config = {
            "type": "session.config",
            "model": args.tts_model,
            "voice": args.tts_voice,
            "language": args.tts_language,
            "response_format": "pcm",
            "task_type": args.tts_task_type,
            "instructions": args.tts_instructions,
            "stream_audio": True,
        }
        ws.send(json.dumps(session_config, ensure_ascii=False))

        def recv_loop() -> None:
            nonlocal audio_chunks, first_audio_ms
            try:
                while not done.is_set():
                    msg = ws.recv()
                    if isinstance(msg, bytes):
                        if not msg:
                            continue
                        if first_audio_ms is None:
                            first_audio_ms = elapsed_ms()
                        audio.extend(msg)
                        audio_chunks += 1
                        continue
                    event = json.loads(msg)
                    event_type = event.get("type")
                    if event_type in {"session.done", "response.done", "done", "completed"}:
                        done.set()
                        return
                    if event_type == "error":
                        recv_error.append(str(event.get("error") or event))
                        done.set()
                        return
            except Exception as exc:
                if not done.is_set():
                    recv_error.append(repr(exc))
                    done.set()

        reader = threading.Thread(target=recv_loop, name=f"tts-ws-reader-{request_id}", daemon=True)
        reader.start()

        first_text_send_ms = None
        input_done_ms = None

        def on_llm_text(text: str) -> None:
            nonlocal first_text_send_ms
            cleaned = clean_text_for_tts(text)
            if not cleaned:
                return
            for char in cleaned:
                if first_text_send_ms is None:
                    first_text_send_ms = elapsed_ms()
                ws.send(json.dumps({"type": "input.text", "text": char}, ensure_ascii=False))

        llm_data = _stream_llm(spec, prompt, args, on_text=on_llm_text)
        llm_done_ms = elapsed_ms()
        ws.send(json.dumps({"type": "input.done"}))
        input_done_ms = elapsed_ms()
        done.wait(args.timeout)
        done.set()
        try:
            ws.close()
        except Exception:
            pass
        reader.join(timeout=2)
        total_ms = elapsed_ms()

        output_text = llm_data.get("output_text") or ""
        if recv_error:
            raise RuntimeError(recv_error[0])
        ok = bool(output_text) and bool(audio)
        duration_ms = pcm_duration_ms(len(audio), args.tts_sample_rate)
        first_audio_after_text = (
            first_audio_ms - first_text_send_ms
            if first_audio_ms is not None and first_text_send_ms is not None
            else None
        )
        completion_tokens = llm_data.get("completion_tokens")
        decode_ms = llm_data.get("decode_ms")
        end_to_end_tps = llm_data.get("end_to_end_tokens_per_s")
        decode_tps = llm_data.get("decode_tokens_per_s")
        return BenchResult(
            case=case,
            model_label=spec.label,
            model=spec.model,
            request_id=request_id,
            ok=ok,
            prompt=prompt,
            prompt_chars=len(prompt),
            output_text=output_text,
            output_chars=len(output_text),
            first_raw_ms=_round(llm_data.get("first_raw_ms")),
            first_content_ms=_round(llm_data.get("first_content_ms")),
            total_ms=_round(llm_data.get("total_ms")),
            decode_ms=_round(decode_ms),
            prompt_tokens=llm_data.get("prompt_tokens"),
            completion_tokens=completion_tokens,
            total_tokens=llm_data.get("total_tokens"),
            end_to_end_tokens_per_s=_round(end_to_end_tps),
            decode_tokens_per_s=_round(decode_tps),
            content_chunks=int(llm_data.get("content_chunks") or 0),
            reasoning_chunks=int(llm_data.get("reasoning_chunks") or 0),
            raw_chunks=int(llm_data.get("raw_chunks") or 0),
            status="ok" if ok else "no_text_or_audio",
            error=None if ok else (llm_data.get("error") or "missing LLM text or TTS audio"),
            tts_connect_ms=_round(tts_connect_ms),
            tts_first_text_send_ms=_round(first_text_send_ms),
            tts_input_done_ms=_round(input_done_ms),
            tts_first_audio_ms=_round(first_audio_ms),
            tts_first_audio_after_text_ms=_round(first_audio_after_text),
            tts_total_ms=_round(total_ms),
            tts_audio_bytes=len(audio),
            tts_audio_chunks=audio_chunks,
            tts_audio_duration_ms=_round(duration_ms),
            tts_rtf=_round(total_ms / duration_ms if duration_ms > 0 else None),
            first_audio_before_llm_done=(
                first_audio_ms < llm_done_ms
                if first_audio_ms is not None
                else False
            ),
            first_audio_before_input_done=(
                first_audio_ms < input_done_ms
                if first_audio_ms is not None and input_done_ms is not None
                else False
            ),
        )
    except Exception as exc:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
        return BenchResult(
            case=case,
            model_label=spec.label,
            model=spec.model,
            request_id=request_id,
            ok=False,
            prompt=prompt,
            prompt_chars=len(prompt),
            output_text="",
            output_chars=0,
            first_raw_ms=None,
            first_content_ms=None,
            total_ms=_round(elapsed_ms()),
            decode_ms=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            end_to_end_tokens_per_s=None,
            decode_tokens_per_s=None,
            content_chunks=0,
            reasoning_chunks=0,
            raw_chunks=0,
            status="error",
            error=repr(exc),
        )


def _round(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value), 1)
    return None


def run_case(
    specs: list[ModelSpec],
    prompts: list[str],
    *,
    case: str,
    concurrency: int,
    runner,
    args: argparse.Namespace,
) -> list[BenchResult]:
    results: list[BenchResult] = []
    tasks = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for spec in specs:
            for index, prompt in enumerate(prompts, start=1):
                tasks.append(executor.submit(runner, spec, prompt, index, case, args))
        for future in as_completed(tasks):
            results.append(future.result())
    return sorted(results, key=lambda item: (item.case, item.model_label, item.request_id))


def write_csv(path: Path, results: list[BenchResult]) -> None:
    rows = [asdict(result) for result in results]
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def metric(summary: dict[str, Any], case: str, model: str, name: str, field: str = "p50") -> Any:
    return (
        summary.get(case, {})
        .get(model, {})
        .get("metrics", {})
        .get(name, {})
        .get(field)
    )


def write_markdown(path: Path, manifest: dict[str, Any]) -> None:
    summary = manifest["summary"]
    specs = manifest["models"]
    lines = [
        f"# LLM Model Compare - {manifest['date']}",
        "",
        f"- Label: `{manifest['label']}`",
        f"- Generated: `{manifest['generated_at']}`",
        f"- Rounds: `{manifest['rounds']}`, concurrency rounds: `{manifest['concurrency_rounds']}`",
        f"- Temperature: `{manifest['temperature']}`, max_tokens: `{manifest['max_tokens']}`",
        f"- TTS WS: `{manifest['tts']['ws_url']}`, voice `{manifest['tts']['voice']}`",
        f"- No-thinking: `extra_body.chat_template_kwargs.enable_thinking=false` plus `{QWEN3_NO_THINK_HINT}`",
        "",
        "## Models",
        "",
        "| label | base_url | model | api_key |",
        "| --- | --- | --- | --- |",
    ]
    for spec in specs:
        lines.append(f"| `{spec['label']}` | `{spec['base_url']}` | `{spec['model']}` | `<set>` |")

    lines.extend([
        "",
        "## LLM-Only Results",
        "",
        "| case | model | ok | first content p50/p95 ms | total p50/p95 ms | completion tokens p50 | decode token/s p50 | e2e token/s p50 | reasoning seen |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for case in ("llm_c1", "llm_c5"):
        for spec in specs:
            label = spec["label"]
            row = summary.get(case, {}).get(label, {})
            lines.append(
                "| "
                f"`{case}` | `{label}` | {row.get('ok', '-')}/{row.get('total', '-')} | "
                f"{fmt(metric(summary, case, label, 'first_content_ms'))}/{fmt(metric(summary, case, label, 'first_content_ms', 'p95'))} | "
                f"{fmt(metric(summary, case, label, 'total_ms'))}/{fmt(metric(summary, case, label, 'total_ms', 'p95'))} | "
                f"{fmt(metric(summary, case, label, 'completion_tokens'))} | "
                f"{fmt(metric(summary, case, label, 'decode_tokens_per_s'))} | "
                f"{fmt(metric(summary, case, label, 'end_to_end_tokens_per_s'))} | "
                f"{row.get('boolean_counts', {}).get('reasoning_seen', 0)} |"
            )

    if any(case.startswith("chain_") for case in summary):
        lines.extend([
            "",
            "## LLM -> TTS WS Results",
            "",
            "| case | model | ok | LLM first p50/p95 ms | TTS first audio p50/p95 ms | audio after text p50 ms | LLM total p50 ms | audio before LLM done | audio before input.done |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for case in ("chain_c1", "chain_c5"):
            for spec in specs:
                label = spec["label"]
                row = summary.get(case, {}).get(label, {})
                if not row:
                    continue
                lines.append(
                    "| "
                    f"`{case}` | `{label}` | {row.get('ok', '-')}/{row.get('total', '-')} | "
                    f"{fmt(metric(summary, case, label, 'first_content_ms'))}/{fmt(metric(summary, case, label, 'first_content_ms', 'p95'))} | "
                    f"{fmt(metric(summary, case, label, 'tts_first_audio_ms'))}/{fmt(metric(summary, case, label, 'tts_first_audio_ms', 'p95'))} | "
                    f"{fmt(metric(summary, case, label, 'tts_first_audio_after_text_ms'))} | "
                    f"{fmt(metric(summary, case, label, 'total_ms'))} | "
                    f"{row.get('boolean_counts', {}).get('first_audio_before_llm_done', 0)} | "
                    f"{row.get('boolean_counts', {}).get('first_audio_before_input_done', 0)} |"
                )

    lines.extend([
        "",
        "## Notes",
        "",
        "- Every model receives the same random prompt set for each case.",
        "- Prompt nonces make repeat-run cache effects less likely.",
        "- `decode token/s` uses completion tokens divided by time after first content chunk; if streamed usage is unavailable, completion tokens are approximated from Chinese output length.",
        "- The chain benchmark sends every cleaned LLM text character to the same Qwen3-TTS WS session as `input.text`, then sends exactly one `input.done`.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="Compare local vLLM LLM endpoints.")
    parser.add_argument("--date", default=today)
    parser.add_argument("--label", default="qwen3-14b-vs-qwen3-5-9b")
    parser.add_argument("--out-dir", default="tmp/llm_model_compare")
    parser.add_argument("--model-spec", action="append", type=parse_model_spec, required=True)
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--concurrency-rounds", type=int, default=10)
    parser.add_argument("--include-concurrency", action="store_true")
    parser.add_argument("--include-chain", action="store_true")
    parser.add_argument("--seed", type=int, default=624901)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--min-output-chars", type=int, default=120)
    parser.add_argument("--max-output-chars", type=int, default=180)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--tts-ws-url", default="ws://127.0.0.1:15120/v1/audio/speech/stream")
    parser.add_argument("--tts-api-key", default="")
    parser.add_argument("--tts-model", default="qwen3-tts")
    parser.add_argument("--tts-voice", default="serena")
    parser.add_argument("--tts-language", default="Chinese")
    parser.add_argument("--tts-task-type", default="CustomVoice")
    parser.add_argument("--tts-instructions", default="自然、温柔、稳定、口语化，语速适中，情绪轻微，不夸张。")
    parser.add_argument("--tts-sample-rate", type=int, default=24000)
    parser.add_argument("--tts-connect-timeout", type=float, default=8.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in args.label).strip("-")
    out_dir = ROOT / args.out_dir / f"{args.date}_{label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    specs = list(args.model_spec)
    llm_prompts = make_prompts(
        args.rounds,
        seed=args.seed,
        min_chars=args.min_output_chars,
        max_chars=args.max_output_chars,
    )
    concurrency_prompts = make_prompts(
        args.concurrency_rounds,
        seed=args.seed + 100,
        min_chars=args.min_output_chars,
        max_chars=args.max_output_chars,
    )

    results: list[BenchResult] = []
    results.extend(
        run_case(
            specs,
            llm_prompts,
            case="llm_c1",
            concurrency=1,
            runner=run_llm_one,
            args=args,
        )
    )
    if args.include_concurrency:
        results.extend(
            run_case(
                specs,
                concurrency_prompts,
                case="llm_c5",
                concurrency=5,
                runner=run_llm_one,
                args=args,
            )
        )
    if args.include_chain:
        results.extend(
            run_case(
                specs,
                llm_prompts,
                case="chain_c1",
                concurrency=1,
                runner=run_chain_one,
                args=args,
            )
        )
        if args.include_concurrency:
            results.extend(
                run_case(
                    specs,
                    concurrency_prompts,
                    case="chain_c5",
                    concurrency=5,
                    runner=run_chain_one,
                    args=args,
                )
            )

    manifest = {
        "schema_version": "llm-model-compare/v1",
        "date": args.date,
        "label": label,
        "generated_at": datetime.now().astimezone().isoformat(),
        "rounds": args.rounds,
        "concurrency_rounds": args.concurrency_rounds,
        "seed": args.seed,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "models": [
            {
                "label": spec.label,
                "base_url": spec.base_url,
                "model": spec.model,
                "api_key": "set" if spec.api_key else "missing",
            }
            for spec in specs
        ],
        "tts": {
            "ws_url": args.tts_ws_url,
            "model": args.tts_model,
            "voice": args.tts_voice,
            "sample_rate": args.tts_sample_rate,
            "api_key": "set" if args.tts_api_key else "missing",
            "submit_policy": "direct incremental input.text per cleaned character; one input.done",
        },
        "summary": summarize_results(results),
        "results": [asdict(result) for result in results],
    }
    json_path = out_dir / "manifest.json"
    csv_path = out_dir / "results.csv"
    md_path = out_dir / "README.md"
    json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(csv_path, results)
    write_markdown(md_path, manifest)
    print(json.dumps({"out_dir": str(out_dir), "summary": manifest["summary"]}, ensure_ascii=False, indent=2))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
