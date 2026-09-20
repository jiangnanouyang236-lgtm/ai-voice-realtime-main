#!/usr/bin/env python3
"""Benchmark the live LLM -> TTS streaming chain.

This script mirrors the Gateway's core streaming shape: LLM StreamChat feeds a
TTS StreamTextToSpeech request iterator, and the client measures when the first
text and first audio arrive. It does not exercise ASR, WebSocket transport, or
robot playback; use Gateway traces for those layers.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import statistics
import sys
import threading
import time
import uuid
from typing import Any, Iterable

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm import llm_service_pb2, llm_service_pb2_grpc  # noqa: E402
from tts import tts_service_pb2, tts_service_pb2_grpc  # noqa: E402


DEFAULT_PROMPTS = [
    "你好。",
    "讲个故事。",
    "好的，我先帮你看一下。",
    "请用一句话安慰一下不开心的人。",
    "目前测试环境首包很快，但实际上线后整体速度变慢，帮我分析可能原因。",
]

RANDOM_PROMPT_TOPICS = [
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
]

RANDOM_PROMPT_STYLES = [
    "温柔自然",
    "轻松幽默",
    "像给小朋友讲故事一样",
    "像朋友聊天一样",
    "简洁但有画面感",
    "略带悬念",
]

RANDOM_PROMPT_TASKS = [
    "说一段一到两句话的小故事",
    "给出一句安慰或鼓励",
    "描述一个有趣的小场景",
    "编一句适合睡前听的话",
    "说一句让人放松的话",
]


@dataclass
class ChainResult:
    request_id: int
    ok: bool
    prompt: str
    prompt_chars: int
    response_text: str
    response_chars: int
    first_tts_text: str
    first_tts_text_chars: int
    tts_setup_ms: float | None
    llm_first_text_ms: float | None
    tts_first_commit_ms: float | None
    tts_first_segment_sent_ms: float | None
    tts_bridge_buffer_ms: float | None
    tts_first_audio_ms: float | None
    tts_first_audio_after_commit_ms: float | None
    tts_first_audio_after_segment_send_ms: float | None
    tts_internal_first_pcm_ms: float | None
    tts_first_text_to_first_pcm_ms: float | None
    tts_provider_first_pcm_after_send_ms: float | None
    tts_gateway_after_server_pcm_ms: float | None
    llm_done_ms: float | None
    total_ms: float | None
    audio_bytes: int
    audio_duration_ms: float | None
    rtf: float | None
    audio_chunks: int
    sample_rate: int | None
    status: str
    error: str | None = None


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
        return {"count": 0, "min": None, "avg": None, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "avg": round(statistics.fmean(values), 1),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": round(max(values), 1),
    }


def pcm_duration_ms(audio_bytes: int, sample_rate: int, *, sample_width: int = 2, channels: int = 1) -> float:
    if sample_rate <= 0 or sample_width <= 0 or channels <= 0:
        return 0.0
    return audio_bytes / (sample_rate * sample_width * channels) * 1000.0


def generate_random_prompts(rounds: int, *, seed: int | None = None) -> list[str]:
    rng = random.Random(seed if seed is not None else time.time_ns())
    prompts: list[str] = []
    for index in range(1, max(1, int(rounds)) + 1):
        topic = rng.choice(RANDOM_PROMPT_TOPICS)
        style = rng.choice(RANDOM_PROMPT_STYLES)
        task = rng.choice(RANDOM_PROMPT_TASKS)
        nonce = uuid.uuid4().hex[:8]
        prompts.append(
            f"随机链路测试 {index}-{nonce}：请用{style}的语气，围绕“{topic}”{task}，"
            "不要列清单，直接回答。"
        )
    return prompts


def _as_metric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize_results(results: list[ChainResult]) -> dict[str, Any]:
    ok_results = [result for result in results if result.ok]
    failed_results = [result for result in results if not result.ok]

    def metric_values(name: str) -> list[float]:
        values: list[float] = []
        for result in ok_results:
            value = _as_metric(getattr(result, name))
            if value is not None:
                values.append(value)
        return values

    return {
        "total": len(results),
        "ok": len(ok_results),
        "failed": len(failed_results),
        "success_rate": round(len(ok_results) / len(results), 4) if results else 0.0,
        "metrics": {
            "llm_first_text_ms": summarize_numeric(metric_values("llm_first_text_ms")),
            "tts_setup_ms": summarize_numeric(metric_values("tts_setup_ms")),
            "tts_first_commit_ms": summarize_numeric(metric_values("tts_first_commit_ms")),
            "tts_first_segment_sent_ms": summarize_numeric(metric_values("tts_first_segment_sent_ms")),
            "tts_bridge_buffer_ms": summarize_numeric(metric_values("tts_bridge_buffer_ms")),
            "tts_first_audio_ms": summarize_numeric(metric_values("tts_first_audio_ms")),
            "tts_first_audio_after_commit_ms": summarize_numeric(metric_values("tts_first_audio_after_commit_ms")),
            "tts_first_audio_after_segment_send_ms": summarize_numeric(metric_values("tts_first_audio_after_segment_send_ms")),
            "tts_internal_first_pcm_ms": summarize_numeric(metric_values("tts_internal_first_pcm_ms")),
            "tts_first_text_to_first_pcm_ms": summarize_numeric(metric_values("tts_first_text_to_first_pcm_ms")),
            "tts_provider_first_pcm_after_send_ms": summarize_numeric(metric_values("tts_provider_first_pcm_after_send_ms")),
            "tts_gateway_after_server_pcm_ms": summarize_numeric(metric_values("tts_gateway_after_server_pcm_ms")),
            "llm_done_ms": summarize_numeric(metric_values("llm_done_ms")),
            "total_ms": summarize_numeric(metric_values("total_ms")),
            "audio_duration_ms": summarize_numeric(metric_values("audio_duration_ms")),
            "rtf": summarize_numeric(metric_values("rtf")),
            "audio_chunks": summarize_numeric(metric_values("audio_chunks")),
        },
        "errors": [result.error for result in failed_results[:10] if result.error],
    }


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _epoch_ms() -> float:
    return time.time() * 1000.0


def _get_tts_api_key(args: argparse.Namespace) -> str | None:
    if args.tts_api_key:
        return args.tts_api_key
    if args.tts_api_key_env:
        return os.getenv(args.tts_api_key_env)
    return (
        os.getenv("TTS_BENCH_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("TTS_API_KEY")
    )


def run_chain_one(
    args: argparse.Namespace,
    *,
    request_id: int,
    prompt: str,
) -> ChainResult:
    start = time.perf_counter()
    start_epoch_ms = _epoch_ms()
    session_id = f"{args.session_prefix}-{uuid.uuid4().hex[:10]}"
    trace_id = f"bench-chain-{uuid.uuid4().hex[:12]}:{request_id}"
    playback_id = f"{trace_id}:playback"
    response_parts: list[str] = []
    first_tts_text = ""
    llm_first_text_ms: float | None = None
    tts_first_commit_ms: float | None = None
    llm_done_ms: float | None = None

    try:
        llm_channel = grpc.insecure_channel(args.llm_target)
        tts_channel = grpc.insecure_channel(args.tts_target)
        llm_stub = llm_service_pb2_grpc.LLMServiceStub(llm_channel)
        tts_stub = tts_service_pb2_grpc.TTSServiceStub(tts_channel)

        llm_request = llm_service_pb2.ChatRequest(
            text=prompt,
            session_id=session_id,
            bot_id=args.bot_id,
            robot_id=args.robot_id,
            trace_id=trace_id,
            config=llm_service_pb2.LLMConfig(
                model_name=args.llm_model,
                temperature=float(args.temperature),
                max_tokens=int(args.max_tokens),
            ),
        )

        def request_iter() -> Iterable[tts_service_pb2.TextChunk]:
            nonlocal first_tts_text, llm_first_text_ms, tts_first_commit_ms, llm_done_ms
            first_chunk = True
            llm_call = llm_stub.StreamChat(llm_request, timeout=args.timeout)
            try:
                for chunk in llm_call:
                    if chunk.text:
                        if llm_first_text_ms is None:
                            llm_first_text_ms = _elapsed_ms(start)
                        response_parts.append(chunk.text)
                        if tts_first_commit_ms is None:
                            tts_first_commit_ms = _elapsed_ms(start)
                            first_tts_text = chunk.text
                        kwargs: dict[str, Any] = {
                            "text": chunk.text,
                            "is_final": False,
                            "session_id": session_id,
                            "trace_id": trace_id,
                            "round_id": trace_id,
                            "playback_id": playback_id,
                            "gateway_send_epoch_ms": _epoch_ms(),
                        }
                        if first_chunk:
                            kwargs["config"] = tts_service_pb2.TTSConfig(tts_profile_id=args.tts_profile_id)
                        first_chunk = False
                        yield tts_service_pb2.TextChunk(**kwargs)
                    if chunk.is_final:
                        break
            finally:
                llm_done_ms = _elapsed_ms(start)

            final_kwargs: dict[str, Any] = {
                "text": "",
                "is_final": True,
                "session_id": session_id,
                "trace_id": trace_id,
                "round_id": trace_id,
                "playback_id": playback_id,
                "gateway_send_epoch_ms": _epoch_ms(),
            }
            if first_chunk:
                final_kwargs["config"] = tts_service_pb2.TTSConfig(tts_profile_id=args.tts_profile_id)
            yield tts_service_pb2.TextChunk(**final_kwargs)

        tts_first_audio_ms: float | None = None
        tts_first_segment_sent_ms: float | None = None
        tts_bridge_buffer_ms: float | None = None
        tts_first_audio_after_segment_send_ms: float | None = None
        first_audio_epoch_ms = 0.0
        audio_bytes = 0
        audio_chunks = 0
        sample_rate = args.tts_sample_rate
        tts_internal_first_pcm_ms: float | None = None
        tts_first_text_to_first_pcm_ms: float | None = None
        tts_provider_first_pcm_after_send_ms: float | None = None
        tts_gateway_after_server_pcm_ms: float | None = None

        for audio_chunk in tts_stub.StreamTextToSpeech(request_iter(), timeout=args.timeout):
            if audio_chunk.is_final:
                continue
            if not audio_chunk.audio_data:
                continue
            if tts_first_audio_ms is None:
                tts_first_audio_ms = _elapsed_ms(start)
                first_audio_epoch_ms = _epoch_ms()
                tts_internal_first_pcm_ms = (
                    float(audio_chunk.tts_internal_first_pcm_ms)
                    if getattr(audio_chunk, "tts_internal_first_pcm_ms", 0.0)
                    else None
                )
                tts_first_text_to_first_pcm_ms = (
                    float(audio_chunk.tts_first_text_to_first_pcm_ms)
                    if getattr(audio_chunk, "tts_first_text_to_first_pcm_ms", 0.0)
                    else None
                )
                tts_server_receive_epoch_ms = float(
                    getattr(audio_chunk, "tts_server_receive_epoch_ms", 0.0) or 0.0
                )
                tts_first_text_receive_epoch_ms = float(
                    getattr(audio_chunk, "tts_first_text_receive_epoch_ms", 0.0) or 0.0
                )
                tts_first_text_send_epoch_ms = float(
                    getattr(audio_chunk, "tts_first_text_send_epoch_ms", 0.0) or 0.0
                )
                server_first_pcm_epoch_ms = float(getattr(audio_chunk, "tts_first_pcm_epoch_ms", 0.0) or 0.0)
                if tts_first_text_send_epoch_ms:
                    tts_first_segment_sent_ms = max(0.0, tts_first_text_send_epoch_ms - start_epoch_ms)
                    tts_first_audio_after_segment_send_ms = max(
                        0.0,
                        tts_first_audio_ms - tts_first_segment_sent_ms,
                    )
                if tts_first_text_receive_epoch_ms and tts_first_text_send_epoch_ms:
                    tts_bridge_buffer_ms = max(
                        0.0,
                        tts_first_text_send_epoch_ms - tts_first_text_receive_epoch_ms,
                    )
                elif tts_server_receive_epoch_ms and tts_first_text_send_epoch_ms:
                    tts_bridge_buffer_ms = max(
                        0.0,
                        tts_first_text_send_epoch_ms - tts_server_receive_epoch_ms,
                    )
                if server_first_pcm_epoch_ms and tts_first_text_send_epoch_ms:
                    tts_provider_first_pcm_after_send_ms = max(
                        0.0,
                        server_first_pcm_epoch_ms - tts_first_text_send_epoch_ms,
                    )
                if server_first_pcm_epoch_ms:
                    tts_gateway_after_server_pcm_ms = max(0.0, first_audio_epoch_ms - server_first_pcm_epoch_ms)
            audio_chunks += 1
            audio_bytes += len(audio_chunk.audio_data)
            sample_rate = int(audio_chunk.sample_rate or sample_rate)

        total_ms = _elapsed_ms(start)
        duration_ms = pcm_duration_ms(audio_bytes, sample_rate)
        ok = audio_bytes > 0 and bool(response_parts)
        return ChainResult(
            request_id=request_id,
            ok=ok,
            prompt=prompt,
            prompt_chars=len(prompt),
            response_text="".join(response_parts),
            response_chars=len("".join(response_parts)),
            first_tts_text=first_tts_text,
            first_tts_text_chars=len(first_tts_text),
            tts_setup_ms=None,
            llm_first_text_ms=llm_first_text_ms,
            tts_first_commit_ms=tts_first_commit_ms,
            tts_first_segment_sent_ms=tts_first_segment_sent_ms,
            tts_bridge_buffer_ms=tts_bridge_buffer_ms,
            tts_first_audio_ms=tts_first_audio_ms,
            tts_first_audio_after_commit_ms=(
                tts_first_audio_ms - tts_first_commit_ms
                if tts_first_audio_ms is not None and tts_first_commit_ms is not None
                else None
            ),
            tts_first_audio_after_segment_send_ms=tts_first_audio_after_segment_send_ms,
            tts_internal_first_pcm_ms=tts_internal_first_pcm_ms,
            tts_first_text_to_first_pcm_ms=tts_first_text_to_first_pcm_ms,
            tts_provider_first_pcm_after_send_ms=tts_provider_first_pcm_after_send_ms,
            tts_gateway_after_server_pcm_ms=tts_gateway_after_server_pcm_ms,
            llm_done_ms=llm_done_ms,
            total_ms=total_ms,
            audio_bytes=audio_bytes,
            audio_duration_ms=duration_ms,
            rtf=total_ms / duration_ms if duration_ms > 0 else None,
            audio_chunks=audio_chunks,
            sample_rate=sample_rate,
            status="ok" if ok else "no_audio_or_text",
            error=None if ok else "missing LLM text or TTS audio",
        )
    except Exception as exc:
        return ChainResult(
            request_id=request_id,
            ok=False,
            prompt=prompt,
            prompt_chars=len(prompt),
            response_text="".join(response_parts),
            response_chars=len("".join(response_parts)),
            first_tts_text=first_tts_text,
            first_tts_text_chars=len(first_tts_text),
            tts_setup_ms=None,
            llm_first_text_ms=llm_first_text_ms,
            tts_first_commit_ms=tts_first_commit_ms,
            tts_first_segment_sent_ms=None,
            tts_bridge_buffer_ms=None,
            tts_first_audio_ms=None,
            tts_first_audio_after_commit_ms=None,
            tts_first_audio_after_segment_send_ms=None,
            tts_internal_first_pcm_ms=None,
            tts_first_text_to_first_pcm_ms=None,
            tts_provider_first_pcm_after_send_ms=None,
            tts_gateway_after_server_pcm_ms=None,
            llm_done_ms=llm_done_ms,
            total_ms=_elapsed_ms(start),
            audio_bytes=0,
            audio_duration_ms=None,
            rtf=None,
            audio_chunks=0,
            sample_rate=None,
            status="error",
            error=repr(exc),
        )


def run_chain_one_qwen_realtime(
    args: argparse.Namespace,
    *,
    request_id: int,
    prompt: str,
) -> ChainResult:
    start = time.perf_counter()
    session_id = f"{args.session_prefix}-{uuid.uuid4().hex[:10]}"
    trace_id = f"bench-qwen-realtime-{uuid.uuid4().hex[:12]}:{request_id}"
    response_parts: list[str] = []
    first_tts_text = ""
    tts_setup_ms: float | None = None
    llm_first_text_ms: float | None = None
    tts_first_commit_ms: float | None = None
    tts_first_segment_sent_ms: float | None = None
    llm_done_ms: float | None = None
    llm_start: float | None = None

    try:
        import dashscope
        from dashscope.audio.qwen_tts_realtime import (
            AudioFormat,
            QwenTtsRealtime,
            QwenTtsRealtimeCallback,
        )

        api_key = _get_tts_api_key(args)
        if not api_key:
            raise ValueError(
                "Cloud Qwen realtime TTS requires --tts-api-key or "
                "--tts-api-key-env (for example DASHSCOPE_API_KEY)."
            )
        dashscope.api_key = api_key

        class Callback(QwenTtsRealtimeCallback):
            def __init__(self) -> None:
                super().__init__()
                self.first_audio_event = threading.Event()
                self.complete_event = threading.Event()
                self.audio = bytearray()
                self.chunks = 0
                self.error: Any = None
                self.first_audio_ms: float | None = None
                self.lock = threading.Lock()

            def on_event(self, response: dict) -> None:
                event_type = response.get("type")
                if event_type == "response.audio.delta":
                    delta = response.get("delta") or ""
                    if delta:
                        data = base64.b64decode(delta)
                        if data:
                            with self.lock:
                                if self.first_audio_ms is None:
                                    self.first_audio_ms = _elapsed_ms(start)
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

        llm_channel = grpc.insecure_channel(args.llm_target)
        llm_stub = llm_service_pb2_grpc.LLMServiceStub(llm_channel)
        llm_request = llm_service_pb2.ChatRequest(
            text=prompt,
            session_id=session_id,
            bot_id=args.bot_id,
            robot_id=args.robot_id,
            trace_id=trace_id,
            config=llm_service_pb2.LLMConfig(
                model_name=args.llm_model,
                temperature=float(args.temperature),
                max_tokens=int(args.max_tokens),
            ),
        )

        callback = Callback()
        qwen_tts = QwenTtsRealtime(
            model=args.cloud_tts_model,
            callback=callback,
            url=args.qwen_url,
        )
        qwen_tts.connect()
        qwen_tts.update_session(
            voice=args.cloud_tts_voice,
            response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            mode=args.qwen_mode,
            speech_rate=float(args.speech_rate),
        )
        tts_setup_ms = _elapsed_ms(start)

        try:
            llm_start = time.perf_counter()
            llm_call = llm_stub.StreamChat(llm_request, timeout=args.timeout)
            for chunk in llm_call:
                if chunk.text:
                    if llm_first_text_ms is None:
                        llm_first_text_ms = _elapsed_ms(llm_start)
                    response_parts.append(chunk.text)
                    if tts_first_commit_ms is None:
                        tts_first_commit_ms = _elapsed_ms(start)
                        tts_first_segment_sent_ms = tts_first_commit_ms
                        first_tts_text = chunk.text
                    qwen_tts.append_text(chunk.text)
                    if args.qwen_mode == "commit":
                        qwen_tts.commit()
                if chunk.is_final:
                    break
        finally:
            if llm_start is not None:
                llm_done_ms = _elapsed_ms(llm_start)

        qwen_tts.finish()
        callback.first_audio_event.wait(args.timeout)
        callback.complete_event.wait(args.timeout)
        qwen_tts.close()

        if callback.error:
            raise RuntimeError(callback.error)

        tts_first_audio_ms = callback.first_audio_ms
        total_ms = _elapsed_ms(start)
        audio_bytes = len(callback.audio)
        sample_rate = 24000
        duration_ms = pcm_duration_ms(audio_bytes, sample_rate)
        ok = audio_bytes > 0 and bool(response_parts)
        tts_first_audio_after_commit_ms = (
            tts_first_audio_ms - tts_first_commit_ms
            if tts_first_audio_ms is not None and tts_first_commit_ms is not None
            else None
        )
        tts_first_audio_after_segment_send_ms = (
            tts_first_audio_ms - tts_first_segment_sent_ms
            if tts_first_audio_ms is not None and tts_first_segment_sent_ms is not None
            else None
        )
        return ChainResult(
            request_id=request_id,
            ok=ok,
            prompt=prompt,
            prompt_chars=len(prompt),
            response_text="".join(response_parts),
            response_chars=len("".join(response_parts)),
            first_tts_text=first_tts_text,
            first_tts_text_chars=len(first_tts_text),
            tts_setup_ms=tts_setup_ms,
            llm_first_text_ms=llm_first_text_ms,
            tts_first_commit_ms=tts_first_commit_ms,
            tts_first_segment_sent_ms=tts_first_segment_sent_ms,
            tts_bridge_buffer_ms=0.0 if tts_first_segment_sent_ms is not None else None,
            tts_first_audio_ms=tts_first_audio_ms,
            tts_first_audio_after_commit_ms=tts_first_audio_after_commit_ms,
            tts_first_audio_after_segment_send_ms=tts_first_audio_after_segment_send_ms,
            tts_internal_first_pcm_ms=tts_first_audio_ms,
            tts_first_text_to_first_pcm_ms=tts_first_audio_after_commit_ms,
            tts_provider_first_pcm_after_send_ms=tts_first_audio_after_segment_send_ms,
            tts_gateway_after_server_pcm_ms=None,
            llm_done_ms=llm_done_ms,
            total_ms=total_ms,
            audio_bytes=audio_bytes,
            audio_duration_ms=duration_ms,
            rtf=total_ms / duration_ms if duration_ms > 0 else None,
            audio_chunks=callback.chunks,
            sample_rate=sample_rate,
            status="ok" if ok else "no_audio_or_text",
            error=None if ok else "missing LLM text or TTS audio",
        )
    except Exception as exc:
        return ChainResult(
            request_id=request_id,
            ok=False,
            prompt=prompt,
            prompt_chars=len(prompt),
            response_text="".join(response_parts),
            response_chars=len("".join(response_parts)),
            first_tts_text=first_tts_text,
            first_tts_text_chars=len(first_tts_text),
            tts_setup_ms=tts_setup_ms,
            llm_first_text_ms=llm_first_text_ms,
            tts_first_commit_ms=tts_first_commit_ms,
            tts_first_segment_sent_ms=tts_first_segment_sent_ms,
            tts_bridge_buffer_ms=0.0 if tts_first_segment_sent_ms is not None else None,
            tts_first_audio_ms=None,
            tts_first_audio_after_commit_ms=None,
            tts_first_audio_after_segment_send_ms=None,
            tts_internal_first_pcm_ms=None,
            tts_first_text_to_first_pcm_ms=None,
            tts_provider_first_pcm_after_send_ms=None,
            tts_gateway_after_server_pcm_ms=None,
            llm_done_ms=llm_done_ms,
            total_ms=_elapsed_ms(start),
            audio_bytes=0,
            audio_duration_ms=None,
            rtf=None,
            audio_chunks=0,
            sample_rate=None,
            status="error",
            error=repr(exc),
        )


def build_report(args: argparse.Namespace, results: list[ChainResult]) -> dict[str, Any]:
    return {
        "schema_version": "llm-tts-chain-benchmark/v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "label": args.label,
        "config": {
            "llm_target": args.llm_target,
            "tts_target": args.tts_target,
            "bot_id": args.bot_id,
            "robot_id": args.robot_id,
            "llm_model": args.llm_model,
            "tts_provider": args.tts_provider,
            "tts_profile_id": args.tts_profile_id,
            "voice": args.voice,
            "cloud_tts_model": args.cloud_tts_model,
            "cloud_tts_voice": args.cloud_tts_voice,
            "qwen_mode": args.qwen_mode,
            "speech_rate": args.speech_rate,
            "concurrency": args.concurrency,
            "random_prompts": args.random_prompts,
            "random_seed": args.random_seed,
            "timeout": args.timeout,
        },
        "summary": summarize_results(results),
        "results": [asdict(result) for result in results],
    }


def write_outputs(report: dict[str, Any], output_dir: Path, label: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{label}.json"
    csv_path = output_dir / f"{label}.csv"
    md_path = output_dir / f"{label}.md"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    results = report["results"]
    if results:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)

    md_path.write_text(render_markdown_report(report), encoding="utf-8")


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# LLM -> TTS Chain Benchmark: {report['label']}",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- LLM target: `{report['config']['llm_target']}`",
        f"- TTS target: `{report['config']['tts_target']}`",
        f"- TTS provider: `{report['config']['tts_provider']}`",
        f"- Bot: `{report['config']['bot_id']}`",
        f"- TTS profile: `{report['config']['tts_profile_id']}`",
        f"- Voice: `{report['config']['voice']}` / cloud `{report['config']['cloud_tts_voice']}`",
        f"- Concurrency: `{report['config']['concurrency']}`",
        "",
        "## Summary",
        "",
        f"- Success: `{report['summary']['ok']}/{report['summary']['total']}` (`{report['summary']['success_rate']}`)",
        "",
        "| metric | count | p50 | p90 | p95 | p99 | avg | max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for metric, summary in report["summary"]["metrics"].items():
        if not summary.get("count"):
            continue
        lines.append(
            f"| `{metric}` | {summary['count']} | {summary['p50']} | {summary['p90']} | "
            f"{summary['p95']} | {summary['p99']} | {summary['avg']} | {summary['max']} |"
        )
    lines.extend(["", "## Results", ""])
    for result in report["results"]:
        lines.append(
            "- "
            f"#{result['request_id']} ok={result['ok']} "
            f"tts_setup={_fmt(result.get('tts_setup_ms'))}ms "
            f"llm_first={_fmt(result['llm_first_text_ms'])}ms "
            f"tts_first_audio={_fmt(result['tts_first_audio_ms'])}ms "
            f"after_commit={_fmt(result['tts_first_audio_after_commit_ms'])}ms "
            f"after_segment_send={_fmt(result.get('tts_first_audio_after_segment_send_ms'))}ms "
            f"provider_after_send={_fmt(result.get('tts_provider_first_pcm_after_send_ms'))}ms "
            f"chars={result['response_chars']} prompt={result['prompt']!r}"
        )
    return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark local LLM StreamChat feeding local TTS StreamTextToSpeech.",
    )
    parser.add_argument("--llm-target", default="127.0.0.1:50053")
    parser.add_argument("--tts-target", default="127.0.0.1:50052")
    parser.add_argument("--tts-provider", choices=["local-grpc", "qwen-realtime-sdk"], default="local-grpc")
    parser.add_argument("--bot-id", default="xiaowen")
    parser.add_argument("--robot-id", default="companion_01")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--voice", default="serena")
    parser.add_argument("--tts-profile-id", default="default_tts_profile")
    parser.add_argument("--cloud-tts-model", default="qwen3-tts-flash-realtime")
    parser.add_argument("--cloud-tts-voice", default="Cherry")
    parser.add_argument("--qwen-url", default="wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
    parser.add_argument("--qwen-mode", choices=["server_commit", "commit"], default="server_commit")
    parser.add_argument("--tts-api-key")
    parser.add_argument("--tts-api-key-env")
    parser.add_argument("--speech-rate", type=float, default=1.0)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--tts-sample-rate", type=int, default=16000)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--prompt", action="append", help="Prompt to benchmark. Can be repeated.")
    parser.add_argument("--random-prompts", action="store_true", help="Generate unique creative prompts for every round.")
    parser.add_argument("--random-seed", type=int, help="Optional seed for random prompt generation.")
    parser.add_argument("--session-prefix", default="bench-chain")
    parser.add_argument("--label", default=f"llm-tts-chain-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    parser.add_argument("--out-dir", default="tmp/llm_tts_chain_bench")
    parser.add_argument("--json-only", action="store_true", help="Print JSON to stdout without writing files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    rounds = max(1, int(args.rounds))
    if args.random_prompts:
        selected_prompts = generate_random_prompts(rounds, seed=args.random_seed)
    else:
        prompts = args.prompt or DEFAULT_PROMPTS
        selected_prompts = [prompts[index % len(prompts)] for index in range(rounds)]

    max_workers = max(1, int(args.concurrency))
    run_one = run_chain_one_qwen_realtime if args.tts_provider == "qwen-realtime-sdk" else run_chain_one
    results_by_id: dict[int, ChainResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_one, args, request_id=index, prompt=prompt): (index, prompt)
            for index, prompt in enumerate(selected_prompts, start=1)
        }
        completed = 0
        for future in as_completed(futures):
            index, prompt = futures[future]
            completed += 1
            result = future.result()
            results_by_id[index] = result
            print(
                f"[{completed}/{rounds}] request={index} ok={result.ok} "
                f"llm_first={_fmt(result.llm_first_text_ms)}ms "
                f"tts_first_audio={_fmt(result.tts_first_audio_ms)}ms "
                f"after_commit={_fmt(result.tts_first_audio_after_commit_ms)}ms "
                f"prompt={prompt!r}",
                file=sys.stderr,
            )

    results = [results_by_id[index] for index in sorted(results_by_id)]

    report = build_report(args, results)
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        output_dir = Path(args.out_dir)
        write_outputs(report, output_dir, args.label)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"wrote: {output_dir / (args.label + '.json')}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
