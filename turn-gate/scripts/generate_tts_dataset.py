#!/usr/bin/env python3
"""Generate isolated Smart Turn candidate WAVs through the existing TTS gRPC service."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
import wave
from pathlib import Path
from typing import Any

import grpc

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from tts import tts_service_pb2, tts_service_pb2_grpc  # noqa: E402


def write_wav(path: Path, pcm: bytes, sample_rate: int, channels: int, sample_width: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


def read_wav(path: Path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as handle:
        return (
            handle.readframes(handle.getnframes()),
            handle.getframerate(),
            handle.getnchannels(),
            handle.getsampwidth(),
        )


def synthesize(
    stub: Any,
    text: str,
    profile_id: str,
    timeout: float,
) -> tuple[bytes, int, int, int]:
    session_id = f"turn-gate-{uuid.uuid4()}"

    def requests():
        yield tts_service_pb2.TextChunk(
            text=text,
            is_final=False,
            session_id=session_id,
            config=tts_service_pb2.TTSConfig(tts_profile_id=profile_id),
        )
        yield tts_service_pb2.TextChunk(text="", is_final=True, session_id=session_id)

    pcm = bytearray()
    audio_format: tuple[int, int, int] | None = None
    for chunk in stub.StreamTextToSpeech(requests(), timeout=timeout):
        if not chunk.audio_data:
            continue
        current = (chunk.sample_rate, chunk.channels or 1, chunk.sample_width or 2)
        if audio_format is None:
            audio_format = current
        elif current != audio_format:
            raise ValueError(f"TTS changed audio format within one utterance: {audio_format} -> {current}")
        pcm.extend(chunk.audio_data)
    if not pcm or audio_format is None:
        raise RuntimeError(f"TTS returned no audio for text: {text}")
    return bytes(pcm), *audio_format


def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--grpc-target", default="127.0.0.1:50052")
    parser.add_argument("--tts-profile-id", default="wzk-base")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    cases = load_cases(args.dataset)
    audio_dir = args.out_dir / "audio"
    segment_dir = audio_dir / "_segments"
    output_manifest = args.out_dir / "manifest.jsonl"
    args.out_dir.mkdir(parents=True, exist_ok=True)

    channel = grpc.insecure_channel(args.grpc_target)
    grpc.channel_ready_future(channel).result(timeout=5)
    stub = tts_service_pb2_grpc.TTSServiceStub(channel)
    segment_cache: dict[str, tuple[bytes, int, int, int]] = {}
    generated: list[dict[str, Any]] = []

    for case in cases:
        pieces: list[tuple[str, bytes | int, tuple[int, int, int] | None]] = []
        candidate = case["candidate_after_segment"]
        for segment in case["segments"][: candidate + 1]:
            if "silence_ms" in segment:
                pieces.append(("silence", int(segment["silence_ms"]), None))
                continue
            text = segment["text"]
            key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            if key not in segment_cache:
                segment_path = segment_dir / f"{key}.wav"
                if segment_path.exists():
                    pcm, rate, channels, width = read_wav(segment_path)
                else:
                    pcm, rate, channels, width = synthesize(
                        stub, text, args.tts_profile_id, args.timeout
                    )
                    write_wav(segment_path, pcm, rate, channels, width)
                segment_cache[key] = (pcm, rate, channels, width)
            pcm, rate, channels, width = segment_cache[key]
            pieces.append(("audio", pcm, (rate, channels, width)))

        formats = [item[2] for item in pieces if item[0] == "audio"]
        if not formats:
            raise ValueError(f"{case['id']}: candidate contains no speech")
        if len(set(formats)) != 1:
            raise ValueError(f"{case['id']}: inconsistent TTS formats")
        rate, channels, width = formats[0]  # type: ignore[misc]
        if (rate, channels, width) != (16000, 1, 2):
            raise ValueError(
                f"{case['id']}: Smart Turn requires 16kHz mono PCM16, got {(rate, channels, width)}"
            )

        if pieces[-1][0] != "silence":
            pieces.append(("silence", int(case["trailing_silence_ms"]), None))
        pcm_out = bytearray()
        for kind, payload, _ in pieces:
            if kind == "audio":
                pcm_out.extend(payload)  # type: ignore[arg-type]
            else:
                frames = round(int(payload) * rate / 1000)
                pcm_out.extend(b"\x00" * frames * channels * width)

        output_wav = audio_dir / f"{case['id']}.wav"
        write_wav(output_wav, bytes(pcm_out), rate, channels, width)
        result = dict(case)
        result["audio_path"] = f"audio/{case['id']}.wav"
        result["tts_profile_id"] = args.tts_profile_id
        result["sample_rate"] = rate
        result["channels"] = channels
        result["sample_width"] = width
        generated.append(result)
        print(f"generated {case['id']}: {len(pcm_out) / (rate * channels * width):.3f}s")

    with output_manifest.open("w", encoding="utf-8") as handle:
        for row in generated:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"manifest={output_manifest} cases={len(generated)} segments={len(segment_cache)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
