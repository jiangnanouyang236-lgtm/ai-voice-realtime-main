"""Gateway upstream configuration helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


UPSTREAM_SERVICES = ("stt", "llm", "tts")


def parse_grpc_upstream_url(raw_url: str) -> dict[str, Any]:
    text = str(raw_url or "").strip()
    if not text:
        raise ValueError("Gateway upstream 地址不能为空")
    if "://" in text:
        scheme, target = text.split("://", 1)
        scheme = scheme.lower().strip()
    else:
        scheme = "grpc"
        target = text
    target = target.strip().rstrip("/")
    if scheme not in {"grpc", "grpcs"}:
        raise ValueError(f"Gateway upstream 只支持 grpc:// 或 grpcs://: {raw_url}")
    if not target:
        raise ValueError(f"Gateway upstream 缺少 host:port: {raw_url}")
    return {
        "url": f"{scheme}://{target}",
        "target": target,
        "secure": scheme == "grpcs",
    }


def build_upstream_specs(settings: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "stt": parse_grpc_upstream_url(str(settings["stt_service_url"])),
        "llm": parse_grpc_upstream_url(str(settings["llm_service_url"])),
        "tts": parse_grpc_upstream_url(str(settings["tts_service_url"])),
    }


def build_initial_upstream_statuses(
    specs: Mapping[str, Mapping[str, Any]],
    *,
    message: str = "等待预热",
) -> dict[str, dict[str, Any]]:
    return {
        service: {
            "service": service,
            "target": str(specs[service]["url"]),
            "secure": bool(specs[service]["secure"]),
            "status": "configured",
            "message": message,
            "last_ready_at": None,
            "last_error": None,
            "last_error_at": None,
        }
        for service in UPSTREAM_SERVICES
    }
