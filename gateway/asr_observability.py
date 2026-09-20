from typing import Any


ASR_OBSERVABILITY_METADATA_KEYS = (
    "asr_queue_wait_ms",
    "asr_inference_ms",
    "stt_provider",
    "stt_request_kind",
    "audio_bytes",
    "sample_rate",
)


def asr_observability_summary(asr_metadata: dict[str, Any]) -> dict[str, Any]:
    provider_metadata = asr_metadata.get("metadata")
    if not isinstance(provider_metadata, dict):
        return {}
    return {
        key: provider_metadata[key]
        for key in ASR_OBSERVABILITY_METADATA_KEYS
        if provider_metadata.get(key) is not None
    }


def build_audio_decoded_summary(
    *,
    trace_id: str | None,
    round_id: str | None,
    playback_id: str | None,
    audio_bytes: int,
    audio_metadata: dict[str, Any],
    audio_transport: str,
    audio_encoding: str,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "round_id": round_id,
        "playback_id": playback_id,
        "utterance_id": audio_metadata.get("utterance_id"),
        "audio_bytes": audio_bytes,
        "audio_duration_ms": audio_metadata.get("duration_ms"),
        "audio_transport": audio_transport,
        "audio_encoding": audio_encoding,
        **audio_metadata,
    }


def build_asr_trace_summary(
    *,
    trace_id: str | None,
    round_id: str | None,
    playback_id: str | None,
    audio_bytes: int,
    audio_metadata: dict[str, Any],
    audio_transport: str,
    audio_encoding: str,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "round_id": round_id,
        "playback_id": playback_id,
        "utterance_id": audio_metadata.get("utterance_id"),
        "audio_bytes": audio_bytes,
        "audio_duration_ms": audio_metadata.get("duration_ms"),
        "audio_transport": audio_transport,
        "audio_encoding": audio_encoding,
        "opus_packets": audio_metadata.get("opus_packets"),
        "stream_chunk_count": audio_metadata.get("stream_chunk_count"),
        "stream_elapsed_ms": audio_metadata.get("stream_elapsed_ms"),
    }


def build_asr_result_summary(
    asr_trace_summary: dict[str, Any],
    asr_metadata: dict[str, Any],
    *,
    text: str | None = None,
    cancelled: bool = False,
) -> dict[str, Any]:
    summary = {
        **asr_trace_summary,
        **asr_observability_summary(asr_metadata),
        "metadata": asr_metadata,
    }
    if text is not None:
        summary["text"] = text
    if cancelled:
        summary["cancelled"] = True
    return summary
