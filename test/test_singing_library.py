import hashlib
import json
from pathlib import Path
import wave

import pytest

from gateway.singing_playback import load_singing_audio
from singing.library import SingingCatalogError, catalog_path, load_catalog, resolve_audio_path


def _catalog(asset: str = "serena/001.wav", sha256: str = "0" * 64) -> dict:
    return {
        "schema_version": "singing-catalog/v2",
        "songs": [
            {
                "id": "001",
                "slug": "aini",
                "title": "爱你",
                "artist": "王心凌",
                "aliases": ["爱泥"],
                "variants": {
                    "serena-v1": {
                        "asset": asset,
                        "duration_seconds": 0.1,
                        "sha256": sha256,
                    }
                },
            }
        ],
    }


def _voices() -> dict:
    return {
        "schema_version": "singing-voices/v1",
        "default_voice_id": "serena-v1",
        "voices": [
            {"id": "serena-v1", "display_name": "Serena", "tts_voice_aliases": ["serena"]}
        ],
    }


def _write_runtime_files(tmp_path: Path, digest: str) -> tuple[Path, Path, Path]:
    audio_root = tmp_path / "audio"
    catalog_path = tmp_path / "catalog.json"
    voices_path = tmp_path / "voices.json"
    catalog_path.write_text(json.dumps(_catalog(sha256=digest)), encoding="utf-8")
    voices_path.write_text(json.dumps(_voices()), encoding="utf-8")
    return audio_root, catalog_path, voices_path


def test_load_singing_audio_enforces_16k_pcm_and_hash(tmp_path: Path, monkeypatch):
    wav_path = tmp_path / "audio" / "serena" / "001.wav"
    wav_path.parent.mkdir(parents=True)
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    digest = hashlib.sha256(wav_path.read_bytes()).hexdigest()
    audio_root, catalog_path, voices_path = _write_runtime_files(tmp_path, digest)
    monkeypatch.setenv("SINGING_CATALOG_PATH", str(catalog_path))
    monkeypatch.setenv("SINGING_VOICES_PATH", str(voices_path))
    monkeypatch.setenv("SINGING_AUDIO_ROOT", str(audio_root))

    audio = load_singing_audio("serena-v1:001")

    assert audio.asset_id == "serena-v1:001"
    assert audio.sample_rate == 16000
    assert audio.channels == 1
    assert audio.sample_width == 2
    assert audio.duration_seconds == pytest.approx(0.1)
    assert len(list(audio.chunks(chunk_ms=40))) == 3


def test_load_singing_audio_rejects_hash_mismatch(tmp_path: Path, monkeypatch):
    wav_path = tmp_path / "audio" / "serena" / "001.wav"
    wav_path.parent.mkdir(parents=True)
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 160)
    audio_root, catalog_path, voices_path = _write_runtime_files(tmp_path, "0" * 64)
    monkeypatch.setenv("SINGING_CATALOG_PATH", str(catalog_path))
    monkeypatch.setenv("SINGING_VOICES_PATH", str(voices_path))
    monkeypatch.setenv("SINGING_AUDIO_ROOT", str(audio_root))

    with pytest.raises(SingingCatalogError, match="哈希不匹配"):
        load_singing_audio("serena-v1:001")


def test_resolve_audio_path_never_escapes_root(tmp_path: Path):
    catalog = _catalog(asset="../outside.wav")

    with pytest.raises(SingingCatalogError, match="越过"):
        resolve_audio_path("001", root=tmp_path, catalog=catalog)


def test_versioned_score_manifests_reference_existing_reusable_segments():
    catalog = load_catalog()
    singing_root = catalog_path().parent
    scored_song_ids = []

    for song in catalog["songs"]:
        manifest_name = song.get("score_manifest")
        if not manifest_name:
            continue
        manifest_path = singing_root / manifest_name
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["song_id"] == song["id"]
        assert manifest["method"]["control"] == "continuous_f0"
        assert manifest["segments"]
        for segment in manifest["segments"]:
            assert (manifest_path.parent / segment["score"]).is_file()
        scored_song_ids.append(song["id"])

    assert scored_song_ids == [f"{value:03d}" for value in range(6, 24)]
