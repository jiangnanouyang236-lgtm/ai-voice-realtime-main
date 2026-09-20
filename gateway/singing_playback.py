from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import wave

from singing.library import SingingCatalogError, resolve_audio_path


@dataclass(frozen=True)
class SingingAudio:
    voice_id: str
    song_id: str
    title: str
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    pcm: bytes

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm) / self.sample_width / self.channels / self.sample_rate

    def chunks(self, chunk_ms: int = 100):
        frames = max(1, round(self.sample_rate * chunk_ms / 1000))
        chunk_bytes = frames * self.channels * self.sample_width
        for offset in range(0, len(self.pcm), chunk_bytes):
            yield self.pcm[offset : offset + chunk_bytes]


    @property
    def asset_id(self) -> str:
        return f"{self.voice_id}:{self.song_id}"


def load_singing_audio(asset_id: str, *, root: Path | None = None) -> SingingAudio:
    try:
        voice_id, song_id = asset_id.split(":", 1)
    except ValueError as exc:
        raise SingingCatalogError("歌曲资产 id 非法") from exc
    song, path = resolve_audio_path(song_id, voice_id=voice_id, root=root)
    if not path.is_file():
        raise SingingCatalogError(f"歌曲音频不存在: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != song["sha256"]:
        raise SingingCatalogError(f"歌曲音频哈希不匹配: {song_id}")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            compression = handle.getcomptype()
            pcm = handle.readframes(handle.getnframes())
    except (OSError, wave.Error) as exc:
        raise SingingCatalogError(f"歌曲 WAV 无法读取: {path}") from exc
    if (sample_rate, channels, sample_width, compression) != (16000, 1, 2, "NONE"):
        raise SingingCatalogError(
            f"歌曲音频必须是 16kHz mono PCM16: {song_id}"
        )
    return SingingAudio(
        voice_id=voice_id,
        song_id=song_id,
        title=str(song["title"]),
        path=path,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        pcm=pcm,
    )
