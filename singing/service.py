from __future__ import annotations

import random
from typing import Any

from singing.library import (
    SingingCatalogError,
    asset_id,
    available_songs,
    default_voice_id,
    load_catalog,
    load_voice_registry,
    song_variant,
    validate_catalog_voice_ids,
)
from singing.matcher import match_song, normalize_text


NOT_LEARNED_TEXT = "这首歌我还没学会，换一首好吗？"
NO_SONGS_TEXT = "我现在还没有学会唱歌，等我准备一下吧。"


class SingingService:
    def __init__(
        self,
        *,
        catalog: dict[str, Any] | None = None,
        voice_registry: dict[str, Any] | None = None,
        chooser: random.Random | random.SystemRandom | None = None,
    ) -> None:
        self.catalog = catalog or load_catalog()
        self.voice_registry = voice_registry or load_voice_registry()
        validate_catalog_voice_ids(self.catalog, self.voice_registry)
        self.chooser = chooser or random.SystemRandom()
        self.known_voice_ids = {
            str(voice["id"])
            for voice in self.voice_registry["voices"]
        }

    def resolve_voice_id(self, meta: dict[str, Any] | None = None) -> str:
        requested = str((meta or {}).get("singing_voice_id") or "").strip()
        tts_profile_id = str((meta or {}).get("tts_profile_id") or "").strip()
        voice_id = requested
        if not voice_id and tts_profile_id:
            voice_id = next(
                (
                    str(voice["id"])
                    for voice in self.voice_registry["voices"]
                    if tts_profile_id in {
                        str(value)
                        for value in voice.get("tts_profile_ids") or []
                    }
                ),
                "",
            )
        if not voice_id and not meta:
            voice_id = default_voice_id(self.voice_registry)
        if not voice_id:
            return ""
        if voice_id not in self.known_voice_ids:
            raise SingingCatalogError(f"未知唱歌音色: {voice_id}")
        return voice_id

    @staticmethod
    def unmapped_voice_result(*, list_request: bool) -> dict[str, Any]:
        if list_request:
            return {
                "kind": "singing_catalog",
                "voice_id": "",
                "query": "",
                "total_available": 0,
                "songs": [],
                "has_more": False,
                "next_cursor": "",
                "message": NO_SONGS_TEXT,
            }
        return {
            "kind": "singing_unavailable",
            "reason": "voice_not_mapped",
            "voice_id": "",
            "message": NO_SONGS_TEXT,
        }

    def play_song(self, query: str, *, voice_id: str) -> dict[str, Any]:
        available = available_songs(voice_id, catalog=self.catalog)
        if not available:
            return {
                "kind": "singing_unavailable",
                "reason": "voice_has_no_songs",
                "voice_id": voice_id,
                "message": NO_SONGS_TEXT,
            }

        match = match_song(query, self.catalog["songs"])
        if match.status == "generic":
            song = self.chooser.choice(available)
            match_score = None
        elif match.status == "ambiguous":
            return {
                "kind": "singing_clarification",
                "reason": "ambiguous_song",
                "query": match.query,
                "candidates": [
                    {
                        "song_id": candidate.song["id"],
                        "title": candidate.song["title"],
                        "artist": str(candidate.song.get("artist") or ""),
                    }
                    for candidate in match.candidates
                ],
            }
        elif match.status != "matched" or match.best is None:
            return {
                "kind": "singing_unavailable",
                "reason": "song_not_found",
                "query": match.query,
                "voice_id": voice_id,
                "message": NOT_LEARNED_TEXT,
            }
        else:
            song = match.best.song
            match_score = round(match.best.score, 4)

        variant = song_variant(song, voice_id)
        if variant is None:
            return {
                "kind": "singing_unavailable",
                "reason": "voice_variant_missing",
                "song_id": song["id"],
                "title": song["title"],
                "voice_id": voice_id,
                "message": NOT_LEARNED_TEXT,
            }
        result = {
            "kind": "singing_playback",
            "song_id": song["id"],
            "title": song["title"],
            "artist": str(song.get("artist") or ""),
            "voice_id": voice_id,
            "asset_id": asset_id(str(song["id"]), voice_id),
            "duration_seconds": float(variant["duration_seconds"]),
        }
        if match_score is not None:
            result["match"] = {"query": match.query, "confidence": match_score}
        return result

    def list_songs(
        self,
        query: str,
        *,
        voice_id: str,
        limit: int = 5,
        cursor: str = "",
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 10))
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise SingingCatalogError("歌曲列表 cursor 非法") from exc
        if offset < 0:
            raise SingingCatalogError("歌曲列表 cursor 非法")

        available = available_songs(voice_id, catalog=self.catalog)
        normalized_query = normalize_text(query)
        if normalized_query:
            direct = [
                song
                for song in available
                if normalized_query in normalize_text(str(song.get("title") or ""))
                or normalized_query in normalize_text(str(song.get("artist") or ""))
                or any(
                    normalized_query in normalize_text(str(alias))
                    for alias in song.get("aliases") or []
                )
            ]
            if direct:
                available = direct
            else:
                match = match_song(query, available)
                candidates = match.candidates if match.status == "ambiguous" else (
                    (match.best,) if match.status == "matched" and match.best else ()
                )
                available = [candidate.song for candidate in candidates]

        page = available[offset:offset + safe_limit]
        next_offset = offset + len(page)
        return {
            "kind": "singing_catalog",
            "voice_id": voice_id,
            "query": query,
            "total_available": len(available),
            "songs": [
                {
                    "song_id": song["id"],
                    "title": song["title"],
                    "artist": str(song.get("artist") or ""),
                }
                for song in page
            ],
            "has_more": next_offset < len(available),
            "next_cursor": str(next_offset) if next_offset < len(available) else "",
            "message": "" if available else NOT_LEARNED_TEXT,
        }
