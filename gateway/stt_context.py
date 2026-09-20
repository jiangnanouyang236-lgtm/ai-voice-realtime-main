"""STT metadata extraction and short audio-context prompt helpers."""

from __future__ import annotations

import json
from typing import Any


def parse_stt_json_field(value: str, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def stt_metadata(response: Any) -> dict[str, Any]:
    return {
        "confidence": float(getattr(response, "confidence", 0.0) or 0.0),
        "confidence_source": getattr(response, "confidence_source", "") or "unavailable",
        "language": getattr(response, "language", "") or "",
        "emotion": getattr(response, "emotion", "") or "",
        "event_type": getattr(response, "event_type", "") or "",
        "raw_text": getattr(response, "raw_text", "") or "",
        "tags": parse_stt_json_field(getattr(response, "tags_json", ""), []),
        "metadata": parse_stt_json_field(getattr(response, "metadata_json", ""), {}),
    }


STT_LANGUAGE_LABELS = {
    "zh": "中文",
    "en": "英文",
    "yue": "粤语",
    "ja": "日语",
    "ko": "韩语",
}

STT_EMOTION_LABELS = {
    "happy": "开心",
    "sad": "难过",
    "angry": "生气",
    "fearful": "害怕",
    "disgusted": "厌恶",
    "surprised": "惊讶",
    "neutral": "平静",
}

STT_EVENT_LABELS = {
    "speech": "语音",
    "laughter": "笑声",
    "applause": "掌声",
    "music": "音乐",
    "noise": "噪声",
    "cough": "咳嗽",
    "cry": "哭声",
}


def stt_label(mapping: dict[str, str], value: str) -> str:
    text = str(value or "").strip().lower()
    return mapping.get(text, text)


def build_llm_query_with_audio_context(
    text: str,
    metadata: dict[str, Any],
    *,
    include_context: bool = True,
) -> str:
    """Add short voice context for LLM input while keeping history text clean."""
    clean_text = str(text or "").strip()
    if not include_context or not clean_text:
        return clean_text

    hints: list[str] = []
    language = str(metadata.get("language") or "").strip().lower()
    emotion = str(metadata.get("emotion") or "").strip().lower()
    event_type = str(metadata.get("event_type") or "").strip().lower()

    if language and language != "zh":
        hints.append(f"语言={stt_label(STT_LANGUAGE_LABELS, language)}")
    if emotion and emotion != "neutral":
        hints.append(f"情绪={stt_label(STT_EMOTION_LABELS, emotion)}")
    if event_type and event_type != "speech":
        hints.append(f"声音事件={stt_label(STT_EVENT_LABELS, event_type)}")

    if not hints:
        return clean_text

    return f"[语音上下文: {'；'.join(hints)}]\n用户说：{clean_text}"
