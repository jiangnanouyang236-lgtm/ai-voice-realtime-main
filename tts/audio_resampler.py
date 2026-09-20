from __future__ import annotations

import struct


def resample_pcm16le(audio_data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample mono little-endian PCM16 bytes with linear interpolation."""
    if not audio_data or from_rate == to_rate:
        return audio_data
    if from_rate <= 0 or to_rate <= 0:
        raise ValueError("sample rates must be positive")

    sample_count = len(audio_data) // 2
    if sample_count == 0:
        return b""
    samples = struct.unpack(f"<{sample_count}h", audio_data[: sample_count * 2])
    new_count = sample_count * to_rate // from_rate
    if new_count <= 0:
        return b""

    output: list[int] = []
    for index in range(new_count):
        src_pos = index * sample_count / new_count
        lo = int(src_pos)
        hi = min(lo + 1, sample_count - 1)
        frac = src_pos - lo
        sample = samples[lo] * (1.0 - frac) + samples[hi] * frac
        output.append(max(-32768, min(32767, round(sample))))
    return struct.pack(f"<{len(output)}h", *output)
