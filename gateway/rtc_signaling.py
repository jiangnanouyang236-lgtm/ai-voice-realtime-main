"""RTC signaling compatibility helpers for the Python WebSocket gateway."""

from __future__ import annotations

from typing import Any


RTC_SESSION_MISMATCH_CODE = "RTC_SESSION_MISMATCH"
_RTC_SESSION_MISMATCH_MESSAGES = {
    "rtc_offer": "rtc_offer session_id 与当前会话不匹配",
    "rtc_ice_candidate": "rtc_ice_candidate session_id 与当前会话不匹配",
    "transport_ready": "transport_ready session_id 与当前会话不匹配",
    "transport_fallback_start": "transport_fallback_start session_id 与当前会话不匹配",
}


def rtc_message_session_id(data: dict[str, Any]) -> str:
    return str(data.get("session_id") or "").strip()


def rtc_session_matches(data: dict[str, Any], session_id: str) -> bool:
    message_session_id = rtc_message_session_id(data)
    return not message_session_id or message_session_id == session_id


def rtc_session_mismatch_error(message_type: str) -> tuple[str, str]:
    return (
        RTC_SESSION_MISMATCH_CODE,
        _RTC_SESSION_MISMATCH_MESSAGES.get(
            message_type,
            f"{message_type} session_id 与当前会话不匹配",
        ),
    )


def rtc_offer_fallback_reason(*, signaling_enabled: bool) -> str:
    return "rtc_media_not_available" if signaling_enabled else "rtc_signaling_disabled"


def build_transport_fallback_ack(
    session_id: str,
    *,
    active_transport: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "active_transport": active_transport,
        "reason": reason,
    }


def build_rtc_offer_fallback_ack(
    session_id: str,
    *,
    signaling_enabled: bool,
) -> dict[str, Any]:
    return build_transport_fallback_ack(
        session_id,
        active_transport="websocket",
        reason=rtc_offer_fallback_reason(signaling_enabled=signaling_enabled),
    )


def build_transport_ready_fallback_ack(session_id: str) -> dict[str, Any]:
    return build_transport_fallback_ack(
        session_id,
        active_transport="websocket",
        reason="rtc_media_not_available",
    )


def build_transport_fallback_start_ack(
    session_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    active_transport = str(data.get("to_transport") or "websocket").strip() or "websocket"
    reason = str(data.get("reason") or "client_requested_fallback")
    return build_transport_fallback_ack(
        session_id,
        active_transport=active_transport,
        reason=reason,
    )
