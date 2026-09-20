from __future__ import annotations

from dataclasses import dataclass


def pcm16_duration_ms(audio_data: bytes, sample_rate: int) -> float:
    return len(audio_data) / 2 / sample_rate * 1000.0 if audio_data else 0.0


@dataclass
class StreamGapStats:
    last_at: float | None = None
    max_ms: float = 0.0
    excess_max_ms: float = 0.0
    excess_count: int = 0
    excess_threshold_ms: float = 20.0

    def observe(self, now: float, expected_duration_ms: float) -> None:
        if self.last_at is not None:
            gap_ms = (now - self.last_at) * 1000.0
            self.max_ms = max(self.max_ms, gap_ms)
            gap_excess_ms = max(0.0, gap_ms - expected_duration_ms)
            self.excess_max_ms = max(self.excess_max_ms, gap_excess_ms)
            if gap_excess_ms > self.excess_threshold_ms:
                self.excess_count += 1
        self.last_at = now
