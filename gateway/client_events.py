from voice_quick_replies import pick_quick_reply, quick_reply_pool


CLIENT_EVENT_TYPES = {"startup_ready", "wake_idle", "wake_interrupt", "sleep_exit"}
CLIENT_EVENT_PHRASES = {
    event_type: quick_reply_pool(f"client.{event_type}")
    for event_type in CLIENT_EVENT_TYPES
}


def pick_client_event_phrase(event_type: str, session_id: str | None = None) -> str:
    event = str(event_type or "").strip()
    phrases = CLIENT_EVENT_PHRASES.get(event)
    if not phrases:
        raise ValueError(f"未知客户端事件: {event}")
    return pick_quick_reply(f"client.{event}", session_id)


def client_event_source(data: dict) -> str:
    return data.get("source") or "client_event"


def build_client_event_received_summary(data: dict) -> dict:
    return {"event": data.get("event"), "source": client_event_source(data)}


def build_client_event_phrase_summary(event_type: str, phrase: str, data: dict) -> dict:
    return {
        "event_type": event_type,
        "phrase": phrase,
        "source": client_event_source(data),
    }
