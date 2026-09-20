#!/usr/bin/env python3
"""Benchmark Qwen3-ASR batch behavior and probe true streaming support.

This script is intentionally independent from the production Gateway/Rust
client path. It generates a reusable TTS audio dataset, runs batch ASR against
the OpenAI-compatible vLLM ASR endpoint, optionally compares the current STT
gRPC unary/collect-stream wrapper, and records whether any known WebSocket
streaming ASR endpoint is available.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import io
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time
import wave
from typing import Any, Iterable

import grpc
import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stt import stt_service_pb2, stt_service_pb2_grpc  # noqa: E402


TEXT_POOL = [
    "帮我查一下今天青岛的天气。",
    "下午三点提醒我开会。",
    "好的，请你往前走一点。",
    "停下，不要继续往前了。",
    "给我讲一个温柔一点的小故事。",
    "我想玩故事接龙，我们从森林里的小路开始。",
    "帮我看看周围有没有人。",
    "我刚才说的那句话你还记得吗？",
    "我们来做一个简单的认知小游戏。",
    "如果我说错了，你可以提醒我一下。",
    "今天有点累，想和你随便聊一会儿。",
    "你能帮我总结一下刚才说的内容吗？",
    "请你查询一下黄金价格。",
    "明天早上八点叫我起床。",
    "我想知道最近几天有没有下雨。",
    "好的，我现在准备出门，你提醒我带钥匙。",
    "我们继续刚才的话题。",
    "退出吧，等会儿我再叫你。",
    "请你慢一点说，我有点没听清。",
    "我想让你观察一下房间里有没有异常情况。",
    "从前有一只小狐狸，它住在一片安静的森林里。",
    "今天青岛的天气是多云，温度在十九到二十五摄氏度之间。",
    "小游戏完成，总分二分，有一些表现值得继续观察。",
    "近期事件遗忘从不，重复提问从不，词汇寻找困难经常。",
    "这不是医学诊断，只作为日常参考，后续可以持续观察。",
    "如果你准备好了，我们现在开始测试语音识别的速度和稳定性。",
    "我想问一个比较长的问题，你先认真听我说完再回答。",
    "刚才那个答案有点复杂，你能不能用更简单的话再说一遍？",
    "请你帮我判断一下，这句话是真还是假。",
    "我想让你随机出一道谜语，然后等我回答。",
]


@dataclass
class AudioItem:
    request_id: int
    text: str
    wav_path: str
    pcm_bytes: int
    duration_ms: float
    sample_rate: int


@dataclass
class ASRResult:
    mode: str
    request_id: int
    ok: bool
    expected_text: str
    recognized_text: str
    exact_match: bool
    normalized_match: bool
    duration_ms: float
    audio_bytes: int
    request_ms: float | None
    final_from_speech_start_ms: float | None
    final_after_audio_end_ms: float | None
    first_partial_ms: float | None
    char_error_rate: float | None
    simulated_batch_after_end_ms: float | None
    simulated_stream_transport_tail_ms: float | None
    simulated_upload_bytes: int | None
    network_profile: str
    status: str
    error: str | None = None


def normalize_text(text: str) -> str:
    return "".join(ch for ch in text.strip() if ch not in "，。！？；,.!?;：:、 \n\t")


def char_error_rate(expected: str, actual: str) -> float:
    expected_norm = normalize_text(expected)
    actual_norm = normalize_text(actual)
    if not expected_norm and not actual_norm:
        return 0.0
    if not expected_norm:
        return 1.0

    previous = list(range(len(actual_norm) + 1))
    for i, expected_char in enumerate(expected_norm, start=1):
        current = [i]
        for j, actual_char in enumerate(actual_norm, start=1):
            cost = 0 if expected_char == actual_char else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + cost,
                )
            )
        previous = current
    return previous[-1] / max(len(expected_norm), 1)


def percentile(values: list[float], pct: float, *, digits: int = 1) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], digits)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight, digits)


def summarize_numeric(values: list[float], *, digits: int = 1) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "avg": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "min": round(min(values), digits),
        "avg": round(statistics.fmean(values), digits),
        "p50": percentile(values, 50, digits=digits),
        "p95": percentile(values, 95, digits=digits),
        "max": round(max(values), digits),
    }


def build_texts(rounds: int, *, seed: int, min_chars: int, max_chars: int) -> list[str]:
    rng = random.Random(seed)
    texts: list[str] = []
    for index in range(rounds):
        target = rng.randint(min_chars, max_chars)
        text = ""
        while len(text) < target:
            part = rng.choice(TEXT_POOL)
            text = f"{text}{part}" if not text else f"{text}{part}"
        text = text[:max_chars].rstrip("，。！？；,.!?;")
        if not text:
            text = TEXT_POOL[index % len(TEXT_POOL)]
        if text[-1] not in "。！？.!?":
            text += "。"
        texts.append(text)
    return texts


def pcm_duration_ms(num_bytes: int, sample_rate: int, *, sample_width: int = 2, channels: int = 1) -> float:
    if sample_rate <= 0 or sample_width <= 0 or channels <= 0:
        return 0.0
    return num_bytes / (sample_rate * sample_width * channels) * 1000.0


def resample_linear_i16(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    if from_rate == to_rate or not pcm:
        return pcm
    sample_count = len(pcm) // 2
    samples = [int.from_bytes(pcm[i * 2 : i * 2 + 2], "little", signed=True) for i in range(sample_count)]
    if sample_count <= 1:
        return pcm
    output_count = max(1, int(round(sample_count * to_rate / from_rate)))
    output = bytearray()
    for out_index in range(output_count):
        src_pos = out_index * (sample_count - 1) / max(1, output_count - 1)
        left = int(math.floor(src_pos))
        right = min(left + 1, sample_count - 1)
        weight = src_pos - left
        value = int(round(samples[left] * (1.0 - weight) + samples[right] * weight))
        value = max(-32768, min(32767, value))
        output.extend(value.to_bytes(2, "little", signed=True))
    return bytes(output)


def wav_bytes_from_pcm(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buffer.getvalue()


def read_wav_pcm(path: Path) -> tuple[bytes, int, float]:
    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
        duration_ms = wav_file.getnframes() / sample_rate * 1000.0 if sample_rate else 0.0
    return frames, sample_rate, duration_ms


async def synthesize_tts_pcm(args: argparse.Namespace, text: str) -> bytes:
    headers = [("Authorization", f"Bearer {args.tts_api_key}")] if args.tts_api_key else []
    pcm = bytearray()
    async with websockets.connect(
        args.tts_ws_url,
        additional_headers=headers,
        max_size=None,
        open_timeout=args.timeout,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "session.config",
                    "model": args.tts_model,
                    "voice": args.tts_voice,
                    "language": args.tts_language,
                    "response_format": "pcm",
                    "task_type": args.tts_task_type,
                    "instructions": args.tts_instructions,
                    "stream_audio": True,
                },
                ensure_ascii=False,
            )
        )
        for ch in text:
            await ws.send(json.dumps({"type": "input.text", "text": ch}, ensure_ascii=False))
            if args.tts_input_delay_ms > 0:
                await asyncio.sleep(args.tts_input_delay_ms / 1000.0)
        await ws.send(json.dumps({"type": "input.done"}))

        while True:
            message = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
            if isinstance(message, bytes):
                pcm.extend(message)
                continue
            event = json.loads(message)
            event_type = event.get("type")
            delta = event.get("delta") or event.get("audio") or event.get("data")
            if event_type in {"response.audio.delta", "audio.delta"} and delta:
                pcm.extend(base64.b64decode(delta))
            elif event_type in {"session.done", "response.done", "done", "completed"}:
                break
            elif event_type == "error":
                raise RuntimeError(event)
    return bytes(pcm)


async def build_audio_dataset(args: argparse.Namespace, out_dir: Path) -> list[AudioItem]:
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    texts = build_texts(args.rounds, seed=args.seed, min_chars=args.min_chars, max_chars=args.max_chars)
    items: list[AudioItem] = []
    for index, text in enumerate(texts, start=1):
        wav_path = audio_dir / f"{index:03d}.wav"
        if wav_path.exists() and not args.regenerate_audio:
            pcm, sample_rate, duration_ms = read_wav_pcm(wav_path)
        else:
            source_pcm = await synthesize_tts_pcm(args, text)
            pcm = resample_linear_i16(source_pcm, args.tts_sample_rate, args.asr_sample_rate)
            wav_path.write_bytes(wav_bytes_from_pcm(pcm, args.asr_sample_rate))
            sample_rate = args.asr_sample_rate
            duration_ms = pcm_duration_ms(len(pcm), sample_rate)
        items.append(
            AudioItem(
                request_id=index,
                text=text,
                wav_path=str(wav_path),
                pcm_bytes=len(pcm),
                duration_ms=duration_ms,
                sample_rate=sample_rate,
            )
        )
        print(f"[audio] {index}/{len(texts)} duration={duration_ms:.0f}ms text={text[:24]}", flush=True)
    return items


def extract_text(response: Any) -> str:
    if isinstance(response, dict):
        return str(response.get("text") or "").strip()
    return str(response or "").strip()


def parse_network_profile(profile: str) -> tuple[float, float]:
    if ":" in profile:
        name, rest = profile.split(":", 1)
    else:
        name, rest = profile, profile
    if "," in rest:
        rtt, kbps = rest.split(",", 1)
    else:
        raise ValueError(f"invalid network profile {profile!r}; expected name:rtt_ms,kbps")
    return float(rtt), float(kbps)


def simulated_upload_bytes(args: argparse.Namespace, item: AudioItem) -> int:
    if args.network_audio_codec == "opus":
        return int(round(item.duration_ms / 1000.0 * args.opus_bitrate_kbps * 1000.0 / 8.0))
    return item.pcm_bytes


def simulated_network_metrics(
    args: argparse.Namespace,
    item: AudioItem,
    request_ms: float | None,
    network_profile: str,
) -> tuple[float | None, float | None, int | None]:
    if request_ms is None or network_profile == "none":
        return None, None, None
    rtt_ms, kbps = parse_network_profile(network_profile)
    upload_bytes = simulated_upload_bytes(args, item)
    upload_ms = upload_bytes * 8 / max(kbps * 1000.0, 1.0) * 1000.0
    simulated_batch_after_end_ms = rtt_ms + upload_ms + request_ms
    simulated_stream_transport_tail_ms = max(0.0, upload_ms - item.duration_ms) + rtt_ms
    return simulated_batch_after_end_ms, simulated_stream_transport_tail_ms, upload_bytes


def with_network_profile(args: argparse.Namespace, item: AudioItem, result: ASRResult, network_profile: str) -> ASRResult:
    sim_batch, sim_stream_tail, upload_bytes = simulated_network_metrics(args, item, result.request_ms, network_profile)
    return replace(
        result,
        network_profile=network_profile,
        simulated_batch_after_end_ms=sim_batch,
        simulated_stream_transport_tail_ms=sim_stream_tail,
        simulated_upload_bytes=upload_bytes,
    )


def result_from_exception(mode: str, item: AudioItem, start: float, error: Exception, network_profile: str) -> ASRResult:
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return ASRResult(
        mode=mode,
        request_id=item.request_id,
        ok=False,
        expected_text=item.text,
        recognized_text="",
        exact_match=False,
        normalized_match=False,
        duration_ms=item.duration_ms,
        audio_bytes=item.pcm_bytes,
        request_ms=elapsed_ms,
        final_from_speech_start_ms=None,
        final_after_audio_end_ms=None,
        first_partial_ms=None,
        char_error_rate=None,
        simulated_batch_after_end_ms=None,
        simulated_stream_transport_tail_ms=None,
        simulated_upload_bytes=None,
        network_profile=network_profile,
        status="error",
        error=str(error),
    )


def run_vllm_batch(args: argparse.Namespace, item: AudioItem, network_profile: str) -> ASRResult:
    mode = "vllm_batch"
    start = time.perf_counter()
    try:
        wav_bytes = Path(item.wav_path).read_bytes()
        response = httpx.post(
            f"{args.asr_base_url.rstrip('/')}/audio/transcriptions"
            if args.asr_base_url.rstrip("/").endswith("/v1")
            else f"{args.asr_base_url.rstrip('/')}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {args.asr_api_key}"},
            data={
                "model": args.asr_model,
                "response_format": "json",
                "temperature": "0",
            },
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            timeout=args.timeout,
        )
        request_ms = (time.perf_counter() - start) * 1000.0
        response.raise_for_status()
        recognized = extract_text(response.json())
        return ASRResult(
            mode=mode,
            request_id=item.request_id,
            ok=bool(recognized),
            expected_text=item.text,
            recognized_text=recognized,
            exact_match=recognized == item.text,
            normalized_match=normalize_text(recognized) == normalize_text(item.text),
            duration_ms=item.duration_ms,
            audio_bytes=item.pcm_bytes,
            request_ms=request_ms,
            final_from_speech_start_ms=item.duration_ms + request_ms,
            final_after_audio_end_ms=request_ms,
            first_partial_ms=None,
            char_error_rate=char_error_rate(item.text, recognized),
            simulated_batch_after_end_ms=None,
            simulated_stream_transport_tail_ms=None,
            simulated_upload_bytes=None,
            network_profile=network_profile,
            status=str(response.status_code),
        )
    except Exception as exc:
        return result_from_exception(mode, item, start, exc, network_profile)


def run_stt_grpc_unary(args: argparse.Namespace, item: AudioItem, network_profile: str) -> ASRResult:
    mode = "stt_grpc_unary"
    start = time.perf_counter()
    try:
        pcm, sample_rate, _ = read_wav_pcm(Path(item.wav_path))
        with grpc.insecure_channel(args.stt_grpc_target) as channel:
            stub = stt_service_pb2_grpc.STTServiceStub(channel)
            response = stub.RecognizeSpeech(
                stt_service_pb2.AudioRequest(
                    audio_data=pcm,
                    format="pcm",
                    sample_rate=sample_rate,
                    language=args.asr_language,
                ),
                timeout=args.timeout,
            )
        request_ms = (time.perf_counter() - start) * 1000.0
        recognized = response.text.strip()
        return ASRResult(
            mode=mode,
            request_id=item.request_id,
            ok=bool(recognized),
            expected_text=item.text,
            recognized_text=recognized,
            exact_match=recognized == item.text,
            normalized_match=normalize_text(recognized) == normalize_text(item.text),
            duration_ms=item.duration_ms,
            audio_bytes=item.pcm_bytes,
            request_ms=request_ms,
            final_from_speech_start_ms=item.duration_ms + request_ms,
            final_after_audio_end_ms=request_ms,
            first_partial_ms=None,
            char_error_rate=char_error_rate(item.text, recognized),
            simulated_batch_after_end_ms=None,
            simulated_stream_transport_tail_ms=None,
            simulated_upload_bytes=None,
            network_profile=network_profile,
            status="ok",
        )
    except Exception as exc:
        return result_from_exception(mode, item, start, exc, network_profile)


def iter_audio_chunks(pcm: bytes, sample_rate: int, chunk_ms: int, realtime: bool) -> Iterable[stt_service_pb2.AudioChunk]:
    bytes_per_ms = sample_rate * 2 / 1000.0
    chunk_bytes = max(2, int(bytes_per_ms * chunk_ms))
    if chunk_bytes % 2:
        chunk_bytes += 1
    started = time.perf_counter()
    for offset in range(0, len(pcm), chunk_bytes):
        if realtime and offset > 0:
            expected_elapsed = offset / (sample_rate * 2) * 1000.0
            actual_elapsed = (time.perf_counter() - started) * 1000.0
            delay_ms = expected_elapsed - actual_elapsed
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
        yield stt_service_pb2.AudioChunk(data=pcm[offset : offset + chunk_bytes], is_final=False)
    yield stt_service_pb2.AudioChunk(data=b"", is_final=True)


def run_stt_grpc_stream_collect(args: argparse.Namespace, item: AudioItem, network_profile: str) -> ASRResult:
    mode = "stt_grpc_stream_collect"
    start = time.perf_counter()
    try:
        pcm, sample_rate, _ = read_wav_pcm(Path(item.wav_path))
        with grpc.insecure_channel(args.stt_grpc_target) as channel:
            stub = stt_service_pb2_grpc.STTServiceStub(channel)
            responses = list(
                stub.StreamRecognize(
                    iter_audio_chunks(pcm, sample_rate, args.chunk_ms, args.realtime_stream_send),
                    timeout=max(args.timeout, item.duration_ms / 1000.0 + args.timeout),
                )
            )
        request_ms = (time.perf_counter() - start) * 1000.0
        recognized = responses[-1].text.strip() if responses else ""
        return ASRResult(
            mode=mode,
            request_id=item.request_id,
            ok=bool(recognized),
            expected_text=item.text,
            recognized_text=recognized,
            exact_match=recognized == item.text,
            normalized_match=normalize_text(recognized) == normalize_text(item.text),
            duration_ms=item.duration_ms,
            audio_bytes=item.pcm_bytes,
            request_ms=request_ms,
            final_from_speech_start_ms=request_ms,
            final_after_audio_end_ms=max(0.0, request_ms - item.duration_ms),
            first_partial_ms=None,
            char_error_rate=char_error_rate(item.text, recognized),
            simulated_batch_after_end_ms=None,
            simulated_stream_transport_tail_ms=None,
            simulated_upload_bytes=None,
            network_profile=network_profile,
            status="collect_then_recognize",
        )
    except Exception as exc:
        return result_from_exception(mode, item, start, exc, network_profile)


async def probe_streaming_endpoint(args: argparse.Namespace, out_dir: Path) -> dict[str, Any]:
    urls = args.streaming_ws_url or [
        args.asr_base_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + suffix
        for suffix in (
            "/audio/transcriptions/stream",
            "/audio/transcriptions",
            "/realtime",
            "/audio/asr/stream",
            "/asr/stream",
        )
    ]
    attempts: list[dict[str, str]] = []
    for url in urls:
        try:
            async with websockets.connect(
                url,
                additional_headers=[("Authorization", f"Bearer {args.asr_api_key}")],
                open_timeout=2,
            ):
                attempts.append({"url": url, "status": "connected"})
        except Exception as exc:
            attempts.append({"url": url, "status": type(exc).__name__, "error": str(exc)[:300]})
    report = {
        "streaming_available": any(attempt["status"] == "connected" for attempt in attempts),
        "attempts": attempts,
    }
    (out_dir / "streaming_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def summarize_results(results: list[ASRResult]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    groups = sorted({(result.mode, result.network_profile) for result in results})
    for mode, network_profile in groups:
        scoped = [
            result
            for result in results
            if result.mode == mode and result.network_profile == network_profile
        ]
        ok = [result for result in scoped if result.ok]

        def metric(name: str) -> list[float]:
            values: list[float] = []
            for result in ok:
                value = getattr(result, name)
                if isinstance(value, (int, float)):
                    values.append(float(value))
            return values

        key = f"{mode}|{network_profile}"
        summary[key] = {
            "mode": mode,
            "network_profile": network_profile,
            "total": len(scoped),
            "ok": len(ok),
            "failed": len(scoped) - len(ok),
            "exact_match": sum(1 for result in ok if result.exact_match),
            "normalized_match": sum(1 for result in ok if result.normalized_match),
            "metrics": {
                "request_ms": summarize_numeric(metric("request_ms")),
                "final_after_audio_end_ms": summarize_numeric(metric("final_after_audio_end_ms")),
                "final_from_speech_start_ms": summarize_numeric(metric("final_from_speech_start_ms")),
                "first_partial_ms": summarize_numeric(metric("first_partial_ms")),
                "char_error_rate": summarize_numeric(metric("char_error_rate"), digits=4),
                "simulated_batch_after_end_ms": summarize_numeric(metric("simulated_batch_after_end_ms")),
                "simulated_stream_transport_tail_ms": summarize_numeric(metric("simulated_stream_transport_tail_ms")),
                "simulated_upload_bytes": summarize_numeric(metric("simulated_upload_bytes")),
            },
            "errors": [result.error for result in scoped if result.error][:5],
        }
    return summary


def write_outputs(args: argparse.Namespace, out_dir: Path, items: list[AudioItem], results: list[ASRResult], probe: dict[str, Any]) -> None:
    summary = summarize_results(results)
    manifest = {
        "schema_version": "asr-streaming-benchmark/v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "label": args.label,
        "rounds": args.rounds,
        "seed": args.seed,
        "asr": {
            "base_url": args.asr_base_url,
            "model": args.asr_model,
            "sample_rate": args.asr_sample_rate,
        },
        "tts": {
            "ws_url": args.tts_ws_url,
            "model": args.tts_model,
            "voice": args.tts_voice,
            "sample_rate": args.tts_sample_rate,
        },
        "network_profiles": args.network_profile,
        "network_simulation": {
            "audio_codec": args.network_audio_codec,
            "opus_bitrate_kbps": args.opus_bitrate_kbps,
        },
        "streaming_probe": probe,
        "summary": summary,
        "audio_items": [asdict(item) for item in items],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with (out_dir / "results.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(asdict(results[0]).keys()) if results else [])
        if results:
            writer.writeheader()
            for result in results:
                writer.writerow(asdict(result))

    lines = [
        f"# ASR Streaming Benchmark - {args.label}",
        "",
        f"- Generated: `{manifest['generated_at']}`",
        f"- Rounds: `{args.rounds}`",
        f"- ASR: `{args.asr_base_url}` / `{args.asr_model}`",
        f"- TTS: `{args.tts_ws_url}` / `{args.tts_model}` / `{args.tts_voice}`",
        f"- Streaming probe available: `{probe.get('streaming_available')}`",
        "",
        "## Results",
        "",
        "| mode | network | ok | normalized match | CER avg/p95 | request p50/p95 | final after audio end p50/p95 | simulated upload p50 | simulated batch after end p50 | simulated stream transport tail p50 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for _, stats in summary.items():
        metrics = stats["metrics"]
        lines.append(
            f"| `{stats['mode']}` | `{stats['network_profile']}` | {stats['ok']}/{stats['total']} | {stats['normalized_match']}/{stats['ok']} | "
            f"{metrics['char_error_rate']['avg']}/{metrics['char_error_rate']['p95']} | "
            f"{metrics['request_ms']['p50']}/{metrics['request_ms']['p95']} | "
            f"{metrics['final_after_audio_end_ms']['p50']}/{metrics['final_after_audio_end_ms']['p95']} | "
            f"{metrics['simulated_upload_bytes']['p50']} | "
            f"{metrics['simulated_batch_after_end_ms']['p50']} | "
            f"{metrics['simulated_stream_transport_tail_ms']['p50']} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation Notes",
            "",
            "- `vllm_batch` is the OpenAI-compatible `/v1/audio/transcriptions` path.",
            "- `stt_grpc_stream_collect` is not true Streaming ASR; the current server collects chunks and recognizes once after `is_final`.",
            "- `first_partial_ms` stays empty unless a real streaming partial/final ASR endpoint is added.",
            f"- Simulated network columns estimate device-to-Gateway media upload tail only; default codec is `{args.network_audio_codec}` at `{args.opus_bitrate_kbps}` kbps for Opus.",
            "- Simulated network columns do not prove model-side streaming behavior.",
            "",
        ]
    )
    if probe.get("attempts"):
        lines.extend(["## Streaming Probe", ""])
        for attempt in probe["attempts"]:
            lines.append(f"- `{attempt['url']}` -> `{attempt['status']}`")
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def async_main(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir) / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    items = await build_audio_dataset(args, out_dir)
    probe = await probe_streaming_endpoint(args, out_dir) if args.probe_streaming else {"streaming_available": False, "attempts": []}

    base_results: list[tuple[AudioItem, ASRResult]] = []
    for item in items:
        print(f"[asr] vllm_batch {item.request_id}/{len(items)}", flush=True)
        base_results.append((item, run_vllm_batch(args, item, "none")))
        if args.include_stt_grpc_unary:
            print(f"[asr] stt_grpc_unary {item.request_id}/{len(items)}", flush=True)
            base_results.append((item, run_stt_grpc_unary(args, item, "none")))
        if args.include_stt_grpc_stream:
            print(f"[asr] stt_grpc_stream_collect {item.request_id}/{len(items)}", flush=True)
            base_results.append((item, run_stt_grpc_stream_collect(args, item, "none")))

    results: list[ASRResult] = []
    for item, result in base_results:
        for profile in args.network_profile:
            results.append(with_network_profile(args, item, result, profile))

    write_outputs(args, out_dir, items, results, probe)
    print(f"Wrote ASR benchmark artifacts to {out_dir}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Qwen3-ASR batch and streaming readiness.")
    parser.add_argument("--label", default=datetime.now().strftime("%Y-%m-%d_qwen3-asr-streaming-readiness"))
    parser.add_argument("--out-dir", default="tmp/asr_streaming_bench")
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--seed", type=int, default=625110)
    parser.add_argument("--min-chars", type=int, default=6)
    parser.add_argument("--max-chars", type=int, default=45)
    parser.add_argument("--timeout", type=float, default=60.0)

    parser.add_argument("--asr-base-url", default=os.getenv("ASR_BASE_URL") or os.getenv("QWEN_ASR_BASE_URL") or "http://127.0.0.1:15110/v1")
    parser.add_argument("--asr-api-key", default=os.getenv("ASR_API_KEY") or os.getenv("QWEN_ASR_API_KEY") or "")
    parser.add_argument("--asr-model", default=os.getenv("ASR_MODEL") or os.getenv("QWEN_ASR_MODEL") or "Qwen3-ASR-1.7B")
    parser.add_argument("--asr-language", default="")
    parser.add_argument("--asr-sample-rate", type=int, default=16000)

    parser.add_argument("--tts-ws-url", default=os.getenv("QWEN3_TTS_CUSTOM_VOICE_WS_URL") or "ws://127.0.0.1:15120/v1/audio/speech/stream")
    parser.add_argument("--tts-api-key", default=os.getenv("QWEN3_TTS_CUSTOM_VOICE_API_KEY") or "")
    parser.add_argument("--tts-model", default=os.getenv("QWEN3_TTS_CUSTOM_VOICE_MODEL") or "qwen3-tts")
    parser.add_argument("--tts-voice", default=os.getenv("LOCAL_QWEN3_TTS_VOICE") or "serena")
    parser.add_argument("--tts-language", default=os.getenv("LOCAL_QWEN3_TTS_LANGUAGE") or "Chinese")
    parser.add_argument("--tts-task-type", default=os.getenv("LOCAL_QWEN3_TTS_TASK_TYPE") or "CustomVoice")
    parser.add_argument(
        "--tts-instructions",
        default=os.getenv("LOCAL_QWEN3_TTS_INSTRUCTIONS") or "自然、温柔、稳定、口语化，语速适中，情绪轻微，不夸张。",
    )
    parser.add_argument("--tts-sample-rate", type=int, default=24000)
    parser.add_argument("--tts-input-delay-ms", type=float, default=0.0)
    parser.add_argument("--regenerate-audio", action="store_true")

    parser.add_argument("--probe-streaming", action="store_true")
    parser.add_argument("--streaming-ws-url", action="append")
    parser.add_argument("--network-profile", action="append", default=None, help="Use none or name:rtt_ms,kbps, e.g. office:20,512")
    parser.add_argument("--network-audio-codec", choices=("opus", "pcm16"), default=os.getenv("ASR_BENCH_NETWORK_AUDIO_CODEC") or "opus")
    parser.add_argument("--opus-bitrate-kbps", type=float, default=float(os.getenv("ASR_BENCH_OPUS_BITRATE_KBPS") or "16"))
    parser.add_argument("--include-stt-grpc-unary", action="store_true")
    parser.add_argument("--include-stt-grpc-stream", action="store_true")
    parser.add_argument("--stt-grpc-target", default="127.0.0.1:50054")
    parser.add_argument("--chunk-ms", type=int, default=40)
    parser.add_argument("--realtime-stream-send", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.network_profile is None:
        args.network_profile = ["none"]
    args.network_profile = list(dict.fromkeys(args.network_profile))
    if not args.asr_api_key:
        raise SystemExit("ASR API key is required via --asr-api-key or ASR_API_KEY/QWEN_ASR_API_KEY")
    if not args.tts_api_key:
        raise SystemExit("TTS API key is required via --tts-api-key or QWEN3_TTS_CUSTOM_VOICE_API_KEY")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
