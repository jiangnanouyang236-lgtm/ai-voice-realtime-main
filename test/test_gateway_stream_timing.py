from gateway.stream_timing import StreamGapStats, pcm16_duration_ms


def test_pcm16_duration_ms_handles_empty_and_mono_pcm():
    assert pcm16_duration_ms(b"", 16000) == 0.0
    assert pcm16_duration_ms(b"\x00\x00" * 320, 16000) == 20.0


def test_stream_gap_stats_tracks_max_gap_and_excess_count():
    stats = StreamGapStats(excess_threshold_ms=20.0)

    stats.observe(100.00, 20.0)
    stats.observe(100.03, 20.0)
    stats.observe(100.10, 20.0)

    assert round(stats.max_ms, 1) == 70.0
    assert round(stats.excess_max_ms, 1) == 50.0
    assert stats.excess_count == 1
    assert stats.last_at == 100.10
