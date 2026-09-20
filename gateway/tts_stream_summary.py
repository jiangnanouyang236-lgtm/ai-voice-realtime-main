from __future__ import annotations

from typing import Any


def audio_duration_seconds(audio_bytes: int, sample_rate: int) -> float:
    return audio_bytes / 2 / sample_rate if audio_bytes else 0.0


def ws_send_average_ms(total_ms: float, count: int) -> float:
    return total_ms / count if count else 0.0


def build_client_event_tts_start_summary(
    *,
    event_type: str | None,
    text: str,
    round_id: str | None,
    playback_id: str | None,
    exit_after: bool,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "text": text,
        "round_id": round_id,
        "playback_id": playback_id,
        "exit": exit_after,
    }


def build_client_event_tts_error_summary(
    *,
    event_type: str | None,
    round_id: str | None,
    playback_id: str | None,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "round_id": round_id,
        "playback_id": playback_id,
    }


def build_llm_tts_stale_round_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
) -> dict[str, Any]:
    return {"round_id": round_id, "playback_id": playback_id}


def build_llm_tts_start_summary(
    *,
    query: str,
    round_id: str | None,
    playback_id: str | None,
) -> dict[str, Any]:
    return {"query": query, "round_id": round_id, "playback_id": playback_id}


def build_tts_first_commit_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    tts_first_commit_ms: float,
    text_chars: int,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "tts_first_commit_ms": tts_first_commit_ms,
        "text_chars": text_chars,
    }


def build_tts_connected_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    tts_connect_ms: float,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "tts_connect_ms": tts_connect_ms,
    }


def build_llm_internal_metrics_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        **metrics,
    }


def build_llm_first_token_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    llm_first_token_ms: float,
    chunk_chars: int,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "llm_first_token_ms": llm_first_token_ms,
        "chunk_chars": chunk_chars,
    }


def build_llm_done_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    llm_total_ms: float,
    text: str,
    should_exit: bool,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "llm_total_ms": llm_total_ms,
        "text": text,
        "text_chars": len(text),
        "exit": should_exit,
    }


def build_ws_backpressure_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    reason: str,
    send_time_ms: float,
    ws_slow_send_strikes: int,
    close_client: bool,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "reason": reason,
        "ws_backpressure": True,
        "ws_slow_send_ms": send_time_ms,
        "ws_send_timeout_ms": send_time_ms if reason == "ws_send_timeout" else None,
        "ws_slow_send_strikes": ws_slow_send_strikes,
        "close_client": close_client,
        "cancelled": True,
    }


def build_tts_first_audio_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    tts_first_audio_ms: float,
    chunk_bytes: int,
    sample_rate: int,
    ws_send_ms: float,
    tts_internal_first_pcm_ms: float,
    tts_first_text_to_first_pcm_ms: float,
    tts_gateway_after_server_pcm_ms: float | None,
    tts_request_to_grpc_yield_ms: float,
    tts_trace_id: str,
    tts_round_id: str,
    tts_playback_id: str,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "tts_first_audio_ms": tts_first_audio_ms,
        "chunk_bytes": chunk_bytes,
        "sample_rate": sample_rate,
        "ws_send_ms": ws_send_ms,
        "tts_internal_first_pcm_ms": tts_internal_first_pcm_ms,
        "tts_first_text_to_first_pcm_ms": tts_first_text_to_first_pcm_ms,
        "tts_gateway_after_server_pcm_ms": tts_gateway_after_server_pcm_ms,
        "tts_request_to_grpc_yield_ms": tts_request_to_grpc_yield_ms,
        "tts_trace_id": tts_trace_id,
        "tts_round_id": tts_round_id,
        "tts_playback_id": tts_playback_id,
    }


def build_llm_tts_cancelled_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
) -> dict[str, Any]:
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "cancelled": True,
    }


def build_llm_tts_error_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
) -> dict[str, Any]:
    return {"round_id": round_id, "playback_id": playback_id}


def build_client_event_tts_summary(
    *,
    event_type: str | None,
    round_id: str | None,
    playback_id: str | None,
    text: str,
    audio_chunks: int,
    audio_bytes: int,
    audio_sample_rate: int,
    tts_empty_audio_chunks: int,
    tts_connect_ms: float | None,
    tts_first_audio_ms: float | None,
    ws_send_ms_max: float,
    ws_send_ms_total: float,
    ws_send_count: int,
    grpc_recv_gap_max_ms: float,
    grpc_recv_gap_excess_max_ms: float,
    grpc_recv_gap_excess_count: int,
    ws_send_gap_max_ms: float,
    ws_send_gap_excess_max_ms: float,
    ws_send_gap_excess_count: int,
    ws_backpressure: bool,
    ws_backpressure_send_ms: float | None,
    ws_backpressure_reason: str | None,
    ws_slow_send_strikes: int,
    interrupted: bool,
    stream_timed_out: bool,
    exit_after: bool,
) -> dict[str, Any]:
    audio_duration_sec = audio_duration_seconds(audio_bytes, audio_sample_rate)
    return {
        "event_type": event_type,
        "round_id": round_id,
        "playback_id": playback_id,
        "text": text,
        "audio_chunks": audio_chunks,
        "audio_bytes": audio_bytes,
        "tts_empty_audio_chunks": tts_empty_audio_chunks,
        "audio_duration_ms": audio_duration_sec * 1000,
        "tts_connect_ms": tts_connect_ms,
        "tts_first_audio_ms": tts_first_audio_ms,
        "ws_send_ms": ws_send_ms_max,
        "ws_send_avg_ms": ws_send_average_ms(ws_send_ms_total, ws_send_count),
        "ws_send_count": ws_send_count,
        "grpc_gap_max_ms": grpc_recv_gap_max_ms,
        "grpc_gap_excess_max_ms": grpc_recv_gap_excess_max_ms,
        "grpc_gap_excess_count": grpc_recv_gap_excess_count,
        "ws_gap_max_ms": ws_send_gap_max_ms,
        "ws_gap_excess_max_ms": ws_send_gap_excess_max_ms,
        "ws_gap_excess_count": ws_send_gap_excess_count,
        "ws_backpressure": ws_backpressure,
        "ws_slow_send_ms": ws_backpressure_send_ms,
        "ws_backpressure_reason": ws_backpressure_reason,
        "ws_slow_send_strikes": ws_slow_send_strikes,
        "cancelled": interrupted,
        "timeout": stream_timed_out,
        "exit": exit_after,
    }


def build_llm_tts_summary(
    *,
    round_id: str | None,
    playback_id: str | None,
    audio_chunks: int,
    audio_bytes: int,
    audio_sample_rate: int,
    tts_empty_audio_chunks: int,
    llm_first_token_ms: float | None,
    llm_total_ms: float | None,
    llm_internal_metrics: dict[str, Any],
    tts_connect_ms: float | None,
    tts_first_commit_ms: float | None,
    tts_first_audio_ms: float | None,
    ws_send_ms_max: float,
    ws_send_ms_total: float,
    ws_send_count: int,
    ws_backpressure: bool,
    ws_backpressure_send_ms: float | None,
    ws_backpressure_reason: str | None,
    ws_slow_send_strikes: int,
    interrupted: bool,
    stream_timed_out: bool,
    should_exit: bool,
) -> dict[str, Any]:
    audio_duration_sec = audio_duration_seconds(audio_bytes, audio_sample_rate)
    return {
        "round_id": round_id,
        "playback_id": playback_id,
        "audio_chunks": audio_chunks,
        "audio_bytes": audio_bytes,
        "tts_empty_audio_chunks": tts_empty_audio_chunks,
        "audio_duration_ms": audio_duration_sec * 1000,
        "audio_duration_sec": round(audio_duration_sec, 2),
        "llm_first_token_ms": llm_first_token_ms,
        "llm_total_ms": llm_total_ms,
        "llm_internal_metrics": llm_internal_metrics,
        "tts_connect_ms": tts_connect_ms,
        "tts_first_commit_ms": tts_first_commit_ms,
        "tts_first_audio_ms": tts_first_audio_ms,
        "ws_send_ms": ws_send_ms_max,
        "ws_send_avg_ms": ws_send_average_ms(ws_send_ms_total, ws_send_count),
        "ws_send_count": ws_send_count,
        "ws_backpressure": ws_backpressure,
        "ws_slow_send_ms": ws_backpressure_send_ms,
        "ws_send_timeout_ms": (
            ws_backpressure_send_ms
            if ws_backpressure_reason == "ws_send_timeout"
            else None
        ),
        "ws_slow_send_strikes": ws_slow_send_strikes,
        "ws_backpressure_reason": ws_backpressure_reason,
        "cancelled": interrupted,
        "timeout": stream_timed_out,
        "exit": should_exit,
    }
