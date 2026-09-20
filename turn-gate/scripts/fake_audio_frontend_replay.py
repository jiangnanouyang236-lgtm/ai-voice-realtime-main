#!/usr/bin/env python3
"""Replay a fixed WAV through the Rust audio_frontend TCP contract.

This is a test-only local fixture: it accepts RustClient mic/speaker TCP
connections, triggers the local HTTP wake endpoint, then sends real-time 48 kHz
mic frames converted from a 16 kHz mono PCM16 WAV.
"""

from __future__ import annotations

import argparse
import socket
import struct
import threading
import time
import urllib.request
import wave
from pathlib import Path


MAGIC = 0x30445541
VERSION = 1
HEADER_BYTES = 48
FRAME_TYPE_MIC = 1
FORMAT_S16LE = 1
WIRE_SAMPLE_RATE = 48_000
WIRE_CHANNELS = 1
WIRE_FRAME_MS = 10
WIRE_SAMPLES = 480
WIRE_PAYLOAD_BYTES = 960
HEADER = struct.Struct("<IHHHHIHHIQQII")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--mic-port", type=int, default=39001)
    parser.add_argument("--speaker-port", type=int, default=39002)
    parser.add_argument("--wake-url", default="http://127.0.0.1:5202/wakeup")
    parser.add_argument("--wake-delay-sec", type=float, default=3.0)
    parser.add_argument("--speech-delay-sec", type=float, default=8.0)
    parser.add_argument("--leading-silence-ms", type=int, default=300)
    parser.add_argument("--trailing-silence-ms", type=int, default=700)
    parser.add_argument("--inject-silence-at-ms", type=int, default=0)
    parser.add_argument("--inject-silence-ms", type=int, default=0)
    parser.add_argument("--linger-sec", type=float, default=8.0)
    return parser.parse_args()


def load_frames(
    path: Path,
    leading_ms: int,
    trailing_ms: int,
    inject_at_ms: int,
    inject_ms: int,
) -> list[bytes]:
    with wave.open(str(path), "rb") as handle:
        if (handle.getnchannels(), handle.getframerate(), handle.getsampwidth()) != (1, 16_000, 2):
            raise ValueError("sample must be 16 kHz mono PCM16 WAV")
        pcm = handle.readframes(handle.getnframes())
    samples = list(struct.unpack(f"<{len(pcm) // 2}h", pcm))
    if inject_at_ms > 0 and inject_ms > 0:
        inject_at = min(len(samples), inject_at_ms * 16)
        samples[inject_at:inject_at] = [0] * (inject_ms * 16)
    samples = [0] * (leading_ms * 16) + samples + [0] * (trailing_ms * 16)
    frames: list[bytes] = []
    for offset in range(0, len(samples), 160):
        frame = samples[offset : offset + 160]
        if len(frame) < 160:
            frame.extend([0] * (160 - len(frame)))
        upsampled = [value for value in frame for _ in range(3)]
        frames.append(struct.pack("<480h", *upsampled))
    return frames


def listen(host: str, port: int) -> socket.socket:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(1)
    return server


def drain_speaker(server: socket.socket, stop: threading.Event) -> None:
    connection, _ = server.accept()
    print("speaker_connected=true", flush=True)
    connection.settimeout(0.2)
    total = 0
    while not stop.is_set():
        try:
            data = connection.recv(64 * 1024)
        except socket.timeout:
            continue
        if not data:
            break
        total += len(data)
    print(f"speaker_bytes={total}", flush=True)


def trigger_wake(url: str) -> None:
    request = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=3) as response:
        body = response.read().decode("utf-8", errors="replace")
    print(f"wake_response={body}", flush=True)


def send_mic_frames(connection: socket.socket, frames: list[bytes]) -> None:
    started = time.monotonic()
    for index, payload in enumerate(frames, start=1):
        timestamp_us = int((time.monotonic() - started) * 1_000_000)
        header = HEADER.pack(
            MAGIC,
            VERSION,
            HEADER_BYTES,
            FRAME_TYPE_MIC,
            FORMAT_S16LE,
            WIRE_SAMPLE_RATE,
            WIRE_CHANNELS,
            WIRE_FRAME_MS,
            WIRE_SAMPLES,
            index,
            timestamp_us,
            WIRE_PAYLOAD_BYTES,
            0,
        )
        connection.sendall(header + payload)
        deadline = started + index * WIRE_FRAME_MS / 1000
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)


def main() -> int:
    args = parse_args()
    frames = load_frames(
        args.sample,
        args.leading_silence_ms,
        args.trailing_silence_ms,
        args.inject_silence_at_ms,
        args.inject_silence_ms,
    )
    mic_server = listen(args.host, args.mic_port)
    speaker_server = listen(args.host, args.speaker_port)
    stop = threading.Event()
    speaker_thread = threading.Thread(
        target=drain_speaker,
        args=(speaker_server, stop),
        daemon=True,
    )
    speaker_thread.start()
    print(
        f"fake_audio_frontend_ready=true mic_port={args.mic_port} speaker_port={args.speaker_port}",
        flush=True,
    )
    mic_connection, _ = mic_server.accept()
    print("mic_connected=true", flush=True)
    time.sleep(args.wake_delay_sec)
    trigger_wake(args.wake_url)
    time.sleep(args.speech_delay_sec)
    send_mic_frames(mic_connection, frames)
    print(f"mic_replay_done=true frames={len(frames)}", flush=True)
    time.sleep(args.linger_sec)
    stop.set()
    mic_connection.close()
    mic_server.close()
    speaker_server.close()
    speaker_thread.join(timeout=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
