#!/usr/bin/env python3
"""Benchmark current Qwen realtime TTS path and local vLLM TTS candidates.

The script intentionally keeps provider calls outside the product path. It is
for evidence gathering before choosing whether to keep Qwen Realtime, switch to
local vLLM TTS, or run a hybrid policy.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time
import uuid
import wave
from typing import Any, Iterable

import grpc
import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tts import tts_service_pb2, tts_service_pb2_grpc  # noqa: E402


TEXT_POOL = [
    "今天的天气真的很好，适合出去散步。",
    "我叫小明，来自北京，正在学习计算机。",
    "人工智能技术正在改变我们的工作方式。",
    "请你帮我查一下明天青岛的天气。",
    "这个产品的体验不错，但还有一些细节需要继续优化。",
    "从前有一只小兔子，它最喜欢在森林里探索新鲜事物。",
    "小游戏完成，总分二分，有一些表现值得继续观察。",
    "记录显示近期事件遗忘从不，重复提问从不，词汇寻找困难经常。",
    "这不是医学诊断，只作为日常参考，后续可以持续观察。",
    "如果你准备好了，我们现在开始测试语音合成的首音延迟和稳定性。",
]


@dataclass
class BenchResult:
    provider: str
    request_id: int
    ok: bool
    text: str
    text_chars: int
    ttfb_ms: float | None
    total_ms: float | None
    audio_bytes: int
    audio_duration_ms: float | None
    rtf: float | None
    chunks: int
    sample_rate: int | None
    connect_ms: float | None = None
    session_ready_ms: float | None = None
    input_done_ms: float | None = None
    first_audio_after_input_done_ms: float | None = None
    first_audio_before_input_done: bool | None = None
    status: int | str | None = None
    error: str | None = None


def generate_texts(rounds: int, *, seed: int, min_chars: int, max_chars: int) -> list[str]:
    rng = random.Random(seed)
    texts: list[str] = []
    for _ in range(rounds):
        parts: list[str] = []
        while len("".join(parts)) < min_chars:
            parts.append(rng.choice(TEXT_POOL))
            if len("".join(parts)) >= max_chars:
                break
        text = "".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip("，。；,.!?！？")
            if not text:
                text = "".join(parts)[:max_chars]
            if text[-1] not in "。！？；.!?;":
                text = f"{text}。" if len(text) < max_chars else f"{text[:-1]}。"
        texts.append(text)
    return texts


def split_text(text: str, chunk_chars: int) -> list[str]:
    chunk_chars = max(1, int(chunk_chars))
    return [text[index : index + chunk_chars] for index in range(0, len(text), chunk_chars)]


def pcm_duration_ms(audio_bytes: int, sample_rate: int, *, sample_width: int = 2, channels: int = 1) -> float:
    if sample_rate <= 0 or sample_width <= 0 or channels <= 0:
        return 0.0
    return audio_bytes / (sample_rate * sample_width * channels) * 1000.0


def wav_duration_ms(audio_bytes: bytes | bytearray) -> float | None:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            sample_rate = wav_file.getframerate()
            if sample_rate <= 0:
                return None
            return wav_file.getnframes() / sample_rate * 1000.0
    except (EOFError, wave.Error):
        return None


def build_http_speech_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith("/v1/audio/speech"):
        return url
    if url.endswith("/v1/audio"):
        return f"{url}/speech"
    if url.endswith("/v1"):
        return f"{url}/audio/speech"
    return f"{url}/v1/audio/speech"


def build_ws_speech_stream_url(ws_url: str) -> str:
    url = ws_url.rstrip("/")
    if url.endswith("/v1/audio/speech/stream"):
        return url
    if url.endswith("/v1/audio/speech"):
        return f"{url}/stream"
    if url.endswith("/v1/audio"):
        return f"{url}/speech/stream"
    if url.endswith("/v1"):
        return f"{url}/audio/speech/stream"
    return f"{url}/v1/audio/speech/stream"


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


def summarize_numeric(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "avg": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "avg": round(statistics.fmean(values), 1),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": round(max(values), 1),
    }


def summarize_results(results: list[BenchResult]) -> dict[str, Any]:
    ok_results = [result for result in results if result.ok]
    failed_results = [result for result in results if not result.ok]

    def metric_values(name: str) -> list[float]:
        values = []
        for result in ok_results:
            value = getattr(result, name)
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    return {
        "total": len(results),
        "ok": len(ok_results),
        "failed": len(failed_results),
        "success_rate": round(len(ok_results) / len(results), 4) if results else 0.0,
        "metrics": {
            "ttfb_ms": summarize_numeric(metric_values("ttfb_ms")),
            "total_ms": summarize_numeric(metric_values("total_ms")),
            "audio_duration_ms": summarize_numeric(metric_values("audio_duration_ms")),
            "rtf": summarize_numeric(metric_values("rtf")),
            "chunks": summarize_numeric(metric_values("chunks")),
            "audio_bytes": summarize_numeric(metric_values("audio_bytes")),
            "connect_ms": summarize_numeric(metric_values("connect_ms")),
            "session_ready_ms": summarize_numeric(metric_values("session_ready_ms")),
            "input_done_ms": summarize_numeric(metric_values("input_done_ms")),
            "first_audio_after_input_done_ms": summarize_numeric(metric_values("first_audio_after_input_done_ms")),
        },
        "boolean_counts": {
            "first_audio_before_input_done": sum(
                1 for result in ok_results if result.first_audio_before_input_done is True
            ),
        },
        "errors": [result.error for result in failed_results[:10] if result.error],
    }


def get_api_key(args: argparse.Namespace) -> str | None:
    if args.api_key:
        return args.api_key
    if args.api_key_env:
        return os.getenv(args.api_key_env)
    return (
        os.getenv("TTS_BENCH_API_KEY")
        or os.getenv("QWEN3_TTS_CUSTOM_VOICE_API_KEY")
        or os.getenv("TTS_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
    )


def result_from_error(provider: str, request_id: int, text: str, start: float, error: Exception) -> BenchResult:
    return BenchResult(
        provider=provider,
        request_id=request_id,
        ok=False,
        text=text,
        text_chars=len(text),
        ttfb_ms=None,
        total_ms=(time.perf_counter() - start) * 1000.0,
        audio_bytes=0,
        audio_duration_ms=None,
        rtf=None,
        chunks=0,
        sample_rate=None,
        error=repr(error),
    )


def run_current_grpc_one(args: argparse.Namespace, text: str, request_id: int) -> BenchResult:
    provider = "current-grpc"
    start = time.perf_counter()
    try:
        channel = grpc.insecure_channel(args.grpc_target)
        stub = tts_service_pb2_grpc.TTSServiceStub(channel)
        session_id = f"bench-{uuid.uuid4()}"

        def request_iter() -> Iterable[tts_service_pb2.TextChunk]:
            chunks = split_text(text, args.input_chunk_chars)
            for index, chunk in enumerate(chunks):
                kwargs = {
                    "text": chunk,
                    "is_final": False,
                    "session_id": session_id,
                }
                if index == 0:
                    kwargs["config"] = tts_service_pb2.TTSConfig(tts_profile_id=args.tts_profile_id)
                yield tts_service_pb2.TextChunk(**kwargs)
                if args.input_chunk_delay_ms > 0:
                    time.sleep(args.input_chunk_delay_ms / 1000.0)
            yield tts_service_pb2.TextChunk(text="", is_final=True, session_id=session_id)

        ttfb_ms = None
        audio_bytes = 0
        chunks = 0
        sample_rate = None
        for audio_chunk in stub.StreamTextToSpeech(request_iter(), timeout=args.timeout):
            if audio_chunk.is_final:
                continue
            if audio_chunk.audio_data and ttfb_ms is None:
                ttfb_ms = (time.perf_counter() - start) * 1000.0
            if audio_chunk.audio_data:
                chunks += 1
                audio_bytes += len(audio_chunk.audio_data)
                sample_rate = audio_chunk.sample_rate or sample_rate

        total_ms = (time.perf_counter() - start) * 1000.0
        sample_rate = sample_rate or args.grpc_sample_rate
        duration_ms = pcm_duration_ms(audio_bytes, sample_rate)
        rtf = total_ms / duration_ms if duration_ms > 0 else None
        return BenchResult(
            provider=provider,
            request_id=request_id,
            ok=audio_bytes > 0,
            text=text,
            text_chars=len(text),
            ttfb_ms=ttfb_ms,
            total_ms=total_ms,
            audio_bytes=audio_bytes,
            audio_duration_ms=duration_ms,
            rtf=rtf,
            chunks=chunks,
            sample_rate=sample_rate,
            error=None if audio_bytes > 0 else "no audio bytes returned",
        )
    except Exception as exc:
        return result_from_error(provider, request_id, text, start, exc)


async def run_vllm_http_one(args: argparse.Namespace, text: str, request_id: int, *, stream: bool) -> BenchResult:
    provider = "vllm-http-stream" if stream else "vllm-http-wav"
    start = time.perf_counter()
    api_key = get_api_key(args)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": args.model,
        "input": text,
        "voice": args.voice,
        "language": args.language,
        "response_format": "pcm" if stream else "wav",
        "speed": args.speech_rate,
        "task_type": args.task_type,
    }
    if args.instructions:
        payload["instructions"] = args.instructions
    if stream:
        payload["stream"] = True

    try:
        async with httpx.AsyncClient(timeout=args.timeout) as client:
            async with client.stream("POST", build_http_speech_url(args.base_url), headers=headers, json=payload) as resp:
                ttfb_ms = None
                chunks = 0
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    if ttfb_ms is None:
                        ttfb_ms = (time.perf_counter() - start) * 1000.0
                    chunks += 1
                    data.extend(chunk)
                total_ms = (time.perf_counter() - start) * 1000.0
                ok = 200 <= resp.status_code < 300 and bool(data)
                audio_bytes = len(data) if ok else 0
                if stream and ok:
                    duration_ms = pcm_duration_ms(audio_bytes, args.vllm_sample_rate)
                elif ok:
                    duration_ms = wav_duration_ms(data)
                else:
                    duration_ms = None
                rtf = total_ms / duration_ms if duration_ms else None
                return BenchResult(
                    provider=provider,
                    request_id=request_id,
                    ok=ok,
                    text=text,
                    text_chars=len(text),
                    ttfb_ms=ttfb_ms,
                    total_ms=total_ms,
                    audio_bytes=audio_bytes,
                    audio_duration_ms=duration_ms,
                    rtf=rtf,
                    chunks=chunks,
                    sample_rate=args.vllm_sample_rate if stream else None,
                    status=resp.status_code,
                    error=None if ok else data[:300].decode("utf-8", errors="ignore"),
                )
    except Exception as exc:
        return result_from_error(provider, request_id, text, start, exc)


async def run_vllm_ws_one(args: argparse.Namespace, text: str, request_id: int) -> BenchResult:
    provider = "vllm-ws-stream"
    start = time.perf_counter()
    api_key = get_api_key(args)
    headers = [("Authorization", f"Bearer {api_key}")] if api_key else []
    pcm = bytearray()
    chunks = 0
    ttfb_ms = None
    connect_ms = None

    try:
        connect_start = time.perf_counter()
        async with websockets.connect(
            build_ws_speech_stream_url(args.ws_url),
            additional_headers=headers,
            max_size=None,
            open_timeout=args.timeout,
        ) as ws:
            connect_ms = (time.perf_counter() - connect_start) * 1000.0
            session_config = {
                "type": "session.config",
                "model": args.model,
                "voice": args.voice,
                "language": args.language,
                "response_format": "pcm",
                "task_type": args.task_type,
                "instructions": args.instructions,
                "stream_audio": True,
            }
            if args.ws_split_granularity:
                session_config["split_granularity"] = args.ws_split_granularity
            await ws.send(json.dumps(session_config, ensure_ascii=False))
            for part in split_text(text, args.input_chunk_chars):
                await ws.send(json.dumps({"type": "input.text", "text": part}, ensure_ascii=False))
                if args.input_chunk_delay_ms > 0:
                    await asyncio.sleep(args.input_chunk_delay_ms / 1000.0)
            await ws.send(json.dumps({"type": "input.done"}))

            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
                if isinstance(msg, bytes):
                    if msg:
                        if ttfb_ms is None:
                            ttfb_ms = (time.perf_counter() - start) * 1000.0
                        chunks += 1
                        pcm.extend(msg)
                    continue
                event = json.loads(msg)
                event_type = event.get("type")
                delta = event.get("delta") or event.get("audio") or event.get("data")
                if event_type in {"response.audio.delta", "audio.delta"} and delta:
                    data = base64.b64decode(delta)
                    if data:
                        if ttfb_ms is None:
                            ttfb_ms = (time.perf_counter() - start) * 1000.0
                        chunks += 1
                        pcm.extend(data)
                    continue
                if event_type in {"session.done", "response.done", "done", "completed"}:
                    break
                if event_type == "error":
                    raise RuntimeError(event)

        total_ms = (time.perf_counter() - start) * 1000.0
        duration_ms = pcm_duration_ms(len(pcm), args.vllm_sample_rate)
        return BenchResult(
            provider=provider,
            request_id=request_id,
            ok=bool(pcm),
            text=text,
            text_chars=len(text),
            ttfb_ms=ttfb_ms,
            total_ms=total_ms,
            audio_bytes=len(pcm),
            audio_duration_ms=duration_ms,
            rtf=total_ms / duration_ms if duration_ms > 0 else None,
            chunks=chunks,
            sample_rate=args.vllm_sample_rate,
            connect_ms=connect_ms,
            error=None if pcm else "no audio bytes returned",
        )
    except Exception as exc:
        result = result_from_error(provider, request_id, text, start, exc)
        result.connect_ms = connect_ms
        return result


def run_qwen_realtime_sdk_one(args: argparse.Namespace, text: str, request_id: int) -> BenchResult:
    provider = f"qwen-realtime-sdk-{args.qwen_mode}"
    start = time.perf_counter()
    try:
        import dashscope
        from dashscope.audio.qwen_tts_realtime import (
            AudioFormat,
            QwenTtsRealtime,
            QwenTtsRealtimeCallback,
        )

        api_key = get_api_key(args)
        if api_key:
            dashscope.api_key = api_key

        class Callback(QwenTtsRealtimeCallback):
            def __init__(self) -> None:
                super().__init__()
                import threading

                self.first_audio_event = threading.Event()
                self.complete_event = threading.Event()
                self.audio = bytearray()
                self.chunks = 0
                self.error: Any = None
                self.first_audio_ms: float | None = None

            def on_event(self, response: dict) -> None:
                event_type = response.get("type")
                if event_type == "response.audio.delta":
                    delta = response.get("delta") or ""
                    if delta:
                        data = base64.b64decode(delta)
                        if self.first_audio_ms is None:
                            self.first_audio_ms = (time.perf_counter() - start) * 1000.0
                        self.audio.extend(data)
                        self.chunks += 1
                        self.first_audio_event.set()
                elif event_type == "session.finished":
                    self.complete_event.set()
                elif event_type == "error":
                    self.error = response.get("error") or response
                    self.complete_event.set()

            def on_close(self, close_status_code, close_msg) -> None:
                self.complete_event.set()

        callback = Callback()
        qwen_tts = QwenTtsRealtime(model=args.model, callback=callback, url=args.qwen_url)
        connect_start = time.perf_counter()
        qwen_tts.connect()
        connect_ms = (time.perf_counter() - connect_start) * 1000.0
        session_start = time.perf_counter()
        qwen_tts.update_session(
            voice=args.voice,
            response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            mode=args.qwen_mode,
            speech_rate=float(args.speech_rate),
        )
        session_ready_ms = (time.perf_counter() - session_start) * 1000.0

        for part in split_text(text, args.input_chunk_chars):
            qwen_tts.append_text(part)
            if args.qwen_mode == "commit":
                qwen_tts.commit()
            if args.input_chunk_delay_ms > 0:
                time.sleep(args.input_chunk_delay_ms / 1000.0)
        input_done_ms = (time.perf_counter() - start) * 1000.0
        qwen_tts.finish()

        got_first = callback.first_audio_event.wait(args.timeout)
        ttfb_ms = callback.first_audio_ms if got_first else None
        if not args.first_audio_only:
            callback.complete_event.wait(args.timeout)
        qwen_tts.close()

        if callback.error:
            raise RuntimeError(callback.error)
        total_ms = (time.perf_counter() - start) * 1000.0
        duration_ms = pcm_duration_ms(len(callback.audio), 24000)
        return BenchResult(
            provider=provider,
            request_id=request_id,
            ok=bool(callback.audio),
            text=text,
            text_chars=len(text),
            ttfb_ms=ttfb_ms,
            total_ms=total_ms,
            audio_bytes=len(callback.audio),
            audio_duration_ms=duration_ms,
            rtf=total_ms / duration_ms if duration_ms > 0 else None,
            chunks=callback.chunks,
            sample_rate=24000,
            connect_ms=connect_ms,
            session_ready_ms=session_ready_ms,
            input_done_ms=input_done_ms,
            first_audio_after_input_done_ms=(
                ttfb_ms - input_done_ms if ttfb_ms is not None else None
            ),
            first_audio_before_input_done=(
                ttfb_ms < input_done_ms if ttfb_ms is not None else None
            ),
            status="first_audio_only" if args.first_audio_only else None,
            error=None if callback.audio else "no audio bytes returned",
        )
    except Exception as exc:
        return result_from_error(provider, request_id, text, start, exc)


async def run_provider(args: argparse.Namespace, texts: list[str]) -> list[BenchResult]:
    sem = asyncio.Semaphore(max(1, int(args.concurrency)))

    async def one(index: int, text: str) -> BenchResult:
        async with sem:
            if args.provider == "current-grpc":
                return await asyncio.to_thread(run_current_grpc_one, args, text, index)
            if args.provider == "vllm-http-stream":
                return await run_vllm_http_one(args, text, index, stream=True)
            if args.provider == "vllm-http-wav":
                return await run_vllm_http_one(args, text, index, stream=False)
            if args.provider == "vllm-ws-stream":
                return await run_vllm_ws_one(args, text, index)
            if args.provider == "qwen-realtime-sdk":
                return await asyncio.to_thread(run_qwen_realtime_sdk_one, args, text, index)
            raise ValueError(f"Unsupported provider: {args.provider}")

    return await asyncio.gather(*(one(index, text) for index, text in enumerate(texts, start=1)))


def write_outputs(args: argparse.Namespace, results: list[BenchResult], texts: list[str]) -> dict[str, Any]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "tts-provider-benchmark/v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "provider": args.provider,
        "rounds": len(results),
        "concurrency": args.concurrency,
        "text_generation": {
            "seed": args.seed,
            "min_chars": args.min_chars,
            "max_chars": args.max_chars,
            "input_chunk_chars": args.input_chunk_chars,
            "input_chunk_delay_ms": args.input_chunk_delay_ms,
        },
        "provider_config": {
            "base_url": args.base_url,
            "ws_url": args.ws_url,
            "grpc_target": args.grpc_target,
            "model": args.model,
            "tts_profile_id": args.tts_profile_id,
            "voice": args.voice,
            "language": args.language,
            "qwen_mode": args.qwen_mode,
        },
        "summary": summarize_results(results),
        "results": [asdict(result) for result in results],
        "texts": texts,
    }
    json_path = out_dir / f"{args.label}-{args.provider}.json"
    csv_path = out_dir / f"{args.label}-{args.provider}.csv"
    md_path = out_dir / f"{args.label}-{args.provider}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(asdict(results[0]).keys()) if results else list(BenchResult.__dataclass_fields__.keys()))
        writer.writeheader()
        for result in results:
            writer.writerow(asdict(result))
    md_path.write_text(render_markdown_report(report), encoding="utf-8")
    return {"json": str(json_path), "csv": str(csv_path), "markdown": str(md_path), "report": report}


def render_markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"# TTS Provider Benchmark: {report['provider']}",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Rounds: `{summary['total']}`",
        f"- Success: `{summary['ok']}/{summary['total']}` (`{summary['success_rate']}`)",
        f"- Concurrency: `{report['concurrency']}`",
        "",
        "## Key Metrics",
        "",
        "| metric | count | p50 | p95 | avg | min | max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, values in summary["metrics"].items():
        if values["count"] == 0:
            continue
        lines.append(
            f"| `{name}` | {values['count']} | {values['p50']} | {values['p95']} | "
            f"{values['avg']} | {values['min']} | {values['max']} |"
        )
    boolean_counts = summary.get("boolean_counts") or {}
    if boolean_counts:
        lines.extend(["", "## Boolean Counts", ""])
        for name, value in boolean_counts.items():
            lines.append(f"- `{name}`: `{value}`")
    if summary["errors"]:
        lines.extend(["", "## Errors", ""])
        for error in summary["errors"]:
            lines.append(f"- `{error}`")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark TTS provider latency and stability.")
    parser.add_argument("--provider", required=True, choices=[
        "current-grpc",
        "vllm-http-stream",
        "vllm-http-wav",
        "vllm-ws-stream",
        "qwen-realtime-sdk",
    ])
    parser.add_argument("--label", default="tts-bench")
    parser.add_argument("--rounds", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260622)
    parser.add_argument("--min-chars", type=int, default=30)
    parser.add_argument("--max-chars", type=int, default=80)
    parser.add_argument("--input-chunk-chars", type=int, default=12)
    parser.add_argument("--input-chunk-delay-ms", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--out-dir", default="tmp/tts_bench")

    parser.add_argument("--base-url", default="http://127.0.0.1:15120")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:15120")
    parser.add_argument("--grpc-target", default="127.0.0.1:50052")
    parser.add_argument("--model", default="qwen3-tts")
    parser.add_argument("--voice", default="serena")
    parser.add_argument("--tts-profile-id", default="default_tts_profile")
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--speech-rate", type=float, default=1.0)
    parser.add_argument("--task-type", default="CustomVoice")
    parser.add_argument("--instructions", default="自然、清晰、稳定地朗读。")
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-env")
    parser.add_argument("--vllm-sample-rate", type=int, default=24000)
    parser.add_argument("--grpc-sample-rate", type=int, default=16000)
    parser.add_argument("--ws-split-granularity", choices=["sentence", "clause"], default="")
    parser.add_argument("--qwen-url", default="wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
    parser.add_argument("--qwen-mode", choices=["server_commit", "commit"], default="server_commit")
    parser.add_argument("--first-audio-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    texts = generate_texts(
        max(1, int(args.rounds)),
        seed=int(args.seed),
        min_chars=max(1, int(args.min_chars)),
        max_chars=max(1, int(args.max_chars)),
    )
    results = asyncio.run(run_provider(args, texts))
    output = write_outputs(args, results, texts)
    print(json.dumps({
        "provider": args.provider,
        "summary": output["report"]["summary"],
        "files": {key: value for key, value in output.items() if key != "report"},
    }, ensure_ascii=False, indent=2))
    return 0 if output["report"]["summary"]["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
