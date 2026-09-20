#!/usr/bin/env python3
"""Send one Opus-stream utterance directly to the Python Gateway /ws endpoint."""

from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
import sys
from typing import Any

import websocket

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.audio_protocol import AudioValidationError, decode_audio_frame, encode_audio_frame
from gateway.opus_audio import build_opus_packet_stream


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def load_packets(path: Path) -> list[bytes]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    packets = payload.get("packets")
    if not isinstance(packets, list) or not packets:
        raise ValueError(f"packet file has no packets: {path}")
    return [base64.b64decode(item) for item in packets]


def recv_message(ws: websocket.WebSocket, timeout: float) -> dict[str, Any]:
    ws.settimeout(timeout)
    raw = ws.recv()
    if isinstance(raw, bytes):
        try:
            header, payload = decode_audio_frame(raw)
        except AudioValidationError as exc:
            return {
                "type": "<binary>",
                "bytes": len(raw),
                "prefix": raw[:8].hex(),
                "decode_error": str(exc),
            }
        return {
            "type": "<binary>",
            "bytes": len(payload),
            "prefix": raw[:8].hex(),
            "header": header,
        }
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected JSON payload: {payload!r}")
    return payload


def recv_until_type(ws: websocket.WebSocket, want_type: str, timeout: float) -> dict[str, Any]:
    while True:
        message = recv_message(ws, timeout)
        message_type = message.get("type")
        if message_type == want_type:
            return message
        if message_type == "error":
            raise RuntimeError(f"gateway error: {message}")


def send_json(ws: websocket.WebSocket, payload: dict[str, Any]) -> None:
    ws.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def run_text_probe(
    *,
    ws_url: str,
    robot_id: str,
    robot_secret: str,
    bot_id: str,
    text: str,
    trace_id: str,
    timeout: float,
    client_type: str = "direct_text_harness",
) -> dict[str, Any]:
    origin = ws_url.replace("ws://", "http://").replace("wss://", "https://")
    ws = websocket.create_connection(ws_url, origin=origin, timeout=timeout)
    started = time.perf_counter()
    messages: list[dict[str, Any]] = []
    audio_frames: list[dict[str, Any]] = []
    try:
        connected = recv_until_type(ws, "connected", timeout)
        session_id = str(connected.get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError("connected 缺少 session_id")

        register_payload = {
            "type": "register",
            "robot_id": robot_id,
            "client_type": client_type,
        }
        if robot_secret:
            register_payload["robot_secret"] = robot_secret
        send_json(ws, register_payload)
        registered = recv_until_type(ws, "registered", timeout)
        if registered.get("bot_id") != bot_id:
            raise RuntimeError("Gateway 注册返回的 Bot 与 Runtime Snapshot 不一致")

        send_json(
            ws,
            {
                "type": "text",
                "content": text,
                "source": "codex_hybrid_harness",
                "trace_id": trace_id,
            },
        )
        while True:
            message = recv_message(ws, timeout)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if message.get("type") == "<binary>":
                audio_frames.append(message)
            else:
                messages.append(
                    {
                        "type": message.get("type"),
                        "elapsed_ms": elapsed_ms,
                        "message": message,
                    }
                )
            if message.get("type") == "done":
                break
            if message.get("type") == "error":
                raise RuntimeError(f"Gateway direct text error: {message}")

        return {
            "ok": True,
            "session_id": session_id,
            "robot_id": robot_id,
            "bot_id": registered.get("bot_id"),
            "bot_name": registered.get("bot_name"),
            "trace_id": trace_id,
            "input_text": text,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "messages": messages,
            "audio_frames": audio_frames,
        }
    finally:
        ws.close()


def build_chunk_frame(
    packets: list[bytes],
    *,
    utterance_id: str,
    chunk_seq: int,
    robot_id: str,
    sample_rate: int,
) -> bytes:
    return encode_audio_frame(
        build_opus_packet_stream(packets),
        direction="client_input",
        encoding="opus",
        robot_id=robot_id,
        utterance_id=utterance_id,
        chunk_seq=chunk_seq,
        seq=chunk_seq,
        stream_event="chunk",
        sample_rate=sample_rate,
        channels=1,
        duration_ms=len(packets) * 20,
        opus_frame_ms=20,
        packet_count=len(packets),
    )


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    packets = load_packets(args.packets_path)
    utterance_id = args.utterance_id or f"direct-{now_ms()}"
    trace_id = f"audio-{utterance_id}"
    origin = args.origin or args.ws_url.replace("ws://", "http://").replace("wss://", "https://")
    ws = websocket.create_connection(args.ws_url, origin=origin, timeout=args.timeout)
    started = time.perf_counter()
    first_binary_ms: int | None = None
    playback_start_ms: int | None = None
    done_ms: int | None = None
    binary_messages = 0
    binary_bytes = 0
    text_messages: list[dict[str, Any]] = []
    round_id = ""
    playback_id = ""
    try:
        connected = recv_until_type(ws, "connected", args.timeout)
        session_id = str(connected.get("session_id") or "")
        if not session_id:
            raise RuntimeError(f"connected missing session_id: {connected}")

        register_payload = {
            "type": "register",
            "robot_id": args.robot_id,
            "client_type": args.client_type,
        }
        if args.robot_secret:
            register_payload["robot_secret"] = args.robot_secret
        send_json(ws, register_payload)
        registered = recv_until_type(ws, "registered", args.timeout)
        registered_bot_id = registered.get("bot_id")
        registered_bot_name = registered.get("bot_name")

        send_json(
            ws,
            {
                "type": "audio_start",
                "utterance_id": utterance_id,
                "trace_id": trace_id,
                "sample_rate": args.sample_rate,
                "channels": 1,
                "opus_frame_ms": 20,
                "packet_count": len(packets),
                "audio_transport": "binary_stream",
                "audio_encoding": "opus",
            },
        )
        sent_packets = 0
        chunk_seq = 0
        for offset in range(0, len(packets), args.packets_per_chunk):
            chunk = packets[offset : offset + args.packets_per_chunk]
            chunk_seq += 1
            ws.send_binary(
                build_chunk_frame(
                    chunk,
                    utterance_id=utterance_id,
                    chunk_seq=chunk_seq,
                    robot_id=args.robot_id,
                    sample_rate=args.sample_rate,
                )
            )
            sent_packets += len(chunk)
            if args.packet_gap_ms > 0:
                time.sleep(args.packet_gap_ms / 1000.0)
        audio_end_at = time.perf_counter()
        send_json(
            ws,
            {
                "type": "audio_end",
                "utterance_id": utterance_id,
                "trace_id": trace_id,
                "duration_ms": sent_packets * 20,
                "packet_count": sent_packets,
                "audio_transport": "binary_stream",
            },
        )

        while True:
            message = recv_message(ws, args.done_timeout)
            elapsed_ms = int((time.perf_counter() - audio_end_at) * 1000)
            message_type = message.get("type")
            if message_type == "<binary>":
                binary_messages += 1
                binary_bytes += int(message.get("bytes") or 0)
                if first_binary_ms is None:
                    first_binary_ms = elapsed_ms
                continue
            text_messages.append(
                {
                    "type": message_type,
                    "elapsed_ms": elapsed_ms,
                    "message": message,
                }
            )
            if message_type == "playback_start" and playback_start_ms is None:
                playback_start_ms = elapsed_ms
                round_id = str(message.get("round_id") or "")
                playback_id = str(message.get("playback_id") or "")
            if message_type == "done":
                done_ms = elapsed_ms
                break
            if message_type == "error":
                raise RuntimeError(f"gateway error after audio_end: {message}")

        if round_id and playback_id:
            send_json(
                ws,
                {
                    "type": "playback_complete",
                    "round_id": round_id,
                    "playback_id": playback_id,
                    "chunks": binary_messages,
                    "samples": 0,
                    "underruns": 0,
                    "zero_fill_samples": 0,
                    "max_buffered_samples": 0,
                    "first_audio_to_playback_start_ms": 0,
                    "playback_start_to_complete_ms": done_ms or 0,
                },
            )

        return {
            "ok": True,
            "mode": "direct_ws",
            "ws_url": args.ws_url,
            "session_id": session_id,
            "robot_id": args.robot_id,
            "registered_bot_id": registered_bot_id,
            "registered_bot_name": registered_bot_name,
            "utterance_id": utterance_id,
            "trace_id": trace_id,
            "packets": sent_packets,
            "chunks": chunk_seq,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "audio_end_to_playback_start_ms": playback_start_ms,
            "audio_end_to_first_binary_ms": first_binary_ms,
            "audio_end_to_done_ms": done_ms,
            "binary_messages": binary_messages,
            "binary_bytes": binary_bytes,
            "text_messages": text_messages,
        }
    finally:
        ws.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ws-url", default="ws://127.0.0.1:7860/ws")
    parser.add_argument("--origin", default="")
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--robot-secret", default="")
    parser.add_argument("--client-type", default="direct_ws_probe")
    parser.add_argument("--packets-path", type=Path, required=True)
    parser.add_argument("--utterance-id", default="")
    parser.add_argument("--packets-per-chunk", type=int, default=5)
    parser.add_argument("--packet-gap-ms", type=float, default=20.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--done-timeout", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_probe(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
