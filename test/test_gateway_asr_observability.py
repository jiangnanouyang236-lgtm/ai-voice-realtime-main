from gateway.asr_observability import (
    asr_observability_summary,
    build_asr_result_summary,
    build_asr_trace_summary,
    build_audio_decoded_summary,
)


def test_asr_observability_summary_returns_empty_without_provider_metadata():
    assert asr_observability_summary({}) == {}
    assert asr_observability_summary({"metadata": "not-a-dict"}) == {}


def test_asr_observability_summary_keeps_known_non_null_fields():
    summary = asr_observability_summary(
        {
            "metadata": {
                "asr_queue_wait_ms": 4,
                "asr_inference_ms": 132,
                "stt_provider": "qwen",
                "stt_request_kind": "wav",
                "audio_bytes": 57600,
                "sample_rate": 16000,
                "ignored": "value",
                "optional_none": None,
            }
        }
    )

    assert summary == {
        "asr_queue_wait_ms": 4,
        "asr_inference_ms": 132,
        "stt_provider": "qwen",
        "stt_request_kind": "wav",
        "audio_bytes": 57600,
        "sample_rate": 16000,
    }


def test_asr_observability_summary_filters_none_values():
    summary = asr_observability_summary(
        {
            "metadata": {
                "asr_queue_wait_ms": None,
                "asr_inference_ms": 10,
                "stt_provider": None,
                "sample_rate": 16000,
            }
        }
    )

    assert summary == {"asr_inference_ms": 10, "sample_rate": 16000}


def test_build_audio_decoded_summary_includes_metadata_payload():
    summary = build_audio_decoded_summary(
        trace_id="trace-1",
        round_id="round-1",
        playback_id="playback-1",
        audio_bytes=1234,
        audio_metadata={
            "utterance_id": "utt-1",
            "duration_ms": 880.0,
            "opus_packets": 44,
            "custom": "value",
        },
        audio_transport="binary_stream",
        audio_encoding="opus",
    )

    assert summary == {
        "trace_id": "trace-1",
        "round_id": "round-1",
        "playback_id": "playback-1",
        "utterance_id": "utt-1",
        "audio_bytes": 1234,
        "audio_duration_ms": 880.0,
        "audio_transport": "binary_stream",
        "audio_encoding": "opus",
        "duration_ms": 880.0,
        "opus_packets": 44,
        "custom": "value",
    }


def test_build_asr_trace_summary_keeps_compact_audio_fields():
    summary = build_asr_trace_summary(
        trace_id="trace-1",
        round_id="round-1",
        playback_id="playback-1",
        audio_bytes=1234,
        audio_metadata={
            "utterance_id": "utt-1",
            "duration_ms": 880.0,
            "opus_packets": 44,
            "stream_chunk_count": 9,
            "stream_elapsed_ms": 910.0,
            "custom": "value",
        },
        audio_transport="binary_stream",
        audio_encoding="opus",
    )

    assert summary == {
        "trace_id": "trace-1",
        "round_id": "round-1",
        "playback_id": "playback-1",
        "utterance_id": "utt-1",
        "audio_bytes": 1234,
        "audio_duration_ms": 880.0,
        "audio_transport": "binary_stream",
        "audio_encoding": "opus",
        "opus_packets": 44,
        "stream_chunk_count": 9,
        "stream_elapsed_ms": 910.0,
    }


def test_build_asr_result_summary_merges_trace_provider_metadata_and_text():
    summary = build_asr_result_summary(
        {"trace_id": "trace-1", "round_id": "round-1", "audio_bytes": 1234},
        {
            "metadata": {
                "asr_queue_wait_ms": 2,
                "asr_inference_ms": 120,
                "stt_provider": "qwen",
                "ignored": "value",
            }
        },
        text="hello",
    )

    assert summary == {
        "trace_id": "trace-1",
        "round_id": "round-1",
        "audio_bytes": 1234,
        "asr_queue_wait_ms": 2,
        "asr_inference_ms": 120,
        "stt_provider": "qwen",
        "metadata": {
            "metadata": {
                "asr_queue_wait_ms": 2,
                "asr_inference_ms": 120,
                "stt_provider": "qwen",
                "ignored": "value",
            }
        },
        "text": "hello",
    }


def test_build_asr_result_summary_can_mark_cancelled_without_text():
    summary = build_asr_result_summary(
        {"trace_id": "trace-1"},
        {},
        cancelled=True,
    )

    assert summary == {
        "trace_id": "trace-1",
        "metadata": {},
        "cancelled": True,
    }
