"""Deterministic ASR-only decisions for natural barge-in candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass


DECISION_IGNORE = "ignore"
DECISION_INTERRUPT = "interrupt"
DECISION_NEW_INTENT = "new_intent"

_TRIM_RE = re.compile(r"[\s，。！？、,.!?~～…]+")
_IGNORE_PHRASES = {
    "嗯",
    "嗯嗯",
    "对",
    "对的",
    "好的",
    "好",
    "行",
    "可以",
    "继续",
    "你继续",
    "没事",
    "知道了",
    "明白了",
    "谢谢",
    "谢谢你",
}
_INTERRUPT_RE = re.compile(
    r"^(?:停|停下|停止|停一下|别说|别说了|不要说|不要说了|不用说|不用说了|"
    r"别讲|别讲了|不要讲|不要讲了|闭嘴|安静)(?:吧|啊|呀|啦|了)*$"
)


@dataclass(frozen=True)
class BargeInDecision:
    decision: str
    text: str
    reason: str


def normalize_barge_in_text(text: str | None) -> str:
    return _TRIM_RE.sub("", str(text or "").strip().lower())


def decide_barge_in(text: str | None, *, asr_ok: bool = True) -> BargeInDecision:
    normalized = normalize_barge_in_text(text)
    if not asr_ok:
        return BargeInDecision(DECISION_IGNORE, normalized, "asr_unavailable")
    if not normalized:
        return BargeInDecision(DECISION_IGNORE, "", "asr_empty")
    if normalized in _IGNORE_PHRASES:
        return BargeInDecision(DECISION_IGNORE, normalized, "backchannel")
    if _INTERRUPT_RE.fullmatch(normalized):
        return BargeInDecision(DECISION_INTERRUPT, normalized, "explicit_stop")
    return BargeInDecision(DECISION_NEW_INTENT, normalized, "meaningful_speech")
