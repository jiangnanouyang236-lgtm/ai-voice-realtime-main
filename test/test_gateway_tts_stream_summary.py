from gateway.tts_stream_summary import (
    audio_duration_seconds,
    build_client_event_tts_error_summary,
    build_client_event_tts_start_summary,
    build_client_event_tts_summary,
    build_llm_done_summary,
    build_llm_first_token_summary,
    build_llm_internal_metrics_summary,
    build_llm_tts_cancelled_summary,
    build_llm_tts_error_summary,
    build_llm_tts_stale_round_summary,
    build_llm_tts_start_summary,
    build_llm_tts_summary,
    build_tts_connected_summary,
    build_tts_first_audio_summary,
    build_tts_first_commit_summary,
    build_ws_backpressure_summary,
    ws_send_average_ms,
)


def test_audio_duration_seconds_and_ws_average_handle_empty_values():
    assert audio_duration_seconds(0, 16000) == 0.0
    assert audio_duration_seconds(32000, 16000) == 1.0
    assert ws_send_average_ms(0, 0) == 0.0
    assert ws_send_average_ms(12.0, 3) == 4.0


def test_build_client_event_tts_start_summary():
    assert build_client_event_tts_start_summary(
        event_type="wake_idle",
        text="我在。",
        round_id="round-1",
        playback_id="round-1:playback",
        exit_after=False,
    ) == {
        "event_type": "wake_idle",
        "text": "我在。",
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "exit": False,
    }


def test_build_client_event_tts_error_summary():
    assert build_client_event_tts_error_summary(
        event_type="wake_idle",
        round_id="round-1",
        playback_id="round-1:playback",
    ) == {
        "event_type": "wake_idle",
        "round_id": "round-1",
        "playback_id": "round-1:playback",
    }


def test_build_llm_tts_stale_round_summary():
    assert build_llm_tts_stale_round_summary(
        round_id="round-1",
        playback_id="round-1:playback",
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
    }


def test_build_llm_tts_start_summary():
    assert build_llm_tts_start_summary(
        query="给我讲个故事",
        round_id="round-1",
        playback_id="round-1:playback",
    ) == {
        "query": "给我讲个故事",
        "round_id": "round-1",
        "playback_id": "round-1:playback",
    }


def test_build_tts_first_commit_summary():
    assert build_tts_first_commit_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        tts_first_commit_ms=123.4,
        text_chars=2,
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "tts_first_commit_ms": 123.4,
        "text_chars": 2,
    }


def test_build_tts_connected_summary():
    assert build_tts_connected_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        tts_connect_ms=12.5,
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "tts_connect_ms": 12.5,
    }


def test_build_llm_internal_metrics_summary_merges_metrics():
    assert build_llm_internal_metrics_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        metrics={"llm_router_ms": 10, "llm_response_chars": 42},
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "llm_router_ms": 10,
        "llm_response_chars": 42,
    }


def test_build_llm_first_token_summary():
    assert build_llm_first_token_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        llm_first_token_ms=88.0,
        chunk_chars=1,
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "llm_first_token_ms": 88.0,
        "chunk_chars": 1,
    }


def test_build_llm_done_summary_counts_text_chars():
    assert build_llm_done_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        llm_total_ms=456.0,
        text="你好",
        should_exit=True,
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "llm_total_ms": 456.0,
        "text": "你好",
        "text_chars": 2,
        "exit": True,
    }


def test_build_ws_backpressure_summary_marks_timeout():
    assert build_ws_backpressure_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        reason="ws_send_timeout",
        send_time_ms=1500.0,
        ws_slow_send_strikes=3,
        close_client=True,
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "reason": "ws_send_timeout",
        "ws_backpressure": True,
        "ws_slow_send_ms": 1500.0,
        "ws_send_timeout_ms": 1500.0,
        "ws_slow_send_strikes": 3,
        "close_client": True,
        "cancelled": True,
    }


def test_build_ws_backpressure_summary_omits_timeout_metric_for_slow_send():
    summary = build_ws_backpressure_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        reason="ws_slow_send",
        send_time_ms=900.0,
        ws_slow_send_strikes=1,
        close_client=False,
    )

    assert summary["ws_send_timeout_ms"] is None
    assert summary["ws_slow_send_ms"] == 900.0
    assert summary["close_client"] is False


def test_build_tts_first_audio_summary_preserves_tts_trace_fields():
    assert build_tts_first_audio_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        tts_first_audio_ms=200.0,
        chunk_bytes=2048,
        sample_rate=24000,
        ws_send_ms=4.5,
        tts_internal_first_pcm_ms=120.0,
        tts_first_text_to_first_pcm_ms=80.0,
        tts_gateway_after_server_pcm_ms=15.0,
        tts_request_to_grpc_yield_ms=9.0,
        tts_trace_id="tts-trace",
        tts_round_id="tts-round",
        tts_playback_id="tts-playback",
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "tts_first_audio_ms": 200.0,
        "chunk_bytes": 2048,
        "sample_rate": 24000,
        "ws_send_ms": 4.5,
        "tts_internal_first_pcm_ms": 120.0,
        "tts_first_text_to_first_pcm_ms": 80.0,
        "tts_gateway_after_server_pcm_ms": 15.0,
        "tts_request_to_grpc_yield_ms": 9.0,
        "tts_trace_id": "tts-trace",
        "tts_round_id": "tts-round",
        "tts_playback_id": "tts-playback",
    }


def test_build_llm_tts_cancelled_and_error_summaries():
    assert build_llm_tts_cancelled_summary(
        round_id="round-1",
        playback_id="round-1:playback",
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "cancelled": True,
    }
    assert build_llm_tts_error_summary(
        round_id="round-1",
        playback_id="round-1:playback",
    ) == {
        "round_id": "round-1",
        "playback_id": "round-1:playback",
    }


def test_build_client_event_tts_summary_collects_gaps_and_backpressure():
    summary = build_client_event_tts_summary(
        event_type="wake_idle",
        round_id="round-1",
        playback_id="round-1:playback",
        text="我在。",
        audio_chunks=2,
        audio_bytes=32000,
        audio_sample_rate=16000,
        tts_empty_audio_chunks=1,
        tts_connect_ms=12.5,
        tts_first_audio_ms=80.0,
        ws_send_ms_max=9.0,
        ws_send_ms_total=12.0,
        ws_send_count=3,
        grpc_recv_gap_max_ms=100.0,
        grpc_recv_gap_excess_max_ms=30.0,
        grpc_recv_gap_excess_count=1,
        ws_send_gap_max_ms=90.0,
        ws_send_gap_excess_max_ms=20.0,
        ws_send_gap_excess_count=2,
        ws_backpressure=True,
        ws_backpressure_send_ms=1500.0,
        ws_backpressure_reason="ws_slow_send",
        ws_slow_send_strikes=3,
        interrupted=False,
        stream_timed_out=False,
        exit_after=False,
    )

    assert summary["event_type"] == "wake_idle"
    assert summary["audio_duration_ms"] == 1000.0
    assert summary["ws_send_avg_ms"] == 4.0
    assert summary["grpc_gap_excess_count"] == 1
    assert summary["ws_gap_excess_count"] == 2
    assert summary["ws_backpressure"] is True
    assert summary["ws_backpressure_reason"] == "ws_slow_send"
    assert summary["cancelled"] is False
    assert summary["exit"] is False


def test_build_llm_tts_summary_includes_timeout_specific_metric():
    summary = build_llm_tts_summary(
        round_id="round-1",
        playback_id="round-1:playback",
        audio_chunks=2,
        audio_bytes=32000,
        audio_sample_rate=16000,
        tts_empty_audio_chunks=0,
        llm_first_token_ms=100.0,
        llm_total_ms=500.0,
        llm_internal_metrics={"router_ms": 10},
        tts_connect_ms=12.5,
        tts_first_commit_ms=110.0,
        tts_first_audio_ms=200.0,
        ws_send_ms_max=9.0,
        ws_send_ms_total=12.0,
        ws_send_count=3,
        ws_backpressure=True,
        ws_backpressure_send_ms=99.0,
        ws_backpressure_reason="ws_send_timeout",
        ws_slow_send_strikes=1,
        interrupted=True,
        stream_timed_out=True,
        should_exit=False,
    )

    assert summary["audio_duration_ms"] == 1000.0
    assert summary["audio_duration_sec"] == 1.0
    assert summary["ws_send_avg_ms"] == 4.0
    assert summary["ws_send_timeout_ms"] == 99.0
    assert summary["cancelled"] is True
    assert summary["timeout"] is True
    assert summary["exit"] is False
