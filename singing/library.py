from __future__ import annotations

import json
import os
from pathlib import Path
import random
import re
from typing import Any, Sequence

from singing.matcher import match_song


CATALOG_SCHEMA_VERSION = "singing-catalog/v2"
VOICE_SCHEMA_VERSION = "singing-voices/v1"
DEFAULT_CATALOG_PATH = Path(__file__).with_name("catalog.json")
DEFAULT_VOICES_PATH = Path(__file__).with_name("voices.json")
SONG_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
VOICE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class SingingCatalogError(ValueError):
    pass


def catalog_path() -> Path:
    configured = os.getenv("SINGING_CATALOG_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CATALOG_PATH


def voices_path() -> Path:
    configured = os.getenv("SINGING_VOICES_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_VOICES_PATH


def audio_root() -> Path | None:
    configured = os.getenv("SINGING_AUDIO_ROOT", "").strip()
    return Path(configured).expanduser() if configured else None


def _read_json(source: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SingingCatalogError(f"无法读取{label}: {source}") from exc
    if not isinstance(data, dict):
        raise SingingCatalogError(f"{label}必须是 JSON 对象")
    return data


def load_voice_registry(path: Path | None = None) -> dict[str, Any]:
    data = _read_json(path or voices_path(), "唱歌音色目录")
    if data.get("schema_version") != VOICE_SCHEMA_VERSION:
        raise SingingCatalogError("唱歌音色目录 schema_version 不受支持")
    voices = data.get("voices")
    if not isinstance(voices, list) or not voices:
        raise SingingCatalogError("唱歌音色目录不能为空")
    seen: set[str] = set()
    for voice in voices:
        voice_id = str(voice.get("id") or "")
        if not VOICE_ID_RE.fullmatch(voice_id) or voice_id in seen:
            raise SingingCatalogError(f"唱歌音色 id 非法或重复: {voice_id!r}")
        if not str(voice.get("display_name") or "").strip():
            raise SingingCatalogError(f"唱歌音色 {voice_id} 缺少 display_name")
        seen.add(voice_id)
    default_voice = str(data.get("default_voice_id") or "")
    if default_voice not in seen:
        raise SingingCatalogError("default_voice_id 不在唱歌音色目录中")
    return data


def default_voice_id(registry: dict[str, Any] | None = None) -> str:
    configured = os.getenv("SINGING_DEFAULT_VOICE_ID", "").strip()
    if configured:
        if not VOICE_ID_RE.fullmatch(configured):
            raise SingingCatalogError("SINGING_DEFAULT_VOICE_ID 非法")
        return configured
    return str((registry or load_voice_registry())["default_voice_id"])


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    data = _read_json(path or catalog_path(), "歌曲目录")
    if data.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise SingingCatalogError("歌曲目录 schema_version 不受支持")
    songs = data.get("songs")
    if not isinstance(songs, list) or not songs:
        raise SingingCatalogError("歌曲目录不能为空")
    seen_ids: set[str] = set()
    seen_assets: set[str] = set()
    for song in songs:
        song_id = str(song.get("id") or "")
        if not SONG_ID_RE.fullmatch(song_id) or song_id in seen_ids:
            raise SingingCatalogError(f"歌曲 id 非法或重复: {song_id!r}")
        if not str(song.get("title") or "").strip():
            raise SingingCatalogError(f"歌曲 {song_id} 缺少 title")
        variants = song.get("variants")
        if not isinstance(variants, dict) or not variants:
            raise SingingCatalogError(f"歌曲 {song_id} 缺少音色版本")
        for voice_id, variant in variants.items():
            if not VOICE_ID_RE.fullmatch(str(voice_id)) or not isinstance(variant, dict):
                raise SingingCatalogError(f"歌曲 {song_id} 的音色版本非法: {voice_id!r}")
            asset = str(variant.get("asset") or "")
            asset_path = Path(asset)
            if not asset or asset_path.is_absolute() or ".." in asset_path.parts or asset in seen_assets:
                raise SingingCatalogError(f"歌曲 asset 非法或重复: {asset!r}")
            if not SHA256_RE.fullmatch(str(variant.get("sha256") or "")):
                raise SingingCatalogError(f"歌曲 {song_id}/{voice_id} 的 sha256 非法")
            try:
                duration = float(variant.get("duration_seconds"))
            except (TypeError, ValueError) as exc:
                raise SingingCatalogError(f"歌曲 {song_id}/{voice_id} 的时长非法") from exc
            if duration <= 0:
                raise SingingCatalogError(f"歌曲 {song_id}/{voice_id} 的时长非法")
            seen_assets.add(asset)
        seen_ids.add(song_id)
    return data


def validate_catalog_voice_ids(
    catalog: dict[str, Any],
    registry: dict[str, Any],
) -> None:
    known = {str(voice["id"]) for voice in registry["voices"]}
    used = {
        str(voice_id)
        for song in catalog["songs"]
        for voice_id in song["variants"]
    }
    unknown = sorted(used - known)
    if unknown:
        raise SingingCatalogError(f"歌曲目录引用未知唱歌音色: {','.join(unknown)}")


def find_song(song_id: str, *, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    if not SONG_ID_RE.fullmatch(song_id or ""):
        raise SingingCatalogError("歌曲 id 非法")
    for song in (catalog or load_catalog())["songs"]:
        if song["id"] == song_id:
            return song
    raise SingingCatalogError(f"歌曲不存在: {song_id}")


def available_songs(
    voice_id: str,
    *,
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not VOICE_ID_RE.fullmatch(voice_id or ""):
        raise SingingCatalogError("唱歌音色 id 非法")
    return [
        song
        for song in (catalog or load_catalog())["songs"]
        if voice_id in song["variants"]
    ]


def song_variant(song: dict[str, Any], voice_id: str) -> dict[str, Any] | None:
    variant = (song.get("variants") or {}).get(voice_id)
    return variant if isinstance(variant, dict) else None


def asset_id(song_id: str, voice_id: str) -> str:
    if not SONG_ID_RE.fullmatch(song_id or "") or not VOICE_ID_RE.fullmatch(voice_id or ""):
        raise SingingCatalogError("歌曲资产 id 非法")
    return f"{voice_id}:{song_id}"


def select_song(
    query: str,
    *,
    catalog: dict[str, Any] | None = None,
    chooser: random.Random | random.SystemRandom | None = None,
    voice_id: str | None = None,
) -> dict[str, Any]:
    data = catalog or load_catalog()
    selected_voice = voice_id or default_voice_id()
    songs: Sequence[dict[str, Any]] = available_songs(selected_voice, catalog=data)
    if not songs:
        raise SingingCatalogError("当前音色没有可播放歌曲")
    match = match_song(query, data["songs"])
    if match.status == "generic":
        return (chooser or random.SystemRandom()).choice(list(songs))
    if match.status == "ambiguous":
        raise SingingCatalogError("歌名匹配存在多个候选")
    if match.status != "matched" or match.best is None:
        raise SingingCatalogError("这首歌还没学会")
    return match.best.song


def playback_tool_result(song: dict[str, Any], voice_id: str | None = None) -> str:
    selected_voice = voice_id or default_voice_id()
    variant = song_variant(song, selected_voice)
    if variant is None:
        raise SingingCatalogError("当前音色还没学会这首歌")
    return json.dumps(
        {
            "kind": "singing_playback",
            "song_id": song["id"],
            "title": song["title"],
            "artist": str(song.get("artist") or ""),
            "voice_id": selected_voice,
            "asset_id": asset_id(str(song["id"]), selected_voice),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def resolve_audio_path(
    song_id: str,
    *,
    voice_id: str | None = None,
    root: Path | None = None,
    catalog: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    data = catalog or load_catalog()
    selected_voice = voice_id or default_voice_id()
    song = find_song(song_id, catalog=data)
    variant = song_variant(song, selected_voice)
    if variant is None:
        raise SingingCatalogError("当前音色还没学会这首歌")
    base = root or audio_root()
    if base is None:
        raise SingingCatalogError("SINGING_AUDIO_ROOT 未配置")
    base = base.resolve()
    path = (base / str(variant["asset"])).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise SingingCatalogError("歌曲资产越过 SINGING_AUDIO_ROOT") from exc
    resolved = {
        **song,
        **variant,
        "voice_id": selected_voice,
        "asset_id": asset_id(song_id, selected_voice),
    }
    return resolved, path
