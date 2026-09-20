#!/usr/bin/env python3
"""Probe Python Gateway status and M1 internal voice WebSocket.

Default behavior is read-only/lightweight: GET /internal/status, then
session.open/session.close on /internal/voice/ws. Use --client-event explicitly
when a TTS-producing active client_event probe is desired.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
import websocket

from gateway.audio_protocol import AudioValidationError, decode_audio_frame, encode_audio_frame


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def redact_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "<set>"
    return f"{value[:3]}...{value[-3:]}"


def derive_urls(base_url: str) -> tuple[str, str]:
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"invalid base URL: {base_url}")
    http_scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
    ws_scheme = "wss" if http_scheme == "https" else "ws"
    root = parsed._replace(scheme=http_scheme, path="", params="", query="", fragment="")
    status_url = urlunparse(root._replace(path="/internal/status"))
    ws_url = urlunparse(root._replace(scheme=ws_scheme, path="/internal/voice/ws"))
    return status_url, ws_url


def envelope(
    event_type: str,
    session_id: str,
    payload: dict[str, Any] | None = None,
    *,
    trace_id: str = "",
    round_id: str = "",
    playback_id: str = "",
    utterance_id: str = "",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": 1,
        "type": event_type,
        "session_id": session_id,
        "timestamp_ms": now_ms(),
        "payload": payload or {},
    }
    if trace_id:
        data["trace_id"] = trace_id
    if round_id:
        data["round_id"] = round_id
    if playback_id:
        data["playback_id"] = playback_id
    if utterance_id:
        data["utterance_id"] = utterance_id
    return data


@dataclass
class TimedResult:
    ok: bool
    elapsed_ms: int
    detail: Any


def get_status(status_url: str, timeout: float) -> TimedResult:
    started = time.perf_counter()
    try:
        response = requests.get(status_url, timeout=timeout)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
        return TimedResult(
            ok=response.ok,
            elapsed_ms=elapsed_ms,
            detail={
                "status_code": response.status_code,
                "body": body,
            },
        )
    except Exception as exc:  # noqa: BLE001 - probe should report any failure.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return TimedResult(ok=False, elapsed_ms=elapsed_ms, detail={"error": str(exc)})


def recv_json(ws: websocket.WebSocket, timeout: float) -> dict[str, Any]:
    ws.settimeout(timeout)
    raw = ws.recv()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return json.loads(raw)


def recv_message(ws: websocket.WebSocket, timeout: float) -> dict[str, Any]:
    ws.settimeout(timeout)
    raw = ws.recv()
    if isinstance(raw, bytes):
        try:
            header, payload = decode_audio_frame(raw)
        except AudioValidationError as exc:
            return {
                "type": "<binary>",
                "payload_bytes": len(raw),
                "prefix": raw[:8].hex(),
                "decode_error": str(exc),
            }
        return {
            "type": "<binary>",
            "payload_bytes": len(payload),
            "prefix": raw[:8].hex(),
            "header": header,
        }
    return json.loads(raw)


def probe_internal_voice(
    ws_url: str,
    *,
    robot_id: str,
    robot_secret: str,
    client_type: str,
    bot_id: str,
    timeout: float,
    client_event: str,
) -> TimedResult:
    session_id = f"probe_{uuid.uuid4().hex[:12]}"
    trace_id = f"probe-{now_ms()}"
    started = time.perf_counter()
    messages: list[dict[str, Any]] = []
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
        try:
            open_payload = {
                "robot_id": robot_id,
                "client_type": client_type,
                "bot_id": bot_id,
                "source": "codex_probe",
                "transport": "probe",
                "bridge_mode": "m1_probe",
            }
            if robot_secret:
                open_payload["robot_secret"] = robot_secret
            ws.send(json.dumps(envelope("session.open", session_id, open_payload), ensure_ascii=False))
            opened = recv_json(ws, timeout)
            messages.append({"phase": "session.open", "message": opened})

            if client_event:
                event_payload = {
                    "event": client_event,
                    "event_id": f"{client_event}-{now_ms()}",
                    "source": "codex_probe",
                    "bot_id": bot_id,
                }
                ws.send(
                    json.dumps(
                        envelope("client_event", session_id, event_payload, trace_id=trace_id),
                        ensure_ascii=False,
                    )
                )
                event_started = time.perf_counter()
                while True:
                    message = recv_message(ws, timeout)
                    messages.append(
                        {
                            "phase": "client_event",
                            "elapsed_ms": int((time.perf_counter() - event_started) * 1000),
                            "message": message,
                        }
                    )
                    if message.get("type") in {
                        "response.done",
                        "response.error",
                        "protocol.error",
                    }:
                        break

            ws.send(json.dumps(envelope("session.close", session_id), ensure_ascii=False))
            closed = recv_json(ws, timeout)
            messages.append({"phase": "session.close", "message": closed})
        finally:
            ws.close()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return TimedResult(
            ok=True,
            elapsed_ms=elapsed_ms,
            detail={
                "session_id": session_id,
                "trace_id": trace_id,
                "messages": messages,
            },
        )
    except Exception as exc:  # noqa: BLE001 - probe should report any failure.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return TimedResult(
            ok=False,
            elapsed_ms=elapsed_ms,
            detail={
                "session_id": session_id,
                "trace_id": trace_id,
                "error": str(exc),
                "messages": messages,
            },
        )


def probe_active_audio(
    ws_url: str,
    *,
    robot_id: str,
    robot_secret: str,
    client_type: str,
    bot_id: str,
    opus_payload: bytes,
    packet_count: int,
    duration_ms: int,
    timeout: float,
    trace_id: str,
) -> TimedResult:
    session_id = f"probe_audio_{uuid.uuid4().hex[:12]}"
    utterance_id = f"utt_{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()
    messages: list[dict[str, Any]] = []
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
        try:
            open_payload = {
                "robot_id": robot_id,
                "client_type": client_type,
                "bot_id": bot_id,
                "source": "codex_probe",
                "transport": "probe",
                "bridge_mode": "m1_active_audio_probe",
            }
            if robot_secret:
                open_payload["robot_secret"] = robot_secret
            ws.send(json.dumps(envelope("session.open", session_id, open_payload), ensure_ascii=False))
            messages.append({"phase": "session.open", "message": recv_json(ws, timeout)})

            ws.send(
                json.dumps(
                    envelope(
                        "input_audio.start",
                        session_id,
                        {
                            "codec": "opus",
                            "packet_format": "OPUSRAW1",
                            "sample_rate": 16000,
                            "channels": 1,
                            "frame_ms": 20,
                        },
                        trace_id=trace_id,
                        utterance_id=utterance_id,
                    ),
                    ensure_ascii=False,
                )
            )
            messages.append({"phase": "input_audio.start", "message": recv_json(ws, timeout)})

            ws.send_binary(
                encode_audio_frame(
                    opus_payload,
                    event_type="input_audio.batch",
                    direction="uplink",
                    session_id=session_id,
                    trace_id=trace_id,
                    utterance_id=utterance_id,
                    encoding="opus",
                    sample_rate=16000,
                    channels=1,
                    opus_frame_ms=20,
                    packet_count=packet_count,
                    payload_bytes=len(opus_payload),
                )
            )
            messages.append({"phase": "input_audio.batch", "message": recv_json(ws, timeout)})

            ws.send(
                json.dumps(
                    envelope(
                        "input_audio.end",
                        session_id,
                        {
                            "packet_count": packet_count,
                            "payload_bytes": len(opus_payload),
                            "duration_ms": duration_ms,
                            "orchestration_mode": "active",
                        },
                        trace_id=trace_id,
                        utterance_id=utterance_id,
                    ),
                    ensure_ascii=False,
                )
            )
            turn_started = time.perf_counter()
            while True:
                message = recv_message(ws, timeout)
                messages.append(
                    {
                        "phase": "input_audio.active",
                        "elapsed_ms": int((time.perf_counter() - turn_started) * 1000),
                        "message": message,
                    }
                )
                if message.get("type") in {
                    "response.done",
                    "response.error",
                    "protocol.error",
                }:
                    break

            ws.send(json.dumps(envelope("session.close", session_id), ensure_ascii=False))
            messages.append({"phase": "session.close", "message": recv_json(ws, timeout)})
        finally:
            ws.close()
        return TimedResult(
            ok=True,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "session_id": session_id,
                "trace_id": trace_id,
                "utterance_id": utterance_id,
                "messages": messages,
            },
        )
    except Exception as exc:  # noqa: BLE001 - probe should report any failure.
        return TimedResult(
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={
                "session_id": session_id,
                "trace_id": trace_id,
                "utterance_id": utterance_id,
                "error": str(exc),
                "messages": messages,
            },
        )


def probe_input_text_sequence(
    ws_url: str,
    *,
    robot_id: str,
    robot_secret: str,
    client_type: str,
    bot_id: str,
    texts: list[str],
    timeout: float,
) -> TimedResult:
    session_id = f"probe_text_{uuid.uuid4().hex[:12]}"
    started = time.perf_counter()
    turns: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    try:
        ws = websocket.create_connection(ws_url, timeout=timeout)
        try:
            open_payload = {
                "robot_id": robot_id,
                "client_type": client_type,
                "bot_id": bot_id,
                "source": "codex_probe",
                "transport": "probe",
                "bridge_mode": "m1_input_text_probe",
            }
            if robot_secret:
                open_payload["robot_secret"] = robot_secret
            ws.send(json.dumps(envelope("session.open", session_id, open_payload), ensure_ascii=False))
            messages.append({"phase": "session.open", "message": recv_json(ws, timeout)})

            for index, text in enumerate(texts, start=1):
                trace_id = f"probe-text-{now_ms()}-{index}"
                utterance_id = f"utt_text_{uuid.uuid4().hex[:12]}"
                ws.send(
                    json.dumps(
                        envelope(
                            "input_text.commit",
                            session_id,
                            {
                                "content": text,
                                "source": "turn_gate_candidate_asr",
                                "bot_id": bot_id,
                                "asr_time_ms": 1.0,
                                "candidate_seq": index,
                                "speech_epoch": index,
                                "audio_watermark": index * 16000,
                            },
                            trace_id=trace_id,
                            utterance_id=utterance_id,
                        ),
                        ensure_ascii=False,
                    )
                )
                turn_messages: list[dict[str, Any]] = []
                turn_started = time.perf_counter()
                while True:
                    message = recv_message(ws, timeout)
                    item = {
                        "phase": "input_text.commit",
                        "turn": index,
                        "elapsed_ms": int((time.perf_counter() - turn_started) * 1000),
                        "message": message,
                    }
                    messages.append(item)
                    turn_messages.append(message)
                    if message.get("type") in {
                        "response.done",
                        "response.error",
                        "protocol.error",
                    }:
                        break
                turns.append(
                    {
                        "turn": index,
                        "trace_id": trace_id,
                        "utterance_id": utterance_id,
                        "text": text,
                        "messages": turn_messages,
                    }
                )

            ws.send(json.dumps(envelope("session.close", session_id), ensure_ascii=False))
            messages.append({"phase": "session.close", "message": recv_json(ws, timeout)})
        finally:
            ws.close()
        return TimedResult(
            ok=True,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"session_id": session_id, "turns": turns, "messages": messages},
        )
    except Exception as exc:  # noqa: BLE001 - probe should report any failure.
        return TimedResult(
            ok=False,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            detail={"session_id": session_id, "error": str(exc), "turns": turns, "messages": messages},
        )


def summarize_status(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {"raw": detail}
    body = detail.get("body")
    summary: dict[str, Any] = {"status_code": detail.get("status_code")}
    if isinstance(body, dict):
        summary.update(
            {
                "success": body.get("success"),
                "status": body.get("status"),
                "service": body.get("service"),
                "active_connections": (body.get("connections") or {}).get("active"),
                "registered_sessions": (body.get("sessions") or {}).get("registered_sessions"),
                "internal_status_available": True,
                "rtc_ready_for_offer": (body.get("rtc") or {}).get("ready_for_offer"),
                "upstreams": [
                    {
                        "service": item.get("service"),
                        "status": item.get("status"),
                        "target": item.get("target"),
                        "last_ready_at": item.get("last_ready_at"),
                    }
                    for item in body.get("upstreams", [])
                    if isinstance(item, dict)
                ],
            }
        )
    else:
        summary["body"] = body
    return summary


def summarize_ws(detail: Any) -> dict[str, Any]:
    if not isinstance(detail, dict):
        return {"raw": detail}
    summary = {
        "session_id": detail.get("session_id"),
        "trace_id": detail.get("trace_id"),
        "error": detail.get("error"),
        "message_types": [],
        "playback_start_ms": None,
        "first_binary_ms": None,
        "first_binary_payload_bytes": None,
        "first_binary_header": None,
        "done_ms": None,
    }
    message_types: list[str] = []
    playback_start_ms = None
    first_binary_ms = None
    first_binary_payload_bytes = None
    first_binary_header = None
    done_ms = None
    for item in detail.get("messages", []):
        message = item.get("message") if isinstance(item, dict) else None
        if not isinstance(message, dict):
            continue
        message_type = message.get("type", "<unknown>")
        message_types.append(message_type)
        elapsed_ms = item.get("elapsed_ms") if isinstance(item, dict) else None
        if message_type == "playback_start" and playback_start_ms is None and isinstance(elapsed_ms, int):
            playback_start_ms = elapsed_ms
        if message_type == "<binary>" and first_binary_ms is None and isinstance(elapsed_ms, int):
            first_binary_ms = elapsed_ms
        if message_type == "<binary>" and first_binary_payload_bytes is None:
            payload_bytes = message.get("payload_bytes")
            if isinstance(payload_bytes, int) and payload_bytes > 0:
                first_binary_payload_bytes = payload_bytes
        if message_type == "<binary>" and first_binary_header is None:
            header = message.get("header")
            if isinstance(header, dict):
                first_binary_header = header
        if message_type in {"done", "response.done"} and isinstance(elapsed_ms, int):
            done_ms = elapsed_ms
        payload = message.get("payload")
        if first_binary_payload_bytes is None and isinstance(payload, dict):
            payload_bytes = payload.get("payload_bytes")
            if isinstance(payload_bytes, int) and payload_bytes > 0:
                first_binary_payload_bytes = payload_bytes
    summary["message_types"] = message_types
    summary["playback_start_ms"] = playback_start_ms
    summary["first_binary_ms"] = first_binary_ms
    summary["first_binary_payload_bytes"] = first_binary_payload_bytes
    summary["first_binary_header"] = first_binary_header
    summary["done_ms"] = done_ms
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Python Gateway internal voice endpoint.")
    parser.add_argument("--base-url", default="http://127.0.0.1:7860", help="Python Gateway base URL.")
    parser.add_argument("--status-url", default="", help="Override status URL.")
    parser.add_argument("--ws-url", default="", help="Override internal voice WS URL.")
    parser.add_argument("--robot-id", default="test_01")
    parser.add_argument("--robot-secret", default="")
    parser.add_argument("--client-type", default="codex_probe")
    parser.add_argument("--bot-id", default="xiaowen")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--client-event",
        default="",
        help="Optional event to send after session.open, e.g. wake_idle. This may trigger TTS.",
    )
    parser.add_argument("--full", action="store_true", help="Print full response bodies.")
    args = parser.parse_args()

    derived_status_url, derived_ws_url = derive_urls(args.base_url)
    status_url = args.status_url or derived_status_url
    ws_url = args.ws_url or derived_ws_url

    status = get_status(status_url, args.timeout)
    internal_voice = probe_internal_voice(
        ws_url,
        robot_id=args.robot_id,
        robot_secret=args.robot_secret,
        client_type=args.client_type,
        bot_id=args.bot_id,
        timeout=args.timeout,
        client_event=args.client_event,
    )

    output = {
        "target": {
            "status_url": status_url,
            "ws_url": ws_url,
            "robot_id": args.robot_id,
            "robot_secret": redact_secret(args.robot_secret),
            "bot_id": args.bot_id,
            "client_event": args.client_event or None,
        },
        "status": {
            "ok": status.ok,
            "elapsed_ms": status.elapsed_ms,
            "summary": summarize_status(status.detail),
        },
        "internal_voice": {
            "ok": internal_voice.ok,
            "elapsed_ms": internal_voice.elapsed_ms,
            "summary": summarize_ws(internal_voice.detail),
        },
    }
    if args.full:
        output["status"]["detail"] = status.detail
        output["internal_voice"]["detail"] = internal_voice.detail

    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if status.ok and internal_voice.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
