from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence


_PUNCTUATION_RE = re.compile(r"[\s。！？!?.,，；;：:'\"“”‘’《》〈〉~～·_\-]+")
_GENERIC_REQUESTS = {
    "唱歌", "唱首歌", "唱一首歌", "唱个歌", "来首歌", "来一首歌", "来个歌",
    "随便唱首歌", "随便唱一首歌", "随便来首歌", "随便来一首歌",
}
_GENERIC_EXTRACTED_QUERIES = {"", "歌", "歌曲"}
_REQUEST_PREFIXES = tuple(sorted((
    "麻烦你给我唱一首", "麻烦你给我唱首", "麻烦你唱一首", "麻烦你唱首",
    "请你给我唱一首", "请你给我唱首", "给我唱一首", "给我唱首", "给我唱一下",
    "你给我唱一首", "你给我唱首", "请你唱一首", "请你唱首", "请唱一首", "请唱首",
    "唱一首", "唱首", "唱一下", "唱个", "来一首", "来首", "来个", "播放", "放一下",
), key=len, reverse=True))
_REQUEST_SUFFIXES = tuple(sorted((
    "给我听听", "唱给我听", "我听听", "给我听", "可以吗", "好不好", "好吗", "吧", "呀", "啊",
), key=len, reverse=True))


def normalize_text(text: str) -> str:
    return _PUNCTUATION_RE.sub("", (text or "").casefold())


def extract_song_query(text: str) -> str:
    value = normalize_text(text)
    changed = True
    while changed and value:
        changed = False
        for prefix in _REQUEST_PREFIXES:
            normalized = normalize_text(prefix)
            if value.startswith(normalized):
                value = value[len(normalized):]
                changed = True
                break
    changed = True
    while changed and value:
        changed = False
        for suffix in _REQUEST_SUFFIXES:
            normalized = normalize_text(suffix)
            if value.endswith(normalized):
                value = value[:-len(normalized)]
                changed = True
                break
    return value


def is_generic_song_request(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized or normalized in {normalize_text(item) for item in _GENERIC_REQUESTS}:
        return True
    return extract_song_query(text) in _GENERIC_EXTRACTED_QUERIES


def _edit_distance(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        for column, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def similarity(left: str, right: str) -> float:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    longest = max(len(normalized_left), len(normalized_right))
    if not longest:
        return 1.0
    return 1.0 - _edit_distance(normalized_left, normalized_right) / longest


def _song_terms(song: dict[str, Any]) -> list[str]:
    terms = [song.get("title", ""), song.get("slug", ""), *(song.get("aliases") or [])]
    artist = str(song.get("artist") or "")
    title = str(song.get("title") or "")
    if artist and title:
        terms.extend((f"{artist}{title}", f"{artist}的{title}"))
    return [normalize_text(str(term)) for term in terms if normalize_text(str(term))]


def _minimum_score(query: str) -> float:
    if len(query) <= 2:
        return 0.86
    if len(query) <= 4:
        return 0.72
    return 0.68


@dataclass(frozen=True)
class MatchCandidate:
    song: dict[str, Any]
    score: float


@dataclass(frozen=True)
class SongMatch:
    status: str
    query: str
    best: MatchCandidate | None = None
    candidates: tuple[MatchCandidate, ...] = ()


def match_song(
    query: str,
    songs: Sequence[dict[str, Any]],
    *,
    ambiguity_gap: float = 0.08,
) -> SongMatch:
    extracted = extract_song_query(query)
    normalized_original = normalize_text(query)
    if is_generic_song_request(query) or not extracted:
        return SongMatch(status="generic", query=extracted)

    ranked: list[MatchCandidate] = []
    for song in songs:
        best_score = 0.0
        for term in _song_terms(song):
            if term and term in normalized_original:
                best_score = max(best_score, 1.0 if term == extracted else 0.98)
            if extracted == term:
                best_score = 1.0
            elif extracted in term or term in extracted:
                shorter = min(len(extracted), len(term))
                longer = max(len(extracted), len(term))
                best_score = max(best_score, 0.84 + 0.12 * shorter / longer)
            best_score = max(best_score, similarity(extracted, term))
        ranked.append(MatchCandidate(song=song, score=best_score))

    ranked.sort(key=lambda item: (-item.score, str(item.song.get("id") or "")))
    threshold = _minimum_score(extracted)
    accepted = [candidate for candidate in ranked if candidate.score >= threshold]
    if not accepted:
        return SongMatch(status="not_found", query=extracted)
    if len(accepted) > 1 and accepted[0].score - accepted[1].score < ambiguity_gap:
        return SongMatch(
            status="ambiguous",
            query=extracted,
            best=accepted[0],
            candidates=tuple(accepted[:3]),
        )
    return SongMatch(status="matched", query=extracted, best=accepted[0])
