from gateway.rtc_signaling import (
    RTC_SESSION_MISMATCH_CODE,
    build_rtc_offer_fallback_ack,
    build_transport_fallback_start_ack,
    build_transport_ready_fallback_ack,
    rtc_offer_fallback_reason,
    rtc_session_matches,
    rtc_session_mismatch_error,
)


def test_rtc_session_matches_allows_empty_or_matching_session_id():
    assert rtc_session_matches({}, "session-1") is True
    assert rtc_session_matches({"session_id": " session-1 "}, "session-1") is True
    assert rtc_session_matches({"session_id": "other"}, "session-1") is False


def test_rtc_session_mismatch_error_uses_stable_code_and_message():
    assert rtc_session_mismatch_error("rtc_offer") == (
        RTC_SESSION_MISMATCH_CODE,
        "rtc_offer session_id 与当前会话不匹配",
    )
    assert rtc_session_mismatch_error("custom") == (
        RTC_SESSION_MISMATCH_CODE,
        "custom session_id 与当前会话不匹配",
    )


def test_rtc_offer_fallback_ack_reflects_signaling_flag():
    assert rtc_offer_fallback_reason(signaling_enabled=True) == "rtc_media_not_available"
    assert rtc_offer_fallback_reason(signaling_enabled=False) == "rtc_signaling_disabled"
    assert build_rtc_offer_fallback_ack("session-1", signaling_enabled=False) == {
        "session_id": "session-1",
        "active_transport": "websocket",
        "reason": "rtc_signaling_disabled",
    }


def test_transport_ready_fallback_ack_uses_websocket_media_fallback():
    assert build_transport_ready_fallback_ack("session-1") == {
        "session_id": "session-1",
        "active_transport": "websocket",
        "reason": "rtc_media_not_available",
    }


def test_transport_fallback_start_ack_honors_client_target_and_defaults():
    assert build_transport_fallback_start_ack(
        "session-1",
        {"to_transport": " websocket ", "reason": "manual"},
    ) == {
        "session_id": "session-1",
        "active_transport": "websocket",
        "reason": "manual",
    }
    assert build_transport_fallback_start_ack("session-1", {}) == {
        "session_id": "session-1",
        "active_transport": "websocket",
        "reason": "client_requested_fallback",
    }
