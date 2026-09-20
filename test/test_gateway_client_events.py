import pytest

from gateway.client_events import (
    CLIENT_EVENT_PHRASES,
    CLIENT_EVENT_TYPES,
    build_client_event_phrase_summary,
    build_client_event_received_summary,
    pick_client_event_phrase,
)


def test_client_event_phrase_pool_covers_expected_events():
    assert CLIENT_EVENT_TYPES == {"startup_ready", "wake_idle", "wake_interrupt", "sleep_exit"}
    expected_minimums = {
        "startup_ready": 16,
        "wake_idle": 16,
        "wake_interrupt": 16,
        "sleep_exit": 16,
    }
    total = sum(len(items) for items in CLIENT_EVENT_PHRASES.values())
    assert total >= 64
    for event_type in CLIENT_EVENT_TYPES:
        phrases = CLIENT_EVENT_PHRASES[event_type]
        assert len(phrases) >= expected_minimums[event_type]
        assert len(phrases) == len(set(phrases))
        assert pick_client_event_phrase(event_type) in phrases


def test_client_event_phrase_does_not_repeat_within_same_session_pool():
    pool_size = len(CLIENT_EVENT_PHRASES["wake_idle"])
    selected = [
        pick_client_event_phrase("wake_idle", "no-repeat-session")
        for _ in range(pool_size)
    ]

    assert len(selected) == len(set(selected)) == pool_size


def test_pick_client_event_phrase_strips_event_type():
    assert pick_client_event_phrase(" wake_idle ") in CLIENT_EVENT_PHRASES["wake_idle"]


def test_pick_client_event_phrase_rejects_unknown_event():
    with pytest.raises(ValueError, match="未知客户端事件"):
        pick_client_event_phrase("unknown")


def test_build_client_event_received_summary_defaults_source():
    assert build_client_event_received_summary({"event": "wake_idle"}) == {
        "event": "wake_idle",
        "source": "client_event",
    }


def test_build_client_event_phrase_summary_keeps_source():
    assert build_client_event_phrase_summary(
        "wake_interrupt",
        "好的，你说。",
        {"source": "hardware_wake"},
    ) == {
        "event_type": "wake_interrupt",
        "phrase": "好的，你说。",
        "source": "hardware_wake",
    }
