from types import SimpleNamespace

from gateway.stt_context import (
    build_llm_query_with_audio_context,
    parse_stt_json_field,
    stt_metadata,
)


def test_parse_stt_json_field_returns_fallback_for_empty_or_invalid_json():
    fallback = {"ok": False}

    assert parse_stt_json_field("", fallback) is fallback
    assert parse_stt_json_field("{bad-json", fallback) is fallback


def test_stt_metadata_extracts_optional_json_fields_and_defaults():
    response = SimpleNamespace(
        confidence=0.91,
        language="en",
        emotion="happy",
        event_type="speech",
        raw_text="hello",
        tags_json='["short"]',
        metadata_json='{"speaker": "near"}',
    )

    metadata = stt_metadata(response)

    assert metadata["confidence"] == 0.91
    assert metadata["confidence_source"] == "unavailable"
    assert metadata["language"] == "en"
    assert metadata["emotion"] == "happy"
    assert metadata["event_type"] == "speech"
    assert metadata["raw_text"] == "hello"
    assert metadata["tags"] == ["short"]
    assert metadata["metadata"] == {"speaker": "near"}


def test_build_llm_query_with_audio_context_skips_default_speech_context():
    query = build_llm_query_with_audio_context(
        "  给我讲个故事  ",
        {"language": "zh", "emotion": "neutral", "event_type": "speech"},
    )

    assert query == "给我讲个故事"


def test_build_llm_query_with_audio_context_can_be_disabled():
    query = build_llm_query_with_audio_context(
        "hello",
        {"language": "en", "emotion": "happy", "event_type": "laughter"},
        include_context=False,
    )

    assert query == "hello"


def test_build_llm_query_with_audio_context_adds_readable_labels():
    query = build_llm_query_with_audio_context(
        "hello",
        {"language": "en", "emotion": "happy", "event_type": "laughter"},
    )

    assert query == "[语音上下文: 语言=英文；情绪=开心；声音事件=笑声]\n用户说：hello"
