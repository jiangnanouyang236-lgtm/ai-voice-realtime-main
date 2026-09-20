#!/usr/bin/env python3
"""Minimal localhost MQTT 3.1.1 broker fixture for Robot MCP integration evidence.

This is deliberately not a production broker. It accepts CONNECT, QoS 0/1
PUBLISH, PINGREQ, and DISCONNECT, and records redacted JSON payloads as JSONL.
"""

from __future__ import annotations

import argparse
import json
import socketserver
import threading
from datetime import datetime
from pathlib import Path
from typing import BinaryIO


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise EOFError("unexpected MQTT connection close")
    return data


def _read_remaining_length(stream: BinaryIO) -> int:
    multiplier = 1
    value = 0
    for _ in range(4):
        encoded = _read_exact(stream, 1)[0]
        value += (encoded & 0x7F) * multiplier
        if encoded & 0x80 == 0:
            return value
        multiplier *= 128
    raise ValueError("invalid MQTT remaining length")


class MQTTFixtureBroker(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], record_path: Path):
        self.record_path = record_path
        self.record_lock = threading.Lock()
        record_path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(address, MQTTFixtureHandler)

    def record_publish(self, *, topic: str, payload: bytes) -> None:
        try:
            decoded_payload = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded_payload = {"non_json_payload_bytes": len(payload)}
        record = {
            "observed_at": datetime.now().astimezone().isoformat(),
            "topic": topic,
            "payload": decoded_payload,
        }
        with self.record_lock:
            with self.record_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class MQTTFixtureHandler(socketserver.StreamRequestHandler):
    server: MQTTFixtureBroker

    def handle(self) -> None:
        while True:
            first = self.rfile.read(1)
            if not first:
                return
            header = first[0]
            packet_type = header >> 4
            remaining_length = _read_remaining_length(self.rfile)
            body = _read_exact(self.rfile, remaining_length)
            if packet_type == 1:  # CONNECT
                self.wfile.write(b"\x20\x02\x00\x00")
                self.wfile.flush()
            elif packet_type == 3:  # PUBLISH
                if len(body) < 2:
                    raise ValueError("invalid MQTT PUBLISH topic")
                topic_length = int.from_bytes(body[:2], "big")
                topic_end = 2 + topic_length
                topic = body[2:topic_end].decode("utf-8")
                qos = (header >> 1) & 0x03
                payload_offset = topic_end
                packet_id = b""
                if qos:
                    packet_id = body[payload_offset : payload_offset + 2]
                    payload_offset += 2
                self.server.record_publish(topic=topic, payload=body[payload_offset:])
                if qos == 1 and len(packet_id) == 2:
                    self.wfile.write(b"\x40\x02" + packet_id)
                    self.wfile.flush()
            elif packet_type == 12:  # PINGREQ
                self.wfile.write(b"\xd0\x00")
                self.wfile.flush()
            elif packet_type == 14:  # DISCONNECT
                return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18883)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    broker = MQTTFixtureBroker((args.host, args.port), args.record.resolve())
    print(f"MQTT fixture ready: {args.host}:{broker.server_address[1]}", flush=True)
    try:
        broker.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        broker.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
