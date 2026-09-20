"""WebRTC configuration parsing and status helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


def normalize_rtc_ice_server(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        url = value.strip()
        return {"urls": [url]} if url else None
    if not isinstance(value, dict):
        return None

    urls = value.get("urls")
    if isinstance(urls, str):
        urls = [urls.strip()] if urls.strip() else []
    elif isinstance(urls, list):
        urls = [str(url).strip() for url in urls if str(url).strip()]
    else:
        urls = []
    if not urls:
        return None

    server: dict[str, Any] = {"urls": urls}
    username = str(value.get("username") or "").strip()
    credential = str(value.get("credential") or "").strip()
    if username:
        server["username"] = username
    if credential:
        server["credential"] = credential
    return server


def parse_rtc_ice_servers(raw_value: str | None = None) -> list[dict[str, Any]]:
    raw = os.getenv("GATEWAY_RTC_ICE_SERVERS", "") if raw_value is None else raw_value
    raw = str(raw or "").strip()
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [part.strip() for part in raw.split(",")]

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    servers: list[dict[str, Any]] = []
    for item in parsed:
        server = normalize_rtc_ice_server(item)
        if server:
            servers.append(server)
    return servers


def _is_rtc_blank(value: Any) -> bool:
    return str(value or "").strip() == ""


def summarize_rtc_ice_servers(servers: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "ice_server_count": len(servers),
        "stun_url_count": 0,
        "turn_url_count": 0,
        "empty_url_count": 0,
        "unknown_url_count": 0,
        "turn_servers_missing_credentials": 0,
        "redacted_servers": [],
    }

    for server in servers:
        urls = server.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        elif isinstance(urls, list):
            urls = [str(url).strip() for url in urls]
        else:
            urls = []

        has_turn = False
        for url in urls:
            normalized = str(url or "").strip().lower()
            if not normalized:
                summary["empty_url_count"] += 1
            elif normalized.startswith(("stun:", "stuns:")):
                summary["stun_url_count"] += 1
            elif normalized.startswith(("turn:", "turns:")):
                summary["turn_url_count"] += 1
                has_turn = True
            else:
                summary["unknown_url_count"] += 1

        username_present = not _is_rtc_blank(server.get("username"))
        credential_present = not _is_rtc_blank(server.get("credential"))
        if has_turn and (not username_present or not credential_present):
            summary["turn_servers_missing_credentials"] += 1

        redacted = {"urls": [url for url in urls if url]}
        if username_present:
            redacted["username"] = str(server.get("username")).strip()
        if credential_present:
            redacted["credential"] = "<redacted>"
        summary["redacted_servers"].append(redacted)

    return summary


def build_rtc_status_payload(
    *,
    signaling_enabled: bool,
    ice_servers: list[dict[str, Any]],
    audio_direction: str,
    audio_codec: str,
    audio_sample_rate: int,
    audio_channels: int,
    audio_ptime_ms: int,
) -> dict[str, Any]:
    ice_summary = summarize_rtc_ice_servers(list(ice_servers))
    warnings: list[str] = []

    if not signaling_enabled:
        warnings.append("rtc_signaling_disabled")
    if ice_summary["ice_server_count"] == 0:
        warnings.append("ice_servers_empty")
    if ice_summary["stun_url_count"] == 0 and ice_summary["turn_url_count"] == 0:
        warnings.append("no_stun_or_turn_url")
    if ice_summary["empty_url_count"] > 0:
        warnings.append("empty_ice_url")
    if ice_summary["unknown_url_count"] > 0:
        warnings.append("unknown_ice_url_scheme")
    if ice_summary["turn_servers_missing_credentials"] > 0:
        warnings.append("turn_missing_username_or_credential")

    codec = str(audio_codec or "").strip()
    if codec.lower() != "opus":
        warnings.append("audio_codec_not_opus")
    if audio_sample_rate != 48000:
        warnings.append("audio_sample_rate_not_webrtc_opus_clock")
    if audio_channels < 1 or audio_channels > 2:
        warnings.append("audio_channels_unsupported")
    if audio_ptime_ms not in {10, 20, 40, 60}:
        warnings.append("audio_ptime_unusual")

    return {
        "signaling_enabled": bool(signaling_enabled),
        "ice": ice_summary,
        "media": {
            "audio": {
                "direction": audio_direction,
                "codec": audio_codec,
                "sample_rate": audio_sample_rate,
                "channels": audio_channels,
                "ptime_ms": audio_ptime_ms,
            }
        },
        "warnings": warnings,
        "ready_for_offer": bool(signaling_enabled) and not warnings,
    }


@dataclass(frozen=True)
class RtcGatewaySettings:
    signaling_enabled: bool
    ice_servers: list[dict[str, Any]]
    audio_direction: str
    audio_codec: str
    audio_sample_rate: int
    audio_channels: int
    audio_ptime_ms: int

    def status_payload(self) -> dict[str, Any]:
        return build_rtc_status_payload(
            signaling_enabled=self.signaling_enabled,
            ice_servers=self.ice_servers,
            audio_direction=self.audio_direction,
            audio_codec=self.audio_codec,
            audio_sample_rate=self.audio_sample_rate,
            audio_channels=self.audio_channels,
            audio_ptime_ms=self.audio_ptime_ms,
        )

    def config_payload(self, session_id: str) -> dict[str, Any]:
        return build_rtc_config_payload(
            session_id=session_id,
            ice_servers=self.ice_servers,
            audio_direction=self.audio_direction,
            audio_codec=self.audio_codec,
            audio_sample_rate=self.audio_sample_rate,
            audio_channels=self.audio_channels,
            audio_ptime_ms=self.audio_ptime_ms,
        )


def build_rtc_config_payload(
    *,
    session_id: str,
    ice_servers: list[dict[str, Any]],
    audio_direction: str,
    audio_codec: str,
    audio_sample_rate: int,
    audio_channels: int,
    audio_ptime_ms: int,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "ice_servers": list(ice_servers),
        "media": {
            "audio": {
                "direction": audio_direction,
                "codec": audio_codec,
                "sample_rate": audio_sample_rate,
                "channels": audio_channels,
                "ptime_ms": audio_ptime_ms,
            }
        },
    }
