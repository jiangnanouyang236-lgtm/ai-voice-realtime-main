"""
FastAPI Gateway 服务器

WebSocket 接入，业务编排
"""

import asyncio
import logging
import time
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional
from datetime import datetime, timezone

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import grpc

# 添加父目录到路径
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 导入配置和会话管理
from gateway.config import *
from voice_logging import configure_logging
from gateway.rtc_config import (
    RtcGatewaySettings,
    parse_rtc_ice_servers,
)
from gateway.rtc_signaling import (
    build_rtc_offer_fallback_ack as _build_rtc_offer_fallback_ack,
    build_transport_fallback_start_ack as _build_transport_fallback_start_ack,
    build_transport_ready_fallback_ack as _build_transport_ready_fallback_ack,
    rtc_session_matches as _rtc_session_matches,
    rtc_session_mismatch_error as _rtc_session_mismatch_error,
)
from gateway.internal_voice_protocol import (
    TYPE_CLIENT_EVENT,
    TYPE_HEARTBEAT_PING,
    TYPE_HEARTBEAT_PONG,
    TYPE_INPUT_AUDIO_BATCH,
    TYPE_INPUT_AUDIO_CANCEL,
    TYPE_INPUT_AUDIO_END,
    TYPE_INPUT_AUDIO_START,
    TYPE_INPUT_TEXT_COMMIT,
    TYPE_INTERRUPT,
    TYPE_ORCHESTRATOR_STATUS,
    TYPE_PLAYBACK_REPORT,
    TYPE_PROTOCOL_ERROR,
    TYPE_RESPONSE_ASR,
    TYPE_RESPONSE_CANCELLED,
    TYPE_RESPONSE_DONE,
    TYPE_RESPONSE_ERROR,
    TYPE_SESSION_CLOSE,
    TYPE_SESSION_OPEN,
    InternalVoiceEnvelope,
)
from gateway.internal_voice_session_state import InternalVoiceSessionStateTracker
from gateway.upstream_config import build_initial_upstream_statuses, build_upstream_specs
from gateway.audio_protocol import (
    AUDIO_FRAME_HEADER_MAX_BYTES,
    AUDIO_FRAME_MAGIC,
    AUDIO_FRAME_VERSION,
    AudioValidationError,
    decode_audio_frame,
    decode_client_audio_frame,
    decode_ws_text_message,
    encode_audio_frame,
)
from gateway.barge_in import decide_barge_in
from gateway.request_queue import (
    attach_request_queue_metrics as _attach_request_queue_metrics,
    build_request_dequeued_summary as _build_request_dequeued_summary,
    drain_request_queue as _drain_request_queue,
    enqueue_latest_request,
    pop_request_queue_metrics as _pop_request_queue_metrics,
)
from gateway.env_utils import (
    get_env_bool as _get_env_bool,
    get_env_float as _get_env_float,
    get_env_int as _get_env_int,
)
from gateway.grpc_control import (
    cancel_grpc_call_holder as _cancel_grpc_call_holder,
    channel_connectivity_label as _channel_connectivity_label,
    create_grpc_channel as _create_grpc_channel,
    grpc_timeout_arg as _grpc_timeout_arg,
    is_cancelled_grpc_error as _is_cancelled_grpc_error,
    is_channel_ready as _is_channel_ready,
    is_locally_cancelled_grpc_error as _is_locally_cancelled_grpc_error,
)
from gateway.llm_metrics import parse_llm_metrics_json as _parse_llm_metrics_json
from gateway.playback_report import (
    build_client_playback_trace_event as _build_client_playback_trace_event,
    build_playback_cancel_payload as _build_playback_cancel_payload,
)
from gateway.workflow_coordinator import WorkflowCoordinator, WorkflowRunRequest
from gateway.workflow_grpc_client import GrpcWorkflowClient
from gateway.workflow_tts_pipeline import WorkflowTTSPipeline
from gateway.workflow_playback_barrier import (
    PLAYBACK_COMPLETE,
    PLAYBACK_INTERRUPTED,
    PlaybackBarrierRegistry,
)
from gateway.text_streaming import (
    build_direct_text_asr_metadata as _build_direct_text_asr_metadata,
    build_direct_text_summary as _build_direct_text_summary,
    split_control_safe_buffer as _split_control_safe_buffer,
    strip_standalone_tool_tags as _strip_standalone_tool_tags,
)
from gateway.singing_playback import load_singing_audio as _load_singing_audio
from gateway.stt_context import (
    build_llm_query_with_audio_context as _build_llm_query_with_audio_context,
    stt_metadata as _stt_metadata,
)
from gateway.tts_chunks import build_tts_text_chunk as _build_tts_text_chunk
from gateway.stream_timing import (
    StreamGapStats,
    pcm16_duration_ms as _pcm16_duration_ms,
)
from gateway.tts_stream_summary import (
    audio_duration_seconds as _audio_duration_seconds,
    build_client_event_tts_error_summary as _build_client_event_tts_error_summary,
    build_client_event_tts_start_summary as _build_client_event_tts_start_summary,
    build_client_event_tts_summary as _build_client_event_tts_summary,
    build_llm_done_summary as _build_llm_done_summary,
    build_llm_first_token_summary as _build_llm_first_token_summary,
    build_llm_internal_metrics_summary as _build_llm_internal_metrics_summary,
    build_llm_tts_cancelled_summary as _build_llm_tts_cancelled_summary,
    build_llm_tts_error_summary as _build_llm_tts_error_summary,
    build_llm_tts_stale_round_summary as _build_llm_tts_stale_round_summary,
    build_llm_tts_start_summary as _build_llm_tts_start_summary,
    build_llm_tts_summary as _build_llm_tts_summary,
    build_tts_connected_summary as _build_tts_connected_summary,
    build_tts_first_commit_summary as _build_tts_first_commit_summary,
    build_tts_first_audio_summary as _build_tts_first_audio_summary,
    build_ws_backpressure_summary as _build_ws_backpressure_summary,
)
from gateway.turn_context import (
    build_turn_ids as _build_turn_ids,
    build_turn_trace as _build_turn_trace,
)
from gateway.asr_observability import (
    build_asr_result_summary as _build_asr_result_summary,
    build_asr_trace_summary as _build_asr_trace_summary,
    build_audio_decoded_summary as _build_audio_decoded_summary,
)
from gateway.asr_pipeline import process_asr_audio as _process_asr_audio
from gateway.turn_gate_shadow_models import (
    run_livekit_eou_shadow as _run_livekit_eou_shadow,
    run_smart_turn_shadow as _run_smart_turn_shadow,
    warmup_turn_gate_shadow_models as _warmup_turn_gate_shadow_models,
)
from gateway.turn_gate_policy import decide_turn_gate
from gateway.recognition import is_valid_recognition
from gateway.client_events import (
    CLIENT_EVENT_PHRASES,
    CLIENT_EVENT_TYPES,
    build_client_event_phrase_summary as _build_client_event_phrase_summary,
    build_client_event_received_summary as _build_client_event_received_summary,
    pick_client_event_phrase,
)
from gateway.ws_transport import (
    WebSocketBackpressureError,
    send_audio_pcm_message as _send_audio_pcm_message,
    send_error_message as _send_error_message,
    send_json_message as _send_json_message,
)
from gateway.robot_session import (
    build_registered_message_payload as _build_registered_message_payload,
    register_robot_session as _register_robot_session,
    resolve_request_bot as _resolve_request_bot,
)
from gateway.wav_audio import (
    truncate_wav_audio_to_limit,
    validate_wav_audio,
    wav_to_pcm,
)
from gateway.opus_audio import (
    DEFAULT_TTS_SAMPLE_RATE,
    AudioStreamAssembler,
    OpusPCMStreamEncoder,
    audio_stream_utterance_id as _audio_stream_utterance_id,
    build_audio_end_log_fields as _build_audio_end_log_fields,
    decode_opus_audio_to_wav,
    parse_opus_packet_stream,
)
from voice_quick_replies import pick_quick_reply

# 保证 gateway_server.py 和 gateway/config.py 线上部署不同步时，ASR -> LLM 主链路不被配置缺失打断。
if "STT_AUDIO_CONTEXT_TO_LLM" not in globals():
    STT_AUDIO_CONTEXT_TO_LLM = os.getenv("STT_AUDIO_CONTEXT_TO_LLM", "true").lower() != "false"


DIRECT_TEXT_MAX_CHARS = max(1, _get_env_int("GATEWAY_DIRECT_TEXT_MAX_CHARS", 2000))
EXIT_TAG = "[EXIT]"
if "GATEWAY_STT_RPC_TIMEOUT_SEC" not in globals():
    GATEWAY_STT_RPC_TIMEOUT_SEC = max(0.0, _get_env_float("GATEWAY_STT_RPC_TIMEOUT_SEC", 15.0))
if "GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC" not in globals():
    GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC = max(0.0, _get_env_float("GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC", 120.0))
if "GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC" not in globals():
    GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC = max(0.0, _get_env_float("GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 120.0))
if "GATEWAY_LLM_SESSION_CLEANUP_TIMEOUT_SEC" not in globals():
    GATEWAY_LLM_SESSION_CLEANUP_TIMEOUT_SEC = max(0.0, _get_env_float("GATEWAY_LLM_SESSION_CLEANUP_TIMEOUT_SEC", 3.0))
if "GATEWAY_REQUEST_QUEUE_MAXSIZE" not in globals():
    GATEWAY_REQUEST_QUEUE_MAXSIZE = max(1, _get_env_int("GATEWAY_REQUEST_QUEUE_MAXSIZE", 1))
if "GATEWAY_WS_SEND_TIMEOUT_SEC" not in globals():
    GATEWAY_WS_SEND_TIMEOUT_SEC = max(0.0, _get_env_float("GATEWAY_WS_SEND_TIMEOUT_SEC", 5.0))
if "GATEWAY_WS_AUDIO_SLOW_SEND_MS" not in globals():
    GATEWAY_WS_AUDIO_SLOW_SEND_MS = max(0.0, _get_env_float("GATEWAY_WS_AUDIO_SLOW_SEND_MS", 1500.0))
if "GATEWAY_WS_SLOW_SEND_MAX_STRIKES" not in globals():
    GATEWAY_WS_SLOW_SEND_MAX_STRIKES = max(1, _get_env_int("GATEWAY_WS_SLOW_SEND_MAX_STRIKES", 3))
if "GATEWAY_COMPLEX_WORKFLOW_ENABLED" not in globals():
    GATEWAY_COMPLEX_WORKFLOW_ENABLED = _get_env_bool("GATEWAY_COMPLEX_WORKFLOW_ENABLED", False)
if "GATEWAY_WORKFLOW_RPC_TIMEOUT_SEC" not in globals():
    GATEWAY_WORKFLOW_RPC_TIMEOUT_SEC = max(
        1.0, _get_env_float("GATEWAY_WORKFLOW_RPC_TIMEOUT_SEC", 30.0)
    )
if "GATEWAY_WORKFLOW_STREAM_TIMEOUT_SEC" not in globals():
    GATEWAY_WORKFLOW_STREAM_TIMEOUT_SEC = max(
        1.0, _get_env_float("GATEWAY_WORKFLOW_STREAM_TIMEOUT_SEC", 120.0)
    )
if "GATEWAY_WORKFLOW_PLAYBACK_TIMEOUT_SEC" not in globals():
    GATEWAY_WORKFLOW_PLAYBACK_TIMEOUT_SEC = max(
        1.0, _get_env_float("GATEWAY_WORKFLOW_PLAYBACK_TIMEOUT_SEC", 30.0)
    )
if "GATEWAY_RTC_SIGNALING_ENABLED" not in globals():
    GATEWAY_RTC_SIGNALING_ENABLED = _get_env_bool("GATEWAY_RTC_SIGNALING_ENABLED", False)
if "GATEWAY_RTC_AUDIO_DIRECTION" not in globals():
    GATEWAY_RTC_AUDIO_DIRECTION = (
        os.getenv("GATEWAY_RTC_AUDIO_DIRECTION", "sendrecv").strip() or "sendrecv"
    )
if "GATEWAY_RTC_AUDIO_CODEC" not in globals():
    GATEWAY_RTC_AUDIO_CODEC = os.getenv("GATEWAY_RTC_AUDIO_CODEC", "opus").strip() or "opus"
if "GATEWAY_RTC_AUDIO_SAMPLE_RATE" not in globals():
    GATEWAY_RTC_AUDIO_SAMPLE_RATE = max(
        1,
        _get_env_int("GATEWAY_RTC_AUDIO_SAMPLE_RATE", 48000),
    )
if "GATEWAY_RTC_AUDIO_CHANNELS" not in globals():
    GATEWAY_RTC_AUDIO_CHANNELS = max(1, _get_env_int("GATEWAY_RTC_AUDIO_CHANNELS", 1))
if "GATEWAY_RTC_AUDIO_PTIME_MS" not in globals():
    GATEWAY_RTC_AUDIO_PTIME_MS = max(1, _get_env_int("GATEWAY_RTC_AUDIO_PTIME_MS", 20))


def build_gateway_rtc_settings() -> RtcGatewaySettings:
    return RtcGatewaySettings(
        signaling_enabled=GATEWAY_RTC_SIGNALING_ENABLED,
        ice_servers=list(GATEWAY_RTC_ICE_SERVERS),
        audio_direction=GATEWAY_RTC_AUDIO_DIRECTION,
        audio_codec=GATEWAY_RTC_AUDIO_CODEC,
        audio_sample_rate=GATEWAY_RTC_AUDIO_SAMPLE_RATE,
        audio_channels=GATEWAY_RTC_AUDIO_CHANNELS,
        audio_ptime_ms=GATEWAY_RTC_AUDIO_PTIME_MS,
    )


def build_rtc_status() -> dict[str, Any]:
    return build_gateway_rtc_settings().status_payload()


if "GATEWAY_RTC_ICE_SERVERS" not in globals():
    GATEWAY_RTC_ICE_SERVERS = parse_rtc_ice_servers()


from gateway.session_manager import SessionManager
from gateway.trace_recorder import TraceRecorder
from gateway.runtime_state import GatewayRuntimeState
from gateway.runtime_api import (
    build_runtime_robots_payload,
    build_runtime_session_payload,
    build_stats_payload,
)

# 导入 gRPC 服务
from stt import stt_service_pb2_grpc
from llm import llm_service_pb2, llm_service_pb2_grpc, workflow_service_pb2_grpc
from tts import tts_service_pb2_grpc

# 配置日志
configure_logging("gateway", force=True)
logger = logging.getLogger(__name__)

# 创建 FastAPI 应用
app = FastAPI(title="Voice Gateway", version="1.0.0")

# 会话管理器
session_manager = SessionManager(
    max_history=MAX_HISTORY_LENGTH
)
trace_recorder = TraceRecorder(
    enabled=TRACE_ENABLED,
    max_events=TRACE_MAX_EVENTS,
    max_rounds=TRACE_MAX_ROUNDS,
    text_max_chars=TRACE_TEXT_MAX_CHARS,
    error_max_chars=TRACE_ERROR_MAX_CHARS,
)
workflow_playback_barriers = PlaybackBarrierRegistry()
workflow_coordinator: WorkflowCoordinator | None = None


def _enqueue_latest_user_request(
    request_queue: asyncio.Queue,
    data: dict,
    *,
    session_id: str,
    reason: str,
    cancel_active_round: bool = True,
) -> int:
    """Keep only the newest user input and optionally cancel the active round."""
    if cancel_active_round:
        session_manager.set_interrupted(session_id, True)

    dropped = enqueue_latest_request(request_queue, data)
    if dropped:
        logger.warning("会话 %s: 丢弃 %s 条待处理请求 (%s)", session_id, dropped, reason)
    return dropped


def _cancel_current_round_for_new_input(session_id: str) -> dict[str, str] | None:
    cancel_current_round = getattr(session_manager, "cancel_current_round", None)
    if callable(cancel_current_round):
        return cancel_current_round(session_id)
    session_manager.set_interrupted(session_id, True)
    return None


async def _send_playback_cancel(
    websocket: WebSocket,
    *,
    session_id: str,
    cancelled_round: dict[str, str] | None,
    reason: str,
) -> None:
    payload = _build_playback_cancel_payload(cancelled_round, reason=reason)
    if payload is None:
        return
    await send_message(
        websocket,
        "playback_cancel",
        **payload,
    )
    logger.info(
        "会话 %s: 已发送 playback_cancel round_id=%s reason=%s",
        session_id,
        payload["round_id"],
        reason,
    )


async def _queue_new_user_input(
    websocket: WebSocket,
    request_queue: asyncio.Queue,
    data: dict,
    *,
    session_id: str,
    queue_reason: str,
    playback_cancel_reason: str,
    send_cancel_before_enqueue: bool = False,
) -> int:
    cancelled_round = _cancel_current_round_for_new_input(session_id)
    if send_cancel_before_enqueue:
        await _send_playback_cancel(
            websocket,
            session_id=session_id,
            cancelled_round=cancelled_round,
            reason=playback_cancel_reason,
        )
    dropped = _enqueue_latest_user_request(
        request_queue,
        data,
        session_id=session_id,
        reason=queue_reason,
        cancel_active_round=False,
    )
    _attach_request_queue_metrics(data, request_queue, dropped=dropped)
    if not send_cancel_before_enqueue:
        await _send_playback_cancel(
            websocket,
            session_id=session_id,
            cancelled_round=cancelled_round,
            reason=playback_cancel_reason,
        )
    return dropped


async def _send_rtc_session_mismatch_if_needed(
    websocket: WebSocket,
    data: dict,
    *,
    session_id: str,
    message_type: str,
) -> bool:
    if _rtc_session_matches(data, session_id):
        return False
    code, message = _rtc_session_mismatch_error(message_type)
    await send_error(websocket, code, message)
    return True


async def _handle_register_message(
    websocket: WebSocket,
    *,
    session_id: str,
    data: dict,
    msg_type: str,
) -> bool:
    if msg_type != "register":
        return False

    session_manager.touch_session(session_id)
    try:
        registration = await register_robot_session(session_id, data)
        await send_message(
            websocket,
            "registered",
            **_build_registered_message_payload(session_id, registration),
        )
        await send_rtc_config_if_enabled(websocket, session_id)
    except ValueError as exc:
        await send_error(websocket, "REGISTER_FAILED", str(exc))
    except RuntimeError as exc:
        await send_error(websocket, "REGISTER_FAILED", str(exc))
    return True


async def _handle_rtc_signaling_message(
    websocket: WebSocket,
    *,
    session_id: str,
    data: dict,
    msg_type: str,
) -> bool:
    if msg_type == "rtc_offer":
        session_manager.touch_session(session_id)
        if await _send_rtc_session_mismatch_if_needed(
            websocket,
            data,
            session_id=session_id,
            message_type="rtc_offer",
        ):
            return True
        await send_message(
            websocket,
            "transport_fallback_ack",
            **_build_rtc_offer_fallback_ack(
                session_id,
                signaling_enabled=GATEWAY_RTC_SIGNALING_ENABLED,
            ),
        )
        logger.info(
            "会话 %s: 收到 rtc_offer sdp_bytes=%s，当前回退 websocket 媒体",
            session_id,
            len(str(data.get("sdp") or "")),
        )
        return True

    if msg_type == "rtc_ice_candidate":
        session_manager.touch_session(session_id)
        if await _send_rtc_session_mismatch_if_needed(
            websocket,
            data,
            session_id=session_id,
            message_type="rtc_ice_candidate",
        ):
            return True
        logger.debug(
            "会话 %s: 收到 rtc_ice_candidate mid=%s mline=%s bytes=%s",
            session_id,
            data.get("sdp_mid") or "-",
            data.get("sdp_mline_index"),
            len(str(data.get("candidate") or "")),
        )
        return True

    if msg_type == "transport_ready":
        session_manager.touch_session(session_id)
        if await _send_rtc_session_mismatch_if_needed(
            websocket,
            data,
            session_id=session_id,
            message_type="transport_ready",
        ):
            return True
        await send_message(
            websocket,
            "transport_fallback_ack",
            **_build_transport_ready_fallback_ack(session_id),
        )
        logger.info(
            "会话 %s: 收到 transport_ready active_transport=%s，当前回退 websocket 媒体",
            session_id,
            data.get("active_transport") or "-",
        )
        return True

    if msg_type == "transport_fallback_start":
        session_manager.touch_session(session_id)
        if await _send_rtc_session_mismatch_if_needed(
            websocket,
            data,
            session_id=session_id,
            message_type="transport_fallback_start",
        ):
            return True
        fallback_ack = _build_transport_fallback_start_ack(session_id, data)
        await send_message(
            websocket,
            "transport_fallback_ack",
            **fallback_ack,
        )
        logger.info(
            "会话 %s: 收到 transport_fallback_start from=%s to=%s reason=%s",
            session_id,
            data.get("from_transport") or "-",
            fallback_ack["active_transport"],
            fallback_ack["reason"],
        )
        return True

    return False


async def _handle_keepalive_message(websocket: WebSocket, session_id: str, msg_type: str) -> bool:
    if msg_type == "ping":
        session_manager.touch_session(session_id)
        await send_message(websocket, "pong")
        return True
    if msg_type == "heartbeat":
        session_manager.touch_session(session_id)
        await send_message(websocket, "heartbeat_ack")
        logger.debug("会话 %s: 收到心跳", session_id)
        return True
    return False


async def _handle_audio_stream_message(
    websocket: WebSocket,
    request_queue: asyncio.Queue,
    audio_streams: dict[str, AudioStreamAssembler],
    *,
    session_id: str,
    data: dict,
    msg_type: str,
) -> bool:
    if msg_type == "audio_start":
        session_manager.touch_session(session_id)
        if ROBOT_SECRET_REQUIRED and not session_manager.is_registered(session_id):
            await send_error(websocket, "REGISTER_REQUIRED", "请先完成 Robot 注册认证")
            return True
        try:
            assembler = AudioStreamAssembler(data)
        except AudioValidationError as exc:
            await send_error(websocket, exc.code, exc.message)
            return True
        cancelled_round = _cancel_current_round_for_new_input(session_id)
        audio_streams[assembler.utterance_id] = assembler
        await _send_playback_cancel(
            websocket,
            session_id=session_id,
            cancelled_round=cancelled_round,
            reason="audio_start",
        )
        logger.info(
            "会话 %s: audio_start utterance_id=%s sample_rate=%s channels=%s",
            session_id,
            assembler.utterance_id,
            assembler.sample_rate,
            assembler.channels,
        )
        return True

    if msg_type == "audio_chunk":
        session_manager.touch_session(session_id)
        utterance_id = _audio_stream_utterance_id(data)
        assembler = audio_streams.get(utterance_id)
        if assembler is None:
            await send_error(websocket, "INVALID_AUDIO_STREAM", "audio_chunk 未找到对应 audio_start")
            return True
        try:
            assembler.append(data)
        except AudioValidationError as exc:
            audio_streams.pop(utterance_id, None)
            await send_error(websocket, exc.code, exc.message)
            return True
        logger.debug(
            "会话 %s: 收到 audio_chunk utterance_id=%s chunk_seq=%s chunks=%s packets=%s bytes=%s",
            session_id,
            utterance_id,
            data.get("chunk_seq"),
            assembler.chunks,
            len(assembler.packets),
            assembler.source_audio_bytes,
        )
        return True

    if msg_type == "audio_end":
        session_manager.touch_session(session_id)
        utterance_id = _audio_stream_utterance_id(data)
        assembler = audio_streams.pop(utterance_id, None)
        if assembler is None:
            await send_error(websocket, "INVALID_AUDIO_STREAM", "audio_end 未找到对应 audio_start")
            return True
        try:
            audio_data = assembler.finish(data)
        except AudioValidationError as exc:
            await send_error(websocket, exc.code, exc.message)
            return True
        dropped = _enqueue_latest_user_request(
            request_queue,
            audio_data,
            session_id=session_id,
            reason="收到新的流式音频，保留最新一轮",
            cancel_active_round=False,
        )
        _attach_request_queue_metrics(audio_data, request_queue, dropped=dropped)
        log_fields = _build_audio_end_log_fields(
            audio_data,
            utterance_id=utterance_id,
            queue_size=request_queue.qsize(),
            dropped=dropped,
        )
        logger.info(
            "会话 %s: audio_end utterance_id=%s chunks=%s packets=%s opus_bytes=%s queue_size=%s dropped=%s elapsed=%.1fms",
            session_id,
            log_fields["utterance_id"],
            log_fields["chunks"],
            log_fields["packets"],
            log_fields["opus_bytes"],
            log_fields["queue_size"],
            log_fields["dropped"],
            log_fields["elapsed_ms"],
        )
        return True

    if msg_type == "audio_cancel":
        session_manager.touch_session(session_id)
        utterance_id = _audio_stream_utterance_id(data)
        if utterance_id:
            audio_streams.pop(utterance_id, None)
        logger.info(
            "会话 %s: audio_cancel utterance_id=%s reason=%s",
            session_id,
            utterance_id,
            data.get("reason"),
        )
        return True

    return False


async def _handle_turn_candidate_shadow_message(
    websocket: WebSocket,
    *,
    session_id: str,
    data: dict[str, Any],
    msg_type: str,
) -> bool:
    if msg_type != "turn_candidate":
        return False

    session_manager.touch_session(session_id)
    candidate_seq = data.get("candidate_seq")
    speech_epoch = data.get("speech_epoch")
    audio_watermark = data.get("audio_watermark")
    silence_ms = data.get("silence_ms")
    utterance_id = str(data.get("utterance_id") or "").strip()
    trace_id = str(data.get("trace_id") or "").strip()

    try:
        candidate_seq = int(candidate_seq or 0)
        speech_epoch = int(speech_epoch or 0)
        audio_watermark = int(audio_watermark or 0)
        silence_ms = int(silence_ms or 0)
    except (TypeError, ValueError):
        candidate_seq = 0

    shadow = bool(data.get("shadow"))
    active_candidate = not shadow
    if (
        (active_candidate and not TURN_GATE_ACTIVE_ENABLED)
        or (shadow and not TURN_GATE_SHADOW_ENABLED)
        or not utterance_id
        or not trace_id
        or candidate_seq <= 0
        or audio_watermark <= 0
    ):
        await send_error(websocket, "INVALID_TURN_CANDIDATE", "Turn candidate 元数据非法")
        return True

    if ROBOT_SECRET_REQUIRED and not session_manager.is_registered(session_id):
        await send_error(websocket, "REGISTER_REQUIRED", "请先完成 Robot 注册认证")
        return True

    result = {
        "utterance_id": utterance_id,
        "trace_id": trace_id,
        "candidate_seq": candidate_seq,
        "speech_epoch": speech_epoch,
        "audio_watermark": audio_watermark,
        "silence_ms": silence_ms,
        "shadow": shadow,
        "committed": False,
    }
    if not (TURN_GATE_SHADOW_ENABLED or TURN_GATE_ACTIVE_ENABLED):
        result.update({"status": "disabled", "asr_text": None, "asr_time_ms": 0})
    else:
        started_at = time.monotonic()
        try:
            decoded_audio = _decode_audio_request_payload(data)
            smart_result: dict[str, Any] = {"status": "disabled"}
            if TURN_GATE_MODELS_ENABLED:
                asr_result, smart_result = await asyncio.gather(
                    process_asr(decoded_audio.audio_data, session_id),
                    asyncio.to_thread(_run_smart_turn_shadow, decoded_audio.audio_data),
                )
            else:
                asr_result = await process_asr(decoded_audio.audio_data, session_id)
            text, asr_time_ms, asr_metadata = asr_result

            context_session_id = str(data.get("context_session_id") or "").strip()
            history: list[dict[str, Any]] = []
            context_source = "empty"
            current_session = (
                session_manager.get_session(session_id)
                if hasattr(session_manager, "get_session")
                else None
            )
            if (
                context_session_id
                and current_session is not None
                and current_session.client_type == "go_voice_gateway"
            ):
                history = session_manager.get_history(context_session_id)
                context_source = "primary_python_session" if history else "primary_session_empty"

            eou_result: dict[str, Any] = {"status": "disabled"}
            if TURN_GATE_MODELS_ENABLED:
                eou_result = await asyncio.to_thread(
                    _run_livekit_eou_shadow,
                    history,
                    text or "",
                )

            smart_end = smart_result.get("end") if smart_result.get("status") == "ok" else None
            eou_end = eou_result.get("end") if eou_result.get("status") == "ok" else None
            if smart_end is None or eou_end is None:
                agreement = "incomplete"
                policy_preview = "unavailable"
            else:
                if smart_end == eou_end:
                    agreement = "both_end" if smart_end else "both_continue"
                else:
                    agreement = "disagree"
                policy_preview = decide_turn_gate(
                    smart_end=smart_end,
                    eou_end=eou_end,
                ).value
            result.update(
                {
                    "status": "observed" if text else "asr_empty",
                    "asr_text": text,
                    "asr_time_ms": asr_time_ms,
                    "asr_metadata": asr_metadata,
                    "duration_ms": decoded_audio.audio_metadata.get("duration_ms"),
                    "models_enabled": TURN_GATE_MODELS_ENABLED,
                    "smart_turn": smart_result,
                    "livekit_eou": eou_result,
                    "agreement": agreement,
                    "policy_preview": policy_preview,
                    "context_source": context_source,
                    "context_turns": len(history),
                }
            )
        except AudioValidationError as exc:
            result.update(
                {
                    "status": "audio_rejected",
                    "error_code": exc.code,
                    "error": exc.message,
                }
            )
        except Exception as exc:
            result.update({"status": "failed", "error": str(exc)})
        result["total_time_ms"] = (time.monotonic() - started_at) * 1000

    smart_log = result.get("smart_turn") if isinstance(result.get("smart_turn"), dict) else {}
    eou_log = result.get("livekit_eou") if isinstance(result.get("livekit_eou"), dict) else {}
    logger.info(
        "会话 %s: Turn candidate 完成 utterance_id=%s candidate_seq=%s shadow=%s "
        "speech_epoch=%s status=%s asr_time_ms=%.1f smart_end=%s smart_probability=%s "
        "smart_time_ms=%s eou_end=%s eou_probability=%s eou_time_ms=%s total_time_ms=%.1f "
        "agreement=%s policy_preview=%s committed=false",
        session_id,
        utterance_id,
        candidate_seq,
        shadow,
        speech_epoch,
        result.get("status"),
        float(result.get("asr_time_ms") or 0),
        smart_log.get("end"),
        smart_log.get("probability"),
        smart_log.get("total_ms"),
        eou_log.get("end"),
        eou_log.get("probability"),
        eou_log.get("total_ms"),
        float(result.get("total_time_ms") or 0),
        result.get("agreement", "-"),
        result.get("policy_preview", "-"),
    )
    await send_message(websocket, "turn_candidate_result", **result)
    await send_message(
        websocket,
        "done",
        trace_id=trace_id,
        utterance_id=utterance_id,
        candidate_seq=candidate_seq,
        shadow=shadow,
        committed=False,
    )
    return True


async def _handle_barge_in_probe_message(
    websocket: WebSocket,
    *,
    session_id: str,
    data: dict[str, Any],
    msg_type: str,
) -> bool:
    if msg_type != "barge_in_probe":
        return False

    session_manager.touch_session(session_id)
    utterance_id = str(data.get("utterance_id") or "").strip()
    trace_id = str(data.get("trace_id") or "").strip()
    round_id = str(data.get("round_id") or "").strip()
    playback_id = str(data.get("playback_id") or "").strip()
    try:
        candidate_seq = int(data.get("candidate_seq") or 0)
        speech_epoch = int(data.get("speech_epoch") or 0)
        audio_watermark = int(data.get("audio_watermark") or 0)
    except (TypeError, ValueError):
        candidate_seq = 0
        speech_epoch = 0
        audio_watermark = 0

    identity = {
        "trace_id": trace_id,
        "utterance_id": utterance_id,
        "round_id": round_id,
        "playback_id": playback_id,
        "candidate_seq": candidate_seq,
        "speech_epoch": speech_epoch,
        "audio_watermark": audio_watermark,
    }
    if not NATURAL_BARGE_IN_ENABLED:
        await send_error(websocket, "BARGE_IN_DISABLED", "自然打断 Probe 未启用")
        return True
    if (
        not utterance_id
        or not trace_id
        or not round_id
        or not playback_id
        or candidate_seq <= 0
        or speech_epoch <= 0
        or audio_watermark <= 0
    ):
        await send_error(websocket, "INVALID_BARGE_IN_PROBE", "自然打断 Probe 元数据非法")
        return True
    if ROBOT_SECRET_REQUIRED and not session_manager.is_registered(session_id):
        await send_error(websocket, "REGISTER_REQUIRED", "请先完成 Robot 注册认证")
        return True

    started_at = time.monotonic()
    text = ""
    asr_time_ms = 0.0
    asr_metadata: dict[str, Any] = {}
    try:
        decoded_audio = _decode_audio_request_payload(data)
        text, asr_time_ms, asr_metadata = await process_asr(
            decoded_audio.audio_data,
            session_id,
        )
        decision = decide_barge_in(text)
        status = "observed" if text else "asr_empty"
    except AudioValidationError as exc:
        decision = decide_barge_in(None, asr_ok=False)
        status = "audio_rejected"
        asr_metadata = {"error_code": exc.code, "error": exc.message}
    except Exception as exc:
        decision = decide_barge_in(None, asr_ok=False)
        status = "failed"
        asr_metadata = {"error": str(exc)}

    result = {
        **identity,
        "status": status,
        "decision": decision.decision,
        "reason": decision.reason,
        "asr_text": text or None,
        "asr_time_ms": asr_time_ms,
        "asr_metadata": asr_metadata,
        "total_time_ms": (time.monotonic() - started_at) * 1000,
    }
    logger.info(
        "会话 %s: Barge-in Probe 完成 utterance_id=%s candidate_seq=%s "
        "speech_epoch=%s decision=%s status=%s asr_time_ms=%.1f total_time_ms=%.1f",
        session_id,
        utterance_id,
        candidate_seq,
        speech_epoch,
        decision.decision,
        status,
        float(asr_time_ms or 0),
        result["total_time_ms"],
    )
    await send_message(websocket, "barge_in_probe_result", **result)
    await send_message(websocket, "done", **identity, committed=False)
    return True


async def _handle_queued_user_input_message(
    websocket: WebSocket,
    request_queue: asyncio.Queue,
    audio_streams: dict[str, AudioStreamAssembler],
    *,
    session_id: str,
    data: dict,
    msg_type: str,
) -> bool:
    if msg_type == "audio":
        queue_reason = "收到新的音频，保留最新一轮"
        playback_cancel_reason = "new_audio"
        send_cancel_before_enqueue = False
    elif msg_type == "text":
        queue_reason = "收到新的文本直发请求，保留最新一轮"
        playback_cancel_reason = "new_text"
        send_cancel_before_enqueue = False
    elif msg_type == "client_event":
        queue_reason = "收到新的客户端状态事件，保留最新一轮"
        playback_cancel_reason = "new_client_event"
        send_cancel_before_enqueue = True
    else:
        return False

    session_manager.touch_session(session_id)
    audio_streams.clear()
    dropped = await _queue_new_user_input(
        websocket,
        request_queue,
        data,
        session_id=session_id,
        queue_reason=queue_reason,
        playback_cancel_reason=playback_cancel_reason,
        send_cancel_before_enqueue=send_cancel_before_enqueue,
    )

    if msg_type == "client_event":
        logger.info(
            "会话 %s: 收到 client_event=%s，已入队 queue_size=%s dropped=%s",
            session_id,
            data.get("event"),
            request_queue.qsize(),
            dropped,
        )
    else:
        logger.info(
            "会话 %s: 收到 %s 消息，已入队 queue_size=%s dropped=%s",
            session_id,
            msg_type,
            request_queue.qsize(),
            dropped,
        )
    return True


def _handle_playback_report_message(session_id: str, data: dict[str, Any], msg_type: str) -> bool:
    if msg_type == "playback_complete":
        session_manager.touch_session(session_id)
        _record_client_playback_report(
            session_id,
            data,
            interrupted=False,
        )
        return True
    if msg_type == "playback_interrupted":
        session_manager.touch_session(session_id)
        round_id = str(data.get("round_id") or "").strip()
        playback_id = str(data.get("playback_id") or "").strip()
        cancelled = session_manager.cancel_round_if_matches(
            session_id,
            round_id=round_id,
            playback_id=playback_id,
        )
        if cancelled:
            logger.info(
                "会话 %s: 客户端播放中断回执已终止服务端当前轮次 round_id=%s playback_id=%s",
                session_id,
                round_id,
                playback_id,
            )
        else:
            logger.info(
                "会话 %s: 客户端播放中断回执不是当前播放，仅记录不取消 round_id=%s playback_id=%s",
                session_id,
                round_id or "-",
                playback_id or "-",
            )
        _record_client_playback_report(
            session_id,
            data,
            interrupted=True,
        )
        return True
    return False


async def _handle_interrupt_message(
    websocket: WebSocket,
    audio_streams: dict[str, AudioStreamAssembler],
    *,
    session_id: str,
    msg_type: str,
    last_interrupt_time: float,
) -> tuple[bool, float]:
    if msg_type != "interrupt":
        return False, last_interrupt_time

    audio_streams.clear()
    if not get_gateway_settings()["interrupt_enabled"]:
        logger.info(f"会话 {session_id}: interrupt 已禁用，忽略")
        return True, last_interrupt_time

    now = time.time()
    if now - last_interrupt_time < 1.0:
        logger.info(f"会话 {session_id}: 打断冷却中，忽略")
        return True, last_interrupt_time

    cancelled_round = _cancel_current_round_for_new_input(session_id)
    await cancel_complex_workflow_session(session_id, reason="client_interrupt")
    await _send_playback_cancel(
        websocket,
        session_id=session_id,
        cancelled_round=cancelled_round,
        reason="client_interrupt",
    )
    logger.info(f"会话 {session_id}: 收到打断请求")
    return True, now


def _start_queued_request_round(
    request_queue: asyncio.Queue,
    data: dict[str, Any],
    *,
    session_id: str,
    round_seq: int,
) -> dict[str, str]:
    turn_ids = _build_turn_ids(data, session_id, round_seq)
    trace_id = turn_ids["trace_id"]
    round_id = turn_ids["round_id"]
    playback_id = turn_ids["playback_id"]
    data.update(turn_ids)
    session_manager.start_round(
        session_id,
        round_id=round_id,
        playback_id=playback_id,
    )
    queue_metrics = _pop_request_queue_metrics(data, request_queue)
    trace_context = session_manager.get_trace_context(session_id)
    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="request_dequeued",
        robot_id=trace_context.get("robot_id"),
        bot_id=trace_context.get("bot_id"),
        bot_name=trace_context.get("bot_name"),
        summary=_build_request_dequeued_summary(
            data,
            round_id=round_id,
            playback_id=playback_id,
            queue_metrics=queue_metrics,
        ),
    )
    return turn_ids


async def _process_queued_request(
    websocket: WebSocket,
    *,
    session_id: str,
    data: dict[str, Any],
    trace_id: str,
    round_id: str,
    playback_id: str,
    round_seq: int,
) -> None:
    if data.get("type") == "text":
        await handle_text(
            websocket,
            session_id,
            data,
            trace_id=trace_id,
            round_id=round_id,
            playback_id=playback_id,
            round_seq=round_seq,
        )
    elif data.get("type") == "client_event":
        await handle_client_event(
            websocket,
            session_id,
            data,
            trace_id=trace_id,
            round_id=round_id,
            playback_id=playback_id,
            round_seq=round_seq,
        )
    else:
        await handle_audio(
            websocket,
            session_id,
            data,
            trace_id=trace_id,
            round_id=round_id,
            playback_id=playback_id,
            round_seq=round_seq,
        )


async def _send_register_required_if_unregistered(
    websocket: WebSocket,
    *,
    session_id: str,
    trace_id: str | None,
    round_seq: int | None,
) -> bool:
    if session_manager.is_registered(session_id):
        return False

    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="register_required",
        status="error",
        error="请先完成 Robot 注册认证",
    )
    await send_error(websocket, "REGISTER_REQUIRED", "请先完成 Robot 注册认证")
    return True


async def _resolve_turn_runtime_context(
    session_id: str,
    data: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    bot_id, bot_name = await resolve_request_bot(session_id, data)
    robot_id = session_manager.get_robot_id(session_id)
    trace.update({"robot_id": robot_id, "bot_id": bot_id, "bot_name": bot_name})

    bot_tts_settings = None
    if gateway_runtime_state and bot_id:
        bot_tts_settings = await asyncio.to_thread(gateway_runtime_state.get_bot_tts_settings, bot_id)

    return {
        "robot_id": robot_id,
        "bot_id": bot_id,
        "bot_name": bot_name,
        "bot_tts_settings": bot_tts_settings,
    }


def _extract_direct_text_content(data: dict[str, Any]) -> tuple[str | None, str | None]:
    content = str(data.get("content") or "").strip()
    if not content:
        return None, "缺少文本内容"
    if len(content) > DIRECT_TEXT_MAX_CHARS:
        return None, f"文本内容过长，最大 {DIRECT_TEXT_MAX_CHARS} 字符"
    return content, None


def _emit_process_error_trace(
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    trace: dict[str, Any],
    error: Exception,
) -> None:
    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="process_error",
        status="error",
        robot_id=trace.get("robot_id"),
        bot_id=trace.get("bot_id"),
        bot_name=trace.get("bot_name"),
        error=str(error),
    )


def _record_audio_received_trace(
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    trace: dict[str, Any],
) -> None:
    session_manager.touch_session(session_id)
    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="audio_received",
        robot_id=trace.get("robot_id"),
        bot_id=trace.get("bot_id"),
        bot_name=trace.get("bot_name"),
    )


def _warn_if_audio_truncated(session_id: str, audio_metadata: dict[str, Any]) -> None:
    if not audio_metadata.get("truncated"):
        return
    logger.warning(
        "会话 %s: 音频 %.0fms 超过限制 %.0fms，已截断为 %.0fms 后继续处理",
        session_id,
        audio_metadata.get("original_duration_ms", 0),
        audio_metadata.get("max_duration_ms", GATEWAY_MAX_AUDIO_DURATION_MS),
        audio_metadata.get("duration_ms", 0),
    )


@dataclass(frozen=True)
class DecodedAudioRequest:
    audio_data: bytes
    audio_metadata: dict[str, Any]
    audio_transport: str
    audio_encoding: str


def _build_asr_trace_summary_for_decoded_audio(
    decoded_audio: DecodedAudioRequest,
    *,
    trace_id: str | None,
    round_id: str | None,
    playback_id: str | None,
) -> dict[str, Any]:
    return _build_asr_trace_summary(
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
        audio_bytes=len(decoded_audio.audio_data),
        audio_metadata=decoded_audio.audio_metadata,
        audio_transport=decoded_audio.audio_transport,
        audio_encoding=decoded_audio.audio_encoding,
    )


def _decode_audio_request_payload(data: dict[str, Any]) -> DecodedAudioRequest:
    binary_audio = data.get("audio_bytes")
    audio_transport = data.get("audio_transport", "binary_frame")
    audio_encoding = data.get("audio_encoding", "opus")

    if not isinstance(binary_audio, (bytes, bytearray)):
        raise AudioValidationError(
            "INVALID_AUDIO_TRANSPORT",
            "客户端 audio 消息必须使用 Opus 二进制帧",
        )

    source_audio = bytes(binary_audio)
    if len(source_audio) > GATEWAY_MAX_AUDIO_RAW_BYTES:
        raise AudioValidationError(
            "AUDIO_TOO_LARGE",
            f"音频数据过大，原始大小最大 {GATEWAY_MAX_AUDIO_RAW_BYTES} bytes",
            details={
                "audio_bytes": len(source_audio),
                "max_audio_bytes": GATEWAY_MAX_AUDIO_RAW_BYTES,
            },
        )

    if audio_encoding != "opus":
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            "当前客户端二进制上行仅支持 Opus",
            details={"audio_encoding": audio_encoding},
        )

    audio_data, opus_metadata = decode_opus_audio_to_wav(
        source_audio,
        sample_rate=data.get("sample_rate"),
        channels=data.get("channels"),
        duration_ms=data.get("duration_ms"),
        opus_frame_ms=data.get("opus_frame_ms"),
    )
    audio_data, audio_metadata = truncate_wav_audio_to_limit(audio_data)
    audio_metadata.update(opus_metadata)
    for key in (
        "utterance_id",
        "stream_chunk_count",
        "stream_source_audio_bytes",
        "stream_elapsed_ms",
    ):
        if key in data:
            audio_metadata[key] = data[key]

    return DecodedAudioRequest(
        audio_data=audio_data,
        audio_metadata=audio_metadata,
        audio_transport=audio_transport,
        audio_encoding=audio_encoding,
    )


async def _send_audio_rejected_error(
    websocket: WebSocket,
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    trace: dict[str, Any],
    error: AudioValidationError,
) -> None:
    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="audio_rejected",
        status="error",
        robot_id=trace.get("robot_id"),
        bot_id=trace.get("bot_id"),
        bot_name=trace.get("bot_name"),
        error=error.message,
        summary=error.details,
    )
    await send_error(websocket, error.code, error.message)


def _record_audio_decoded_trace_and_logs(
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    round_id: str | None,
    playback_id: str | None,
    robot_id: str | None,
    bot_id: str | None,
    bot_name: str | None,
    audio_data: bytes,
    audio_metadata: dict[str, Any],
    audio_transport: str,
    audio_encoding: str,
) -> None:
    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="audio_decoded",
        robot_id=robot_id,
        bot_id=bot_id,
        bot_name=bot_name,
        summary=_build_audio_decoded_summary(
            trace_id=trace_id,
            round_id=round_id,
            playback_id=playback_id,
            audio_bytes=len(audio_data),
            audio_metadata=audio_metadata,
            audio_transport=audio_transport,
            audio_encoding=audio_encoding,
        ),
    )
    logger.info(
        "会话 %s: 收到音频 %s bytes, duration=%.0fms, bot_id=%s, bot_name=%s",
        session_id,
        len(audio_data),
        audio_metadata["duration_ms"],
        bot_id,
        bot_name,
    )
    if audio_transport == "binary_stream":
        logger.info(
            "会话 %s: 流式上行组包完成 utterance_id=%s chunks=%s packets=%s stream_elapsed=%.1fms",
            session_id,
            audio_metadata.get("utterance_id"),
            audio_metadata.get("stream_chunk_count"),
            audio_metadata.get("opus_packets"),
            audio_metadata.get("stream_elapsed_ms", 0.0),
        )


async def _send_no_valid_speech_done(
    websocket: WebSocket,
    *,
    trace_id: str | None,
    round_id: str | None,
    playback_id: str | None,
) -> None:
    await send_message(websocket, "status", message="未检测到有效语音")
    await send_message(
        websocket,
        "done",
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
    )


async def _send_no_valid_asr_result(
    websocket: WebSocket,
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    round_id: str | None,
    playback_id: str | None,
    stage: str,
    robot_id: str | None,
    bot_id: str | None,
    bot_name: str | None,
    asr_time_ms: float,
    asr_trace_summary: dict[str, Any],
    asr_metadata: dict[str, Any],
    text: str | None = None,
) -> None:
    _emit_asr_result_trace(
        trace_id=trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage=stage,
        robot_id=robot_id,
        bot_id=bot_id,
        bot_name=bot_name,
        asr_time_ms=asr_time_ms,
        asr_trace_summary=asr_trace_summary,
        asr_metadata=asr_metadata,
        text=text,
    )
    await _send_no_valid_speech_done(
        websocket,
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
    )


async def _send_asr_text_result(
    websocket: WebSocket,
    *,
    text: str,
    asr_time_ms: float,
    asr_metadata: dict[str, Any],
    trace_id: str | None,
    round_id: str | None,
    playback_id: str | None,
) -> None:
    await send_message(
        websocket,
        "text",
        content=text,
        asr_time_ms=asr_time_ms,
        asr_metadata=asr_metadata,
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
    )


async def _send_valid_asr_result(
    websocket: WebSocket,
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    round_id: str | None,
    playback_id: str | None,
    robot_id: str | None,
    bot_id: str | None,
    bot_name: str | None,
    text: str,
    asr_time_ms: float,
    asr_trace_summary: dict[str, Any],
    asr_metadata: dict[str, Any],
) -> None:
    _emit_asr_result_trace(
        trace_id=trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="asr_done",
        robot_id=robot_id,
        bot_id=bot_id,
        bot_name=bot_name,
        asr_time_ms=asr_time_ms,
        asr_trace_summary=asr_trace_summary,
        asr_metadata=asr_metadata,
        text=text,
    )
    await _send_asr_text_result(
        websocket,
        text=text,
        asr_time_ms=asr_time_ms,
        asr_metadata=asr_metadata,
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
    )


def _record_interrupted_asr_result(
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    robot_id: str | None,
    bot_id: str | None,
    bot_name: str | None,
    asr_time_ms: float,
    asr_trace_summary: dict[str, Any],
    asr_metadata: dict[str, Any],
) -> None:
    _emit_asr_result_trace(
        trace_id=trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="asr_interrupted",
        robot_id=robot_id,
        bot_id=bot_id,
        bot_name=bot_name,
        asr_time_ms=asr_time_ms,
        asr_trace_summary=asr_trace_summary,
        asr_metadata=asr_metadata,
        cancelled=True,
    )
    logger.info("会话 %s: ASR 完成后发现本轮已被新输入打断，跳过旧轮后续处理", session_id)


async def _send_asr_start_status_and_trace(
    websocket: WebSocket,
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    robot_id: str | None,
    bot_id: str | None,
    bot_name: str | None,
    asr_trace_summary: dict[str, Any],
) -> None:
    await send_message(websocket, "status", message="识别中...")
    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage="asr_start",
        robot_id=robot_id,
        bot_id=bot_id,
        bot_name=bot_name,
        summary=asr_trace_summary,
    )


async def _stream_llm_tts_for_asr_result(
    *,
    text: str,
    session_id: str,
    websocket: WebSocket,
    bot_id: str | None,
    bot_tts_settings: dict[str, Any],
    trace: dict[str, Any],
    asr_metadata: dict[str, Any],
) -> None:
    await send_message(websocket, "status", message="思考中...")
    if await _try_process_complex_workflow(
        text=text,
        session_id=session_id,
        websocket=websocket,
        bot_id=bot_id,
        bot_tts_settings=bot_tts_settings,
        trace=trace,
    ):
        return
    llm_query = build_llm_query_with_audio_context(text, asr_metadata)
    if llm_query != text:
        logger.info("会话 %s: 已为 LLM 注入语音上下文，历史仍保存干净文本", session_id)

    await process_llm_tts_stream(
        llm_query,
        session_id,
        websocket,
        bot_id,
        bot_tts_settings,
        trace=trace,
        history_query=text,
    )


async def _try_process_complex_workflow(
    *,
    text: str,
    session_id: str,
    websocket: WebSocket,
    bot_id: str | None,
    bot_tts_settings: dict[str, Any],
    trace: dict[str, Any],
) -> bool:
    coordinator = get_workflow_coordinator()
    if coordinator is None:
        return False
    request = WorkflowRunRequest(
        text=text,
        session_id=session_id,
        bot_id=bot_id or "",
        robot_id=str(trace.get("robot_id") or ""),
        trace_id=str(trace.get("trace_id") or ""),
        round_id=str(trace.get("round_id") or ""),
        playback_id=str(trace.get("playback_id") or ""),
    )
    try:
        is_complex = await coordinator.classify_complex(request)
    except Exception as exc:
        logger.warning("会话 %s: Workflow 预分类失败，回退原有 StreamChat: %s", session_id, exc)
        return False
    if not is_complex:
        return False
    if (
        session_manager.is_interrupted(session_id)
        or not session_manager.is_current_round(session_id, request.round_id)
    ):
        logger.info("会话 %s: Workflow 分类完成时轮次已失效，不再启动播放管线", session_id)
        return True

    logger.info("会话 %s: Router 命中复杂任务，进入独立 WorkflowService", session_id)
    pipeline = WorkflowTTSPipeline(
        coordinator=coordinator,
        tts_stub_provider=get_tts_stub,
        tts_timeout_sec=GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC,
    )

    async def send_control(kind: str, payload: dict[str, Any]) -> None:
        await send_message(websocket, kind, **payload)

    async def send_audio(chunk, seq: int) -> None:
        sample_rate = int(getattr(chunk, "sample_rate", 0) or DEFAULT_TTS_SAMPLE_RATE)
        duration_ms = _pcm16_duration_ms(chunk.audio_data, sample_rate)
        await send_audio_message(
            websocket,
            payload=chunk.audio_data,
            trace_id=request.trace_id,
            round_id=request.round_id,
            playback_id=request.playback_id,
            chunk_seq=seq,
            seq=seq,
            sample_rate=sample_rate,
            channels=1,
            duration_ms=round(duration_ms, 2),
        )

    try:
        result = await pipeline.run(
            request,
            bot_tts_settings=bot_tts_settings,
            send_control=send_control,
            send_audio=send_audio,
            is_cancelled=lambda: (
                session_manager.is_interrupted(session_id)
                or not session_manager.is_current_round(session_id, request.round_id)
            ),
            initial_text=pick_quick_reply("tool.complex", session_id),
        )
    except asyncio.CancelledError:
        if (
            session_manager.is_interrupted(session_id)
            or not session_manager.is_current_round(session_id, request.round_id)
        ):
            logger.info("会话 %s: 复杂任务已按客户端打断结束，保留会话处理循环", session_id)
            return True
        raise
    except Exception as exc:
        logger.error("会话 %s: 复杂任务 TTS 管线失败: %s", session_id, exc)
        await send_error(websocket, "WORKFLOW_TTS_FAILED", "复杂任务语音处理失败")
        return True

    if not result.workflow.success:
        logger.warning(
            "会话 %s: 复杂任务失败 code=%s completed_steps=%s",
            session_id,
            result.workflow.error_code,
            result.workflow.completed_steps,
        )
        if not result.done_sent:
            await send_error(
                websocket,
                result.workflow.error_code or "WORKFLOW_FAILED",
                result.workflow.message or "复杂任务处理失败",
            )
    else:
        logger.info(
            "会话 %s: 复杂任务完成 plan_id=%s steps=%s exit=%s audio_chunks=%s",
            session_id,
            result.workflow.plan_id,
            result.workflow.completed_steps,
            result.workflow.exit,
            result.audio_chunks,
        )
    session_manager.set_interrupted(session_id, False)
    return True


def _emit_asr_result_trace(
    *,
    trace_id: str | None,
    session_id: str,
    round_seq: int | None,
    stage: str,
    robot_id: str | None,
    bot_id: str | None,
    bot_name: str | None,
    asr_time_ms: float,
    asr_trace_summary: dict[str, Any],
    asr_metadata: dict[str, Any],
    text: str | None = None,
    cancelled: bool = False,
) -> None:
    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=round_seq,
        stage=stage,
        robot_id=robot_id,
        bot_id=bot_id,
        bot_name=bot_name,
        duration_ms=asr_time_ms,
        summary=_build_asr_result_summary(
            asr_trace_summary,
            asr_metadata,
            text=text,
            cancelled=cancelled,
        ),
    )


def _record_client_playback_report(
    session_id: str,
    data: dict[str, Any],
    *,
    interrupted: bool,
) -> None:
    trace_context = session_manager.get_trace_context(session_id)
    event = _build_client_playback_trace_event(
        session_id,
        data,
        interrupted=interrupted,
        trace_context=trace_context,
    )
    if event is None:
        logger.warning("会话 %s: 客户端播放回执缺少 round_id，已忽略", session_id)
        return

    summary = event["summary"]
    trace_id = event["trace_id"]
    round_id = summary["round_id"]
    playback_id = summary["playback_id"]
    reason = summary.get("reason")
    first_audio_to_playback_ms = summary.get("client_first_audio_to_playback_start_ms")
    if GATEWAY_COMPLEX_WORKFLOW_ENABLED and playback_id:
        workflow_playback_barriers.record_report(
            session_id=session_id,
            round_id=round_id,
            playback_id=playback_id,
            report_type=PLAYBACK_INTERRUPTED if interrupted else PLAYBACK_COMPLETE,
            reason=reason,
        )

    trace_recorder.emit(
        trace_id,
        session_id=session_id,
        round_seq=event["round_seq"],
        robot_id=event.get("robot_id"),
        bot_id=event.get("bot_id"),
        bot_name=event.get("bot_name"),
        stage=event["stage"],
        status="ok",
        summary=summary,
    )
    logger.info(
        "会话 %s: 客户端播放回执 %s trace_id=%s round_id=%s playback_id=%s chunks=%s samples=%s first_audio_to_playback=%.1fms%s",
        session_id,
        "interrupted" if interrupted else "completed",
        trace_id,
        round_id,
        playback_id or "-",
        summary["client_playback_chunks"],
        summary["client_playback_samples"],
        first_audio_to_playback_ms if first_audio_to_playback_ms is not None else 0.0,
        f" reason={reason}" if reason else "",
    )


# 连接计数（使用锁保证线程安全）
active_connections = 0
_connections_lock = threading.Lock()

# gRPC 连接池 - 持久化连接，避免每次请求都创建新连接
stt_channel = None
stt_stub = None
llm_channel = None
llm_stub = None
tts_channel = None
tts_stub = None

# 连接池锁（保证线程安全）
_stt_lock = threading.Lock()
_llm_lock = threading.Lock()
_tts_lock = threading.Lock()
_upstream_status_lock = threading.Lock()


_upstream_specs = build_upstream_specs(
    {
        "stt_service_url": STT_SERVICE_URL,
        "llm_service_url": LLM_SERVICE_URL,
        "tts_service_url": TTS_SERVICE_URL,
    }
)
_upstream_status = build_initial_upstream_statuses(_upstream_specs)

# 后台任务控制
_cleanup_task = None
_cleanup_running = False

gateway_runtime_state = GatewayRuntimeState(database_url=CONFIG_DATABASE_URL) if CONFIG_DATABASE_URL else None


def get_gateway_settings() -> dict:
    if gateway_runtime_state:
        return gateway_runtime_state.get_gateway_settings()
    return {
        "max_connections": MAX_CONNECTIONS,
        "max_history_length": MAX_HISTORY_LENGTH,
        "interrupt_enabled": INTERRUPT_ENABLED,
        "stt_service_url": STT_SERVICE_URL,
        "llm_service_url": LLM_SERVICE_URL,
        "tts_service_url": TTS_SERVICE_URL,
    }


def _detach_channel(service: str, reason: str) -> None:
    """Drop the shared channel reference without closing in-flight RPCs."""
    global stt_channel, stt_stub, llm_channel, llm_stub, tts_channel, tts_stub
    if service == "stt":
        had_channel = stt_channel is not None
        stt_channel = None
        stt_stub = None
    elif service == "llm":
        had_channel = llm_channel is not None
        llm_channel = None
        llm_stub = None
    elif service == "tts":
        had_channel = tts_channel is not None
        tts_channel = None
        tts_stub = None
    else:
        had_channel = False
    if had_channel:
        logger.info("Gateway upstream %s 配置变化，释放旧连接引用: %s", service, reason)


def _detach_tts_channel_for_retry(reason: str) -> None:
    """Drop the shared TTS channel reference so the next attempt opens a fresh channel."""
    global tts_channel, tts_stub
    with _tts_lock:
        had_channel = tts_channel is not None or tts_stub is not None
        tts_channel = None
        tts_stub = None
    if had_channel:
        logger.warning("Gateway upstream tts 释放旧连接引用: %s", reason)


def _is_retryable_tts_channel_error(exc: Exception) -> bool:
    if _is_locally_cancelled_grpc_error(exc):
        return False
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    try:
        grpc_code = code() if callable(code) else None
        detail_text = details() if callable(details) else str(exc)
    except Exception:
        return False
    retryable_codes = {
        grpc.StatusCode.CANCELLED,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.UNKNOWN,
    }
    if grpc_code not in retryable_codes:
        return False
    lowered = str(detail_text).lower()
    retryable_markers = (
        "channel closed",
        "socket closed",
        "transport is closing",
        "connection reset",
        "broken pipe",
        "failed to connect",
    )
    return any(marker in lowered for marker in retryable_markers)


def apply_gateway_upstreams(settings: dict) -> None:
    global _upstream_specs
    new_specs = build_upstream_specs(settings)
    old_specs = _upstream_specs
    _upstream_specs = new_specs
    for service, new_spec in new_specs.items():
        old_spec = old_specs.get(service)
        if old_spec != new_spec:
            _detach_channel(service, "Apply / Reload")
            _set_upstream_status(
                service,
                target=new_spec["url"],
                secure=new_spec["secure"],
                status="configured",
                message="配置已更新，等待重建连接",
                last_ready_at=None,
                last_error=None,
                last_error_at=None,
            )

def apply_gateway_runtime_settings() -> dict:
    settings = get_gateway_settings()
    session_manager.set_max_history(settings["max_history_length"])
    apply_gateway_upstreams(settings)
    return settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_upstream_status(service: str, **fields):
    with _upstream_status_lock:
        status = _upstream_status[service]
        status.update(fields)


def get_upstream_statuses() -> list[dict]:
    with _upstream_status_lock:
        return [dict(item) for item in _upstream_status.values()]


def _active_connection_count() -> int:
    with _connections_lock:
        return active_connections


def build_internal_status_payload(session_limit: int = 16) -> dict[str, Any]:
    """Build a cheap in-process status snapshot for Gateway operations."""
    session_limit = max(0, min(int(session_limit), 100))
    settings = get_gateway_settings()
    sessions = session_manager.list_sessions()
    runtime_status = (
        gateway_runtime_state.get_status()
        if gateway_runtime_state
        else {
            "success": False,
            "source": "env",
            "message": "Gateway 未配置 CONFIG_DATABASE_URL",
        }
    )
    return {
        "success": True,
        "status": "ok",
        "service": "Voice Gateway",
        "generated_at": _now_iso(),
        "connections": {
            "active": _active_connection_count(),
            "max": settings["max_connections"],
        },
        "sessions": {
            **session_manager.get_stats(),
            "limit": session_limit,
            "items": sessions[:session_limit],
        },
        "traces": trace_recorder.stats(),
        "upstreams": get_upstream_statuses(),
        "runtime": runtime_status,
        "settings": settings,
        "rtc": build_rtc_status(),
        "cleanup": {
            "running": _cleanup_running,
            "task_created": _cleanup_task is not None,
            "task_done": bool(_cleanup_task.done()) if _cleanup_task is not None else None,
        },
    }


def get_stt_stub():
    """获取 STT 服务 stub（复用连接，线程安全，支持健康检查和自动重连）"""
    global stt_channel, stt_stub
    spec = _upstream_specs["stt"]

    # 检查连接是否需要重建
    need_reconnect = stt_channel is None or stt_stub is None or not _is_channel_ready(stt_channel)

    if need_reconnect:
        with _stt_lock:
            # 再次检查（可能其他线程已经创建）
            need_reconnect = stt_channel is None or stt_stub is None or not _is_channel_ready(stt_channel)

            if need_reconnect:
                # 关闭旧连接
                if stt_channel is not None:
                    try:
                        stt_channel.close()
                        logger.info("关闭旧的 STT 连接")
                    except Exception as e:
                        logger.warning(f"关闭旧 STT 连接失败: {e}")

                stt_channel = _create_grpc_channel(spec["target"], secure=spec["secure"])
                logger.info(
                    "创建 STT gRPC 连接池（%s）: %s",
                    "grpcs" if spec["secure"] else "grpc",
                    spec["target"],
                )
                stt_stub = stt_service_pb2_grpc.STTServiceStub(stt_channel)
                _set_upstream_status(
                    "stt",
                    target=spec["url"],
                    secure=spec["secure"],
                    status="connecting",
                    message="连接已创建，等待 ready",
                    last_error=None,
                    last_error_at=None,
                )
    return stt_stub


def get_llm_stub():
    """获取 LLM 服务 stub（复用连接，线程安全，支持健康检查和自动重连）"""
    global llm_channel, llm_stub
    spec = _upstream_specs["llm"]

    # 检查连接是否需要重建
    need_reconnect = llm_channel is None or llm_stub is None or not _is_channel_ready(llm_channel)

    if need_reconnect:
        with _llm_lock:
            # 再次检查（可能其他线程已经创建）
            need_reconnect = llm_channel is None or llm_stub is None or not _is_channel_ready(llm_channel)

            if need_reconnect:
                # 关闭旧连接
                if llm_channel is not None:
                    try:
                        llm_channel.close()
                        logger.info("关闭旧的 LLM 连接")
                    except Exception as e:
                        logger.warning(f"关闭旧 LLM 连接失败: {e}")

                llm_channel = _create_grpc_channel(spec["target"], secure=spec["secure"])
                llm_stub = llm_service_pb2_grpc.LLMServiceStub(llm_channel)
                logger.info("创建 LLM gRPC 连接池（%s）: %s", "grpcs" if spec["secure"] else "grpc", spec["target"])
                _set_upstream_status(
                    "llm",
                    target=spec["url"],
                    secure=spec["secure"],
                    status="connecting",
                    message="连接已创建，等待 ready",
                    last_error=None,
                    last_error_at=None,
                )
    return llm_stub


def get_workflow_stub():
    """复用 LLM channel 创建独立 WorkflowService stub。"""
    get_llm_stub()
    return workflow_service_pb2_grpc.WorkflowServiceStub(llm_channel)


def get_workflow_coordinator() -> WorkflowCoordinator | None:
    global workflow_coordinator
    if not GATEWAY_COMPLEX_WORKFLOW_ENABLED:
        return None
    if workflow_coordinator is None:
        client = GrpcWorkflowClient(
            stub_provider=get_workflow_stub,
            unary_timeout_sec=GATEWAY_WORKFLOW_RPC_TIMEOUT_SEC,
            stream_timeout_sec=GATEWAY_WORKFLOW_STREAM_TIMEOUT_SEC,
        )
        workflow_coordinator = WorkflowCoordinator(
            client=client,
            playback_barriers=workflow_playback_barriers,
            playback_timeout_sec=GATEWAY_WORKFLOW_PLAYBACK_TIMEOUT_SEC,
        )
    return workflow_coordinator


async def cancel_complex_workflow_session(session_id: str, *, reason: str) -> bool:
    coordinator = get_workflow_coordinator()
    if coordinator is None:
        return False
    return await coordinator.cancel_session(session_id, reason=reason)


async def cleanup_llm_session(session_id: str) -> bool:
    """Best-effort cleanup for the LLM service's in-memory session cache."""
    if not session_id:
        return False
    if llm_stub is None:
        logger.debug("会话 %s: LLM stub 尚未创建，跳过 LLM 会话清理", session_id)
        return False

    try:
        stub = get_llm_stub()
        request = llm_service_pb2.ClearSessionRequest(session_id=session_id)
        timeout = _grpc_timeout_arg(GATEWAY_LLM_SESSION_CLEANUP_TIMEOUT_SEC)
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: stub.ClearSession(request, timeout=timeout),
        )
        logger.info(
            "会话 %s: LLM 会话清理响应 success=%s cleared=%s message=%s",
            session_id,
            response.success,
            response.cleared,
            response.message,
        )
        return bool(response.success)
    except Exception as exc:
        logger.warning("会话 %s: LLM 会话清理失败: %s", session_id, exc)
        return False


def get_tts_stub():
    """获取 TTS 服务 stub（复用连接，线程安全，支持健康检查和自动重连）"""
    global tts_channel, tts_stub
    spec = _upstream_specs["tts"]

    # 检查连接是否需要重建
    need_reconnect = tts_channel is None or tts_stub is None or not _is_channel_ready(tts_channel)

    if need_reconnect:
        with _tts_lock:
            # 再次检查（可能其他线程已经创建）
            need_reconnect = tts_channel is None or tts_stub is None or not _is_channel_ready(tts_channel)

            if need_reconnect:
                # 先创建并切换新连接，不主动关闭旧连接：旧 stub 可能仍被并发 TTS
                # 轮次使用，直接 close 会把正常的打断流程变成 Channel closed 错误。
                old_channel = tts_channel
                new_channel = _create_grpc_channel(spec["target"], secure=spec["secure"])
                new_stub = tts_service_pb2_grpc.TTSServiceStub(new_channel)
                tts_channel = new_channel
                tts_stub = new_stub
                if old_channel is not None:
                    logger.info("替换旧的 TTS 连接引用，保留在途 RPC 直至自然结束")
                logger.info("创建 TTS gRPC 连接池（%s）: %s", "grpcs" if spec["secure"] else "grpc", spec["target"])
                _set_upstream_status(
                    "tts",
                    target=spec["url"],
                    secure=spec["secure"],
                    status="connecting",
                    message="连接已创建，等待 ready",
                    last_error=None,
                    last_error_at=None,
                )
    return tts_stub


def cleanup_grpc_channels():
    """清理 gRPC 连接（带异常处理）"""
    global stt_channel, llm_channel, tts_channel

    if stt_channel:
        try:
            stt_channel.close()
            logger.info("关闭 STT gRPC 连接")
        except Exception as e:
            logger.warning(f"关闭 STT 连接失败: {e}")

    if llm_channel:
        try:
            llm_channel.close()
            logger.info("关闭 LLM gRPC 连接")
        except Exception as e:
            logger.warning(f"关闭 LLM 连接失败: {e}")

    if tts_channel:
        try:
            tts_channel.close()
            logger.info("关闭 TTS gRPC 连接")
        except Exception as e:
            logger.warning(f"关闭 TTS 连接失败: {e}")


async def _wait_channel_ready(service: str, channel, timeout_seconds: float = 3.0) -> dict:
    if channel is None:
        message = "channel 尚未创建"
        _set_upstream_status(
            service,
            status="error",
            message=message,
            last_error=message,
            last_error_at=_now_iso(),
        )
        return dict(next(item for item in get_upstream_statuses() if item["service"] == service))

    _set_upstream_status(service, status="connecting", message="等待 gRPC ready")
    try:
        await asyncio.to_thread(grpc.channel_ready_future(channel).result, timeout_seconds)
        _set_upstream_status(
            service,
            status="ready",
            message=f"ready ({_channel_connectivity_label(channel)})",
            last_ready_at=_now_iso(),
            last_error=None,
            last_error_at=None,
        )
    except Exception as exc:
        message = f"ready 超时或失败: {exc or exc.__class__.__name__}"
        _set_upstream_status(
            service,
            status="error",
            message=message,
            last_error=message,
            last_error_at=_now_iso(),
        )

    return dict(next(item for item in get_upstream_statuses() if item["service"] == service))


async def warmup_grpc_upstreams(timeout_seconds: float = 3.0) -> list[dict]:
    logger.info("开始预热 Gateway 上游 gRPC 连接...")
    get_stt_stub()
    get_llm_stub()
    get_tts_stub()
    statuses = await asyncio.gather(
        _wait_channel_ready("stt", stt_channel, timeout_seconds),
        _wait_channel_ready("llm", llm_channel, timeout_seconds),
        _wait_channel_ready("tts", tts_channel, timeout_seconds),
    )
    ready_count = sum(1 for item in statuses if item.get("status") == "ready")
    logger.info("Gateway 上游 gRPC 预热完成: ready=%s/%s", ready_count, len(statuses))
    return statuses


async def cleanup_sessions_periodically():
    """定期清理过期会话的后台任务"""
    global _cleanup_running
    _cleanup_running = True

    logger.info("会话清理后台任务已启动")

    try:
        while _cleanup_running:
            # 每60秒清理一次过期会话
            await asyncio.sleep(60)

            if _cleanup_running:
                session_manager.cleanup_expired()
                logger.debug("执行会话清理")
    except asyncio.CancelledError:
        logger.info("会话清理任务被取消")
    finally:
        logger.info("会话清理后台任务已停止")


@app.on_event("startup")
async def startup_event():
    """应用启动时初始化连接池和后台任务"""
    global _cleanup_task

    if gateway_runtime_state:
        status = gateway_runtime_state.get_status()
        settings = apply_gateway_runtime_settings()
        logger.info(
            "Gateway runtime 已加载: source=%s version=%s bot_count=%s robot_count=%s default_bot=%s max_connections=%s max_history=%s stt=%s llm=%s tts=%s",
            status["source"],
            status["config_version"],
            status["bot_count"],
            status["robot_count"],
            status["default_bot_id"],
            settings["max_connections"],
            settings["max_history_length"],
            settings["stt_service_url"],
            settings["llm_service_url"],
            settings["tts_service_url"],
        )
    await warmup_grpc_upstreams()
    if (TURN_GATE_SHADOW_ENABLED or TURN_GATE_ACTIVE_ENABLED) and TURN_GATE_MODELS_ENABLED:
        model_status = await asyncio.to_thread(_warmup_turn_gate_shadow_models)
        logger.info("Turn Gate Shadow 模型预热完成: %s", model_status)

    # 启动会话清理后台任务
    _cleanup_task = asyncio.create_task(cleanup_sessions_periodically())
    logger.info("会话清理后台任务已创建")


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时清理资源"""
    global _cleanup_running, _cleanup_task

    logger.info("停止会话清理任务...")
    _cleanup_running = False
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass

    logger.info("清理 gRPC 连接...")
    cleanup_grpc_channels()


@app.get("/")
@app.get("/healthz")
async def root():
    """健康检查"""
    return {"status": "ok", "service": "Voice Gateway"}


@app.get("/stats")
async def stats():
    """统计信息"""
    return build_stats_payload(active_connections, session_manager.get_stats())


@app.get("/internal/status")
async def gateway_internal_status(
    session_limit: int = Query(default=16, ge=0, le=100),
):
    return build_internal_status_payload(session_limit=session_limit)


@app.get("/internal/config/status")
async def gateway_config_status():
    if not gateway_runtime_state:
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "Gateway 未配置 CONFIG_DATABASE_URL"},
        )
    payload = gateway_runtime_state.get_status()
    payload["rtc"] = build_rtc_status()
    return payload


@app.post("/internal/config/reload")
async def gateway_reload_config(version: int | None = Query(default=None)):
    if not gateway_runtime_state:
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "Gateway 未配置 CONFIG_DATABASE_URL"},
        )
    try:
        payload = await gateway_runtime_state.reload(version)
        apply_gateway_runtime_settings()
        payload["upstreams"] = await warmup_grpc_upstreams()
        payload["rtc"] = build_rtc_status()
        return JSONResponse(status_code=200, content=payload)
    except Exception as exc:
        logger.exception("Gateway 重新加载配置失败: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@app.get("/internal/config/validate")
async def gateway_validate_config(version: int | None = Query(default=None)):
    if not gateway_runtime_state:
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "Gateway 未配置 CONFIG_DATABASE_URL"},
        )
    try:
        payload = await gateway_runtime_state.validate(version)
        payload["rtc"] = build_rtc_status()
        return JSONResponse(status_code=200, content=payload)
    except Exception as exc:
        logger.exception("Gateway 校验配置失败: %s", exc)
        return JSONResponse(status_code=500, content={"success": False, "message": str(exc)})


@app.get("/internal/runtime/robots")
async def gateway_runtime_robots():
    if not gateway_runtime_state:
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "Gateway 未配置 CONFIG_DATABASE_URL"},
        )
    return build_runtime_robots_payload(
        active_connections=active_connections,
        session_stats=session_manager.get_stats(),
        sessions=session_manager.list_sessions(),
        robots=gateway_runtime_state.list_runtime_robots(),
        settings=gateway_runtime_state.get_gateway_settings(),
        rtc=build_rtc_status(),
        upstreams=get_upstream_statuses(),
    )


@app.get("/internal/runtime/sessions/{session_id}")
async def gateway_runtime_session(session_id: str):
    if not gateway_runtime_state:
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": "Gateway 未配置 CONFIG_DATABASE_URL"},
        )
    session = session_manager.get_session_detail(session_id)
    if not session:
        return JSONResponse(
            status_code=404,
            content=build_runtime_session_payload(None),
        )
    return build_runtime_session_payload(session)


@app.get("/internal/runtime/traces")
async def gateway_runtime_traces(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return trace_recorder.list_rounds(limit=limit, offset=offset)


@app.get("/internal/runtime/traces/{trace_id:path}")
async def gateway_runtime_trace_detail(trace_id: str):
    payload = trace_recorder.get_round(trace_id)
    if not payload.get("success", False):
        return JSONResponse(status_code=404, content=payload)
    return payload


async def send_message(websocket: WebSocket, msg_type: str, **kwargs):
    return await _send_json_message(websocket, msg_type, logger=logger, **kwargs)


def build_rtc_config(session_id: str) -> dict[str, Any]:
    return build_gateway_rtc_settings().config_payload(session_id)


async def send_rtc_config_if_enabled(websocket: WebSocket, session_id: str) -> bool:
    if not GATEWAY_RTC_SIGNALING_ENABLED:
        return False
    config = build_rtc_config(session_id)
    await send_message(websocket, "rtc_config", **config)
    logger.info(
        "会话 %s: 已发送 rtc_config ice_servers=%s codec=%s sample_rate=%s",
        session_id,
        len(config["ice_servers"]),
        config["media"]["audio"]["codec"],
        config["media"]["audio"]["sample_rate"],
    )
    return True


async def send_audio_message(
    websocket: WebSocket,
    payload: bytes,
    *,
    opus_stream_encoder: OpusPCMStreamEncoder | None = None,
    finalize_opus_stream: bool = False,
    **kwargs,
) -> float:
    return await _send_audio_pcm_message(
        websocket,
        payload,
        header=kwargs,
        ws_send_timeout_sec=GATEWAY_WS_SEND_TIMEOUT_SEC,
        ws_audio_slow_send_ms=GATEWAY_WS_AUDIO_SLOW_SEND_MS,
        logger=logger,
        opus_stream_encoder=opus_stream_encoder,
        finalize_opus_stream=finalize_opus_stream,
    )


async def send_error(websocket: WebSocket, code: str, message: str):
    await _send_error_message(websocket, code, message, logger=logger)


async def register_robot_session(session_id: str, payload: dict) -> dict:
    return await _register_robot_session(
        session_manager=session_manager,
        runtime_state=gateway_runtime_state,
        session_id=session_id,
        payload=payload,
        robot_secret_required=ROBOT_SECRET_REQUIRED,
    )


async def resolve_request_bot(session_id: str, payload: dict) -> tuple[str | None, str | None]:
    return await _resolve_request_bot(
        session_manager=session_manager,
        runtime_state=gateway_runtime_state,
        session_id=session_id,
        payload=payload,
    )


# ============ ASR 处理 ============


async def receive_client_ws_message(websocket: WebSocket) -> dict[str, Any]:
    message = await websocket.receive()
    message_type = message.get("type")
    if message_type == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    if "text" in message and message["text"] is not None:
        return decode_ws_text_message(message["text"])
    if "bytes" in message and message["bytes"] is not None:
        return decode_client_audio_frame(message["bytes"])
    raise AudioValidationError("INVALID_WEBSOCKET_MESSAGE", "WebSocket 消息缺少 text 或 bytes")


def build_llm_query_with_audio_context(text: str, metadata: dict[str, Any]) -> str:
    return _build_llm_query_with_audio_context(
        text,
        metadata,
        include_context=STT_AUDIO_CONTEXT_TO_LLM,
    )


async def process_asr(audio_data: bytes, session_id: str) -> tuple[Optional[str], float, dict[str, Any]]:
    return await _process_asr_audio(
        audio_data,
        session_id,
        get_stt_stub=get_stt_stub,
        wav_to_pcm_fn=wav_to_pcm,
        stt_metadata_fn=_stt_metadata,
        stt_rpc_timeout_sec=GATEWAY_STT_RPC_TIMEOUT_SEC,
        audio_format=AUDIO_FORMAT,
        logger=logger,
    )


# ============ LLM + TTS 流式处理 ============


async def _await_gateway_stream_thread(
    session_id: str,
    label: str,
    thread_future,
    timeout_seconds: float,
) -> None:
    if thread_future is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(thread_future), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "会话 %s: %s 线程 %.1f 秒未结束，继续释放本轮",
            session_id,
            label,
            timeout_seconds,
        )


async def _cancel_gateway_stream_thread(
    session_id: str,
    label: str,
    thread_future,
    stop_requested,
    call_holders: list[tuple[str, dict]],
) -> None:
    if stop_requested is not None:
        stop_requested.set()
    for call_label, holder in call_holders:
        _cancel_grpc_call_holder(holder, call_label, session_id)
    if thread_future is not None and not thread_future.done():
        await _await_gateway_stream_thread(
            session_id,
            label,
            thread_future,
            LLM_TTS_THREAD_JOIN_TIMEOUT_SEC,
        )


async def process_direct_tts_stream(
    text: str,
    session_id: str,
    websocket: WebSocket,
    bot_id: Optional[str] = None,
    bot_tts_settings: Optional[dict] = None,
    trace: dict[str, Any] | None = None,
    *,
    event_type: str | None = None,
    exit_after: bool = False,
):
    """直接把系统事件话术送入 TTS，不经过 LLM/Router/历史。"""
    start_time = time.time()
    trace = trace or {}
    trace_id = trace.get("trace_id")
    round_seq = trace.get("round_seq")
    round_id = trace.get("round_id") or trace_id
    playback_id = trace.get("playback_id") or (f"{round_id}:playback" if round_id else None)
    robot_id = trace.get("robot_id")
    bot_name = trace.get("bot_name")
    thread_future = None
    stop_requested = None
    tts_call_holder = {"call": None}

    def _round_cancelled() -> bool:
        if session_manager.is_interrupted(session_id):
            return True
        return bool(round_id and not session_manager.is_current_round(session_id, round_id))

    try:
        if round_id and not session_manager.is_current_round(session_id, round_id):
            logger.info("会话 %s: 跳过已过期直接 TTS 轮次 round_id=%s", session_id, round_id)
            return

        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            stage="client_event_tts_start",
            summary=_build_client_event_tts_start_summary(
                event_type=event_type,
                text=text,
                round_id=round_id,
                playback_id=playback_id,
                exit_after=exit_after,
            ),
        )

        if round_id and playback_id:
            await send_message(
                websocket,
                "playback_start",
                trace_id=trace_id,
                round_id=round_id,
                playback_id=playback_id,
            )

        audio_chunks = 0
        audio_bytes = 0
        tts_empty_audio_chunks = 0
        audio_sample_rate = DEFAULT_TTS_SAMPLE_RATE
        grpc_recv_gap = StreamGapStats()
        ws_send_gap = StreamGapStats()
        tts_connect_ms = None
        tts_first_audio_ms = None
        ws_send_ms_total = 0.0
        ws_send_ms_max = 0.0
        ws_send_count = 0
        ws_backpressure = False
        ws_backpressure_reason = None
        ws_backpressure_send_ms = None
        ws_slow_send_strikes = 0
        close_client_for_backpressure = False

        audio_queue = asyncio.Queue(maxsize=50)
        stop_requested = threading.Event()
        thread_error: dict[str, Exception] = {}
        tts_thread_audio_chunks = 0
        loop = asyncio.get_event_loop()

        def build_tts_chunk(chunk_text: str, *, is_final: bool, first_chunk: bool):
            return _build_tts_text_chunk(
                chunk_text,
                is_final=is_final,
                session_id=session_id,
                trace_id=trace_id,
                round_id=round_id,
                playback_id=playback_id,
                bot_tts_settings=bot_tts_settings,
                include_config=first_chunk,
            )

        def iter_tts_chunks():
            yield build_tts_chunk(text, is_final=False, first_chunk=True)
            yield build_tts_chunk("", is_final=True, first_chunk=False)

        def _run_tts_attempt(attempt_index: int) -> None:
            nonlocal audio_sample_rate, tts_empty_audio_chunks, tts_connect_ms, tts_thread_audio_chunks
            tts_stub = get_tts_stub()
            tts_connect_started = time.time()
            tts_call = tts_stub.StreamTextToSpeech(
                iter_tts_chunks(),
                timeout=_grpc_timeout_arg(GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC),
            )
            tts_connect_ms = (time.time() - tts_connect_started) * 1000
            tts_call_holder["call"] = tts_call
            logger.debug(
                "会话 %s: 直接 TTS gRPC 流已创建 event=%s attempt=%s connect=%.1fms",
                session_id,
                event_type or "-",
                attempt_index + 1,
                tts_connect_ms,
            )
            for audio_chunk in tts_call:
                if stop_requested.is_set() or _round_cancelled():
                    logger.info("会话 %s: 直接 TTS 被打断，取消 gRPC 流", session_id)
                    _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                    break
                if not audio_chunk.audio_data:
                    tts_empty_audio_chunks += 1
                    continue
                now = time.time()
                sample_rate = audio_chunk.sample_rate or DEFAULT_TTS_SAMPLE_RATE
                audio_sample_rate = sample_rate
                chunk_duration_ms = _pcm16_duration_ms(audio_chunk.audio_data, sample_rate)
                grpc_recv_gap.observe(now, chunk_duration_ms)
                future = asyncio.run_coroutine_threadsafe(audio_queue.put(audio_chunk), loop)
                try:
                    future.result(timeout=5.0)
                    tts_thread_audio_chunks += 1
                except Exception as exc:
                    stop_requested.set()
                    _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                    logger.error("会话 %s: 直接 TTS 音频入队失败: %s", session_id, exc)
                    break
        def _run_tts_in_thread():
            try:
                for attempt_index in range(2):
                    try:
                        _run_tts_attempt(attempt_index)
                        if (
                            tts_thread_audio_chunks <= 0
                            and attempt_index == 0
                            and not stop_requested.is_set()
                            and not _round_cancelled()
                        ):
                            _detach_tts_channel_for_retry(
                                f"direct_tts_empty_retry event={event_type or '-'}"
                            )
                            logger.warning(
                                "会话 %s: 直接 TTS 首次返回空音频，已重建连接并重试 event=%s",
                                session_id,
                                event_type or "-",
                            )
                            continue
                        break
                    except Exception as exc:
                        if stop_requested.is_set() or _round_cancelled() or _is_locally_cancelled_grpc_error(exc):
                            logger.info("会话 %s: 直接 TTS gRPC 流已取消", session_id)
                            break
                        if tts_thread_audio_chunks <= 0 and attempt_index == 0 and _is_retryable_tts_channel_error(exc):
                            _detach_tts_channel_for_retry(
                                f"direct_tts_retry event={event_type or '-'} error={exc}"
                            )
                            logger.warning(
                                "会话 %s: 直接 TTS 首次建流失败，已重建连接并重试 event=%s error=%s",
                                session_id,
                                event_type or "-",
                                exc,
                            )
                            continue
                        thread_error["exc"] = exc
                        break
            except Exception as exc:
                if stop_requested.is_set() or _round_cancelled() or _is_locally_cancelled_grpc_error(exc):
                    logger.info("会话 %s: 直接 TTS gRPC 流已取消", session_id)
                else:
                    thread_error["exc"] = exc
            finally:
                tts_call_holder["call"] = None
                try:
                    asyncio.run_coroutine_threadsafe(audio_queue.put(None), loop).result(timeout=1.0)
                except Exception as exc:
                    logger.error("会话 %s: 直接 TTS 结束标记入队失败: %s", session_id, exc)

        thread_future = loop.run_in_executor(None, _run_tts_in_thread)
        interrupted = False
        stream_timed_out = False
        last_queue_activity = time.time()

        while True:
            if _round_cancelled():
                interrupted = True
                stop_requested.set()
                _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                while not audio_queue.empty():
                    try:
                        audio_queue.get_nowait()
                    except Exception:
                        break
                break

            try:
                audio_chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.5)
                last_queue_activity = time.time()
            except asyncio.TimeoutError:
                idle_for = time.time() - last_queue_activity
                if idle_for >= LLM_TTS_IDLE_TIMEOUT_SEC:
                    stream_timed_out = True
                    stop_requested.set()
                    _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                    logger.warning("会话 %s: 直接 TTS %.1fs 无输出，结束本轮", session_id, idle_for)
                    break
                continue

            if audio_chunk is None:
                break

            if _round_cancelled():
                interrupted = True
                stop_requested.set()
                _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                break

            now = time.time()
            sample_rate = audio_chunk.sample_rate or DEFAULT_TTS_SAMPLE_RATE
            audio_sample_rate = sample_rate
            chunk_duration_ms = _pcm16_duration_ms(audio_chunk.audio_data, sample_rate)
            ws_send_gap.observe(now, chunk_duration_ms)
            chunk_seq = audio_chunks + 1
            try:
                ws_send_ms = await send_audio_message(
                    websocket,
                    payload=audio_chunk.audio_data,
                    trace_id=trace_id,
                    round_id=round_id,
                    playback_id=playback_id,
                    chunk_seq=chunk_seq,
                    seq=chunk_seq,
                    sample_rate=sample_rate,
                    channels=1,
                    duration_ms=round(chunk_duration_ms, 2),
                )
            except WebSocketBackpressureError as exc:
                interrupted = True
                ws_backpressure = True
                ws_backpressure_reason = exc.reason
                ws_backpressure_send_ms = exc.send_time_ms
                ws_send_ms_total += exc.send_time_ms
                ws_send_ms_max = max(ws_send_ms_max, exc.send_time_ms)
                ws_send_count += 1
                record_slow_send = getattr(session_manager, "record_slow_ws_send", None)
                ws_slow_send_strikes = (
                    record_slow_send(session_id) if callable(record_slow_send) else 1
                )
                close_client_for_backpressure = (
                    ws_slow_send_strikes >= GATEWAY_WS_SLOW_SEND_MAX_STRIKES
                )
                session_manager.set_interrupted(session_id, True)
                stop_requested.set()
                _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                logger.warning(
                    "会话 %s: 直接 TTS WebSocket backpressure reason=%s elapsed=%.0fms strikes=%s/%s",
                    session_id,
                    exc.reason,
                    exc.send_time_ms,
                    ws_slow_send_strikes,
                    GATEWAY_WS_SLOW_SEND_MAX_STRIKES,
                )
                break

            ws_send_ms_total += ws_send_ms
            ws_send_ms_max = max(ws_send_ms_max, ws_send_ms)
            ws_send_count += 1
            audio_chunks += 1
            audio_bytes += len(audio_chunk.audio_data)
            if audio_chunks == 1:
                tts_first_audio_ms = (time.time() - start_time) * 1000
                logger.info(
                    "会话 %s: 直接 TTS 首个音频块 event=%s 用时 %.0fms, bytes=%s",
                    session_id,
                    event_type or "-",
                    tts_first_audio_ms,
                    len(audio_chunk.audio_data),
                )

        try:
            await _await_gateway_stream_thread(
                session_id,
                "直接 TTS",
                thread_future,
                LLM_TTS_THREAD_JOIN_TIMEOUT_SEC,
            )
        except asyncio.CancelledError:
            stop_requested.set()
            session_manager.set_interrupted(session_id, True)
            _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
            raise

        if thread_error.get("exc") and not interrupted and not stream_timed_out:
            raise thread_error["exc"]

        if not interrupted and not stream_timed_out:
            reset_slow_send = getattr(session_manager, "reset_slow_ws_send_strikes", None)
            if callable(reset_slow_send):
                reset_slow_send(session_id)

        session_manager.set_interrupted(session_id, False)

        if ws_backpressure:
            logger.warning("会话 %s: 直接 TTS 因 WebSocket backpressure 取消，不发送 done", session_id)
        elif stream_timed_out:
            await send_error(websocket, "TTS_TIMEOUT", "事件播报超时，已自动恢复")
        elif audio_chunks <= 0:
            await send_error(websocket, "TTS_EMPTY_AUDIO", "事件播报未产生音频")
        else:
            await send_message(
                websocket,
                "done",
                exit=exit_after,
                trace_id=trace_id,
                round_id=round_id,
                playback_id=playback_id,
            )

        total_time = time.time() - start_time
        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            stage="client_event_tts_done",
            status="timeout" if stream_timed_out else ("error" if ws_backpressure else "ok"),
            duration_ms=total_time * 1000,
            summary=_build_client_event_tts_summary(
                event_type=event_type,
                round_id=round_id,
                playback_id=playback_id,
                text=text,
                audio_chunks=audio_chunks,
                audio_bytes=audio_bytes,
                audio_sample_rate=audio_sample_rate,
                tts_empty_audio_chunks=tts_empty_audio_chunks,
                tts_connect_ms=tts_connect_ms,
                tts_first_audio_ms=tts_first_audio_ms,
                ws_send_ms_max=ws_send_ms_max,
                ws_send_ms_total=ws_send_ms_total,
                ws_send_count=ws_send_count,
                grpc_recv_gap_max_ms=grpc_recv_gap.max_ms,
                grpc_recv_gap_excess_max_ms=grpc_recv_gap.excess_max_ms,
                grpc_recv_gap_excess_count=grpc_recv_gap.excess_count,
                ws_send_gap_max_ms=ws_send_gap.max_ms,
                ws_send_gap_excess_max_ms=ws_send_gap.excess_max_ms,
                ws_send_gap_excess_count=ws_send_gap.excess_count,
                ws_backpressure=ws_backpressure,
                ws_backpressure_send_ms=ws_backpressure_send_ms,
                ws_backpressure_reason=ws_backpressure_reason,
                ws_slow_send_strikes=ws_slow_send_strikes,
                interrupted=interrupted,
                stream_timed_out=stream_timed_out,
                exit_after=exit_after,
            ),
        )
        logger.info(
            "会话 %s: 直接 TTS 完成 event=%s chunks=%s bytes=%s duration=%.2fs exit=%s",
            session_id,
            event_type or "-",
            audio_chunks,
            audio_bytes,
            total_time,
            exit_after,
        )

        if close_client_for_backpressure:
            try:
                await websocket.close(code=1011, reason="WebSocket audio send backpressure")
                logger.warning("会话 %s: 因直接 TTS 慢发送关闭 WebSocket", session_id)
            except Exception as exc:
                logger.warning("会话 %s: 关闭慢客户端 WebSocket 失败: %s", session_id, exc)

    except Exception as exc:
        await _cancel_gateway_stream_thread(
            session_id,
            "直接 TTS",
            thread_future,
            stop_requested,
            [("TTS", tts_call_holder)],
        )
        if _is_locally_cancelled_grpc_error(exc):
            session_manager.set_interrupted(session_id, False)
            await send_message(
                websocket,
                "done",
                exit=False,
                trace_id=trace_id,
                round_id=round_id,
                playback_id=playback_id,
            )
            return
        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            stage="client_event_tts_error",
            status="error",
            error=str(exc),
            summary=_build_client_event_tts_error_summary(
                event_type=event_type,
                round_id=round_id,
                playback_id=playback_id,
            ),
        )
        logger.error("会话 %s: 直接 TTS 处理失败 - %s", session_id, exc)
        await send_error(websocket, "TTS_FAILED", str(exc))


async def process_llm_tts_stream(
    query: str,
    session_id: str,
    websocket: WebSocket,
    bot_id: Optional[str] = None,
    bot_tts_settings: Optional[dict] = None,
    trace: dict[str, Any] | None = None,
    history_query: Optional[str] = None,
):
    """
    处理 LLM + TTS 流式响应

    LLM 流式输出 → TTS 流式合成 → WebSocket 流式返回
    """
    thread_future = None
    stop_requested = None
    llm_call_holder = {"call": None}
    tts_call_holder = {"call": None}
    try:
        start_time = time.time()
        trace = trace or {}
        trace_id = trace.get("trace_id")
        round_seq = trace.get("round_seq")
        round_id = trace.get("round_id") or trace_id
        playback_id = trace.get("playback_id") or (f"{round_id}:playback" if round_id else None)
        robot_id = trace.get("robot_id")
        bot_name = trace.get("bot_name")

        def _round_cancelled() -> bool:
            if session_manager.is_interrupted(session_id):
                return True
            return bool(round_id and not session_manager.is_current_round(session_id, round_id))

        def _is_expected_stream_cancellation(exc: Exception) -> bool:
            if _is_locally_cancelled_grpc_error(exc):
                return True
            cancellation_requested = bool(
                (stop_requested is not None and stop_requested.is_set())
                or _round_cancelled()
            )
            return cancellation_requested and _is_cancelled_grpc_error(exc)

        if round_id and not session_manager.is_current_round(session_id, round_id):
            logger.info("会话 %s: 跳过已过期 LLM/TTS 轮次 round_id=%s", session_id, round_id)
            trace_recorder.emit(
                trace_id,
                session_id=session_id,
                round_seq=round_seq,
                robot_id=robot_id,
                bot_id=bot_id,
                bot_name=bot_name,
                stage="llm_tts_stale_round",
                summary=_build_llm_tts_stale_round_summary(
                    round_id=round_id,
                    playback_id=playback_id,
                ),
            )
            return

        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            stage="llm_tts_start",
            summary=_build_llm_tts_start_summary(
                query=query,
                round_id=round_id,
                playback_id=playback_id,
            ),
        )

        if round_id and playback_id:
            await send_message(
                websocket,
                "playback_start",
                trace_id=trace_id,
                round_id=round_id,
                playback_id=playback_id,
            )

        # 使用连接池获取 stub（复用连接）
        llm_stub = get_llm_stub()
        tts_stub = get_tts_stub()

        # 构建 LLM 请求
        llm_request = llm_service_pb2.ChatRequest(
            text=query,
            session_id=session_id,
            bot_id=bot_id or "",
            robot_id=robot_id or "",
            trace_id=trace_id or "",
        )

        logger.info(f"会话 {session_id}: 开始 LLM 流式处理")

        # LLM 流式输出 → TTS 流式合成（异步处理，避免阻塞事件循环）
        llm_text = ""
        llm_text_for_history = ""
        audio_chunks = 0
        audio_bytes = 0
        tts_empty_audio_chunks = 0
        audio_sample_rate = DEFAULT_TTS_SAMPLE_RATE
        should_exit = False  # 是否检测到 [EXIT] 退出标记
        selected_singing_asset_id = None
        grpc_recv_gap = StreamGapStats()
        ws_send_gap = StreamGapStats()
        llm_first_token_ms = None
        llm_total_ms = None
        llm_internal_metrics = {}
        tts_connect_ms = None
        tts_first_commit_ms = None
        tts_first_audio_ms = None
        ws_send_ms_total = 0.0
        ws_send_ms_max = 0.0
        ws_send_count = 0
        ws_backpressure = False
        ws_backpressure_reason = None
        ws_backpressure_send_ms = None
        ws_slow_send_strikes = 0
        close_client_for_backpressure = False

        # 使用队列在线程和协程之间传递数据（增大队列避免 TTS 暂停）
        text_queue = asyncio.Queue(maxsize=50)
        stop_requested = threading.Event()

        # 获取当前事件循环（在启动线程前）
        loop = asyncio.get_event_loop()

        # 在线程池中运行 LLM + TTS 流式处理
        def _run_llm_tts_in_thread():
            """在线程中运行 LLM → TTS 流式处理"""
            nonlocal llm_text, llm_text_for_history, should_exit, selected_singing_asset_id
            nonlocal llm_internal_metrics
            nonlocal audio_sample_rate, tts_empty_audio_chunks
            nonlocal llm_first_token_ms, llm_total_ms, tts_connect_ms, tts_first_commit_ms

            def generate_text_chunks():
                """生成文本块，实时发送给 TTS（过滤 [EXIT] 标记）"""
                nonlocal llm_text, llm_text_for_history, should_exit, selected_singing_asset_id
                nonlocal llm_first_token_ms, llm_total_ms, tts_first_commit_ms
                # 缓冲区：用于检测可能跨 chunk 的 [EXIT] 标记
                buffer = ""
                first_tts_chunk = True
                first_tts_text_logged = False

                def build_tts_chunk(text: str, *, is_final: bool):
                    nonlocal first_tts_chunk, first_tts_text_logged, llm_text_for_history
                    nonlocal tts_first_commit_ms
                    if text and not is_final:
                        llm_text_for_history += text
                        if tts_first_commit_ms is None:
                            tts_first_commit_ms = (time.time() - start_time) * 1000
                            trace_recorder.emit(
                                trace_id,
                                session_id=session_id,
                                round_seq=round_seq,
                                robot_id=robot_id,
                                bot_id=bot_id,
                                bot_name=bot_name,
                                stage="tts_first_commit",
                                duration_ms=tts_first_commit_ms,
                                summary=_build_tts_first_commit_summary(
                                    round_id=round_id,
                                    playback_id=playback_id,
                                    tts_first_commit_ms=tts_first_commit_ms,
                                    text_chars=len(text),
                                ),
                            )
                        if not first_tts_text_logged:
                            first_tts_text_logged = True
                            logger.info(
                                "会话 %s: 首段文本送入 TTS，用时 %.0fms - %s",
                                session_id,
                                (time.time() - start_time) * 1000,
                                text[:80],
                            )
                    chunk = _build_tts_text_chunk(
                        text,
                        is_final=is_final,
                        session_id=session_id,
                        trace_id=trace_id,
                        round_id=round_id,
                        playback_id=playback_id,
                        bot_tts_settings=bot_tts_settings,
                        include_config=first_tts_chunk,
                    )
                    first_tts_chunk = False
                    return chunk

                llm_call = llm_stub.StreamChat(
                    llm_request,
                    timeout=_grpc_timeout_arg(GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC),
                )
                llm_call_holder["call"] = llm_call
                try:
                    for chunk in llm_call:
                        if stop_requested.is_set():
                            logger.info(f"会话 {session_id}: LLM 流收到停止信号")
                            _cancel_grpc_call_holder(llm_call_holder, "LLM", session_id)
                            break
                        # 检查打断标志
                        if _round_cancelled():
                            logger.info(f"会话 {session_id}: LLM 被打断")
                            _cancel_grpc_call_holder(llm_call_holder, "LLM", session_id)
                            break
                        parsed_metrics = _parse_llm_metrics_json(getattr(chunk, "metrics_json", ""))
                        if parsed_metrics:
                            llm_internal_metrics = parsed_metrics
                            trace_recorder.emit(
                                trace_id,
                                session_id=session_id,
                                round_seq=round_seq,
                                robot_id=robot_id,
                                bot_id=bot_id,
                                bot_name=bot_name,
                                stage="llm_internal_metrics",
                                summary=_build_llm_internal_metrics_summary(
                                    round_id=round_id,
                                    playback_id=playback_id,
                                    metrics=parsed_metrics,
                                ),
                            )
                            logger.info(
                                "会话 %s: LLM 内部指标 router=%s/%s router_ms=%s first_text_ms=%s response_chars=%s mcp_prepare_ms=%s service_total_ms=%s",
                                session_id,
                                parsed_metrics.get("llm_router_kind", "<none>"),
                                parsed_metrics.get("llm_router_source", "<none>"),
                                parsed_metrics.get("llm_router_ms", 0),
                                parsed_metrics.get("llm_first_text_ms", 0),
                                parsed_metrics.get("llm_response_chars", 0),
                                parsed_metrics.get("llm_mcp_prepare_ms", 0),
                                parsed_metrics.get("llm_service_total_ms", 0),
                            )
                        if chunk.text:
                            if llm_first_token_ms is None:
                                llm_first_token_ms = (time.time() - start_time) * 1000
                                trace_recorder.emit(
                                    trace_id,
                                    session_id=session_id,
                                    round_seq=round_seq,
                                    robot_id=robot_id,
                                    bot_id=bot_id,
                                    bot_name=bot_name,
                                    stage="llm_first_token",
                                    duration_ms=llm_first_token_ms,
                                    summary=_build_llm_first_token_summary(
                                        round_id=round_id,
                                        playback_id=playback_id,
                                        llm_first_token_ms=llm_first_token_ms,
                                        chunk_chars=len(chunk.text),
                                    ),
                                )
                            llm_text += chunk.text
                            buffer += chunk.text

                            safe_text, buffer, found_exit, found_singing_asset_id = _split_control_safe_buffer(buffer)
                            if found_exit:
                                should_exit = True
                                logger.info(f"会话 {session_id}: 检测到 [EXIT] 标记，已过滤并设置退出")
                            if found_singing_asset_id:
                                if selected_singing_asset_id and selected_singing_asset_id != found_singing_asset_id:
                                    raise ValueError("同一轮只允许选择一首歌曲")
                                selected_singing_asset_id = found_singing_asset_id
                                logger.info(
                                    "会话 %s: 收到歌曲播放标记 asset_id=%s",
                                    session_id,
                                    selected_singing_asset_id,
                                )
                            safe_text = _strip_standalone_tool_tags(safe_text)
                            if safe_text:
                                yield build_tts_chunk(safe_text, is_final=False)
                except Exception as exc:
                    if stop_requested.is_set() or _round_cancelled() or _is_expected_stream_cancellation(exc):
                        logger.info("会话 %s: LLM gRPC 流已取消，TTS 请求生成器退出", session_id)
                        return
                    raise
                finally:
                    llm_call_holder["call"] = None

                # 被打断时不发送剩余内容和结束标记
                if stop_requested.is_set() or _round_cancelled():
                    logger.info(f"会话 {session_id}: LLM 生成器因打断退出")
                    return

                # 处理缓冲区剩余内容
                if buffer:
                    safe_text, pending, found_exit, found_singing_asset_id = _split_control_safe_buffer(buffer)
                    if found_exit:
                        should_exit = True
                        logger.info(f"会话 {session_id}: 检测到 [EXIT] 标记，已过滤并设置退出")
                    if found_singing_asset_id:
                        if selected_singing_asset_id and selected_singing_asset_id != found_singing_asset_id:
                            raise ValueError("同一轮只允许选择一首歌曲")
                        selected_singing_asset_id = found_singing_asset_id
                    if pending:
                        raise ValueError("LLM 歌曲控制标记不完整")
                    safe_text = _strip_standalone_tool_tags(safe_text)
                    if safe_text:
                        yield build_tts_chunk(safe_text, is_final=False)

                # 发送结束标记
                yield build_tts_chunk("", is_final=True)

                llm_total_ms = (time.time() - start_time) * 1000
                trace_recorder.emit(
                    trace_id,
                    session_id=session_id,
                    round_seq=round_seq,
                    robot_id=robot_id,
                    bot_id=bot_id,
                    bot_name=bot_name,
                    stage="llm_done",
                    duration_ms=llm_total_ms,
                    summary=_build_llm_done_summary(
                        round_id=round_id,
                        playback_id=playback_id,
                        llm_total_ms=llm_total_ms,
                        text=llm_text_for_history,
                        should_exit=should_exit,
                    ),
                )
                logger.info(
                    "会话 %s: LLM 完成 (%s字) - %s",
                    session_id,
                    len(llm_text_for_history),
                    llm_text_for_history[:200],
                )

            # TTS 流式合成，将结果放入队列
            tts_call = None
            try:
                tts_connect_started = time.time()
                tts_call = tts_stub.StreamTextToSpeech(
                    generate_text_chunks(),
                    timeout=_grpc_timeout_arg(GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC),
                )
                tts_connect_ms = (time.time() - tts_connect_started) * 1000
                trace_recorder.emit(
                    trace_id,
                    session_id=session_id,
                    round_seq=round_seq,
                    robot_id=robot_id,
                    bot_id=bot_id,
                    bot_name=bot_name,
                    stage="tts_connected",
                    duration_ms=tts_connect_ms,
                    summary=_build_tts_connected_summary(
                        round_id=round_id,
                        playback_id=playback_id,
                        tts_connect_ms=tts_connect_ms,
                    ),
                )
                tts_call_holder["call"] = tts_call
                for audio_chunk in tts_call:
                    # 检查打断标志
                    if stop_requested.is_set() or _round_cancelled():
                        logger.info(f"会话 {session_id}: TTS 被打断，取消 gRPC 流")
                        _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                        break
                    if not audio_chunk.audio_data:
                        tts_empty_audio_chunks += 1
                        logger.debug(
                            "会话 %s: 跳过空 TTS 音频块 is_final=%s",
                            session_id,
                            getattr(audio_chunk, "is_final", False),
                        )
                        continue
                    now = time.time()
                    sample_rate = audio_chunk.sample_rate or DEFAULT_TTS_SAMPLE_RATE
                    audio_sample_rate = sample_rate
                    chunk_duration_ms = _pcm16_duration_ms(audio_chunk.audio_data, sample_rate)
                    grpc_recv_gap.observe(now, chunk_duration_ms)
                    future = asyncio.run_coroutine_threadsafe(text_queue.put(audio_chunk), loop)
                    try:
                        future.result(timeout=5.0)
                    except Exception as e:
                        stop_requested.set()
                        _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                        logger.error(f"放入队列失败: {e}")
                        break
            except Exception as exc:
                if _is_expected_stream_cancellation(exc):
                    logger.info(
                        "会话 %s: TTS gRPC 流已按打断请求取消: %s",
                        session_id,
                        exc,
                    )
                    return
                raise
            finally:
                tts_call_holder["call"] = None
                try:
                    asyncio.run_coroutine_threadsafe(text_queue.put(None), loop).result(timeout=1.0)
                except Exception as e:
                    logger.error(f"发送结束标记失败: {e}")

        # 启动线程处理 LLM + TTS
        thread_future = loop.run_in_executor(None, _run_llm_tts_in_thread)

        # 从队列中读取音频并发送
        interrupted = False
        stream_timed_out = False
        last_queue_activity = time.time()
        while True:
            if _round_cancelled():
                interrupted = True
                logger.info(f"会话 {session_id}: 检测到打断，主动取消 LLM/TTS gRPC 流")
                stop_requested.set()
                _cancel_grpc_call_holder(llm_call_holder, "LLM", session_id)
                _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                # 清空队列，快速退出
                while not text_queue.empty():
                    try:
                        text_queue.get_nowait()
                    except Exception:
                        break
                break

            try:
                audio_chunk = await asyncio.wait_for(text_queue.get(), timeout=0.5)
                last_queue_activity = time.time()
            except asyncio.TimeoutError:
                idle_for = time.time() - last_queue_activity
                if idle_for >= LLM_TTS_IDLE_TIMEOUT_SEC:
                    stream_timed_out = True
                    logger.warning(
                        f"会话 {session_id}: LLM/TTS 流 {idle_for:.1f}s 无输出，强制结束本轮"
                    )
                    stop_requested.set()
                    _cancel_grpc_call_holder(llm_call_holder, "LLM", session_id)
                    _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                    break
                continue

            if audio_chunk is None:
                break

            # 检查打断标志
            if _round_cancelled():
                interrupted = True
                stop_requested.set()
                _cancel_grpc_call_holder(llm_call_holder, "LLM", session_id)
                _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                # 清空队列，快速退出
                while not text_queue.empty():
                    try:
                        text_queue.get_nowait()
                    except Exception:
                        break
                break

            now = time.time()
            sample_rate = audio_chunk.sample_rate or DEFAULT_TTS_SAMPLE_RATE
            audio_sample_rate = sample_rate
            chunk_duration_ms = _pcm16_duration_ms(audio_chunk.audio_data, sample_rate)
            ws_send_gap.observe(now, chunk_duration_ms)
            chunk_seq = audio_chunks + 1
            try:
                ws_send_ms = await send_audio_message(
                    websocket,
                    payload=audio_chunk.audio_data,
                    trace_id=trace_id,
                    round_id=round_id,
                    playback_id=playback_id,
                    chunk_seq=chunk_seq,
                    seq=chunk_seq,
                    sample_rate=sample_rate,
                    channels=1,
                    duration_ms=round(chunk_duration_ms, 2),
                )
            except WebSocketBackpressureError as exc:
                interrupted = True
                ws_backpressure = True
                ws_backpressure_reason = exc.reason
                ws_backpressure_send_ms = exc.send_time_ms
                ws_send_ms_total += exc.send_time_ms
                ws_send_ms_max = max(ws_send_ms_max, exc.send_time_ms)
                ws_send_count += 1
                if exc.reason == "ws_slow_send":
                    audio_chunks += 1
                    audio_bytes += len(audio_chunk.audio_data)
                    if tts_first_audio_ms is None:
                        tts_first_audio_ms = (time.time() - start_time) * 1000
                record_slow_send = getattr(session_manager, "record_slow_ws_send", None)
                ws_slow_send_strikes = (
                    record_slow_send(session_id) if callable(record_slow_send) else 1
                )
                close_client_for_backpressure = (
                    ws_slow_send_strikes >= GATEWAY_WS_SLOW_SEND_MAX_STRIKES
                )
                session_manager.set_interrupted(session_id, True)
                stop_requested.set()
                _cancel_grpc_call_holder(llm_call_holder, "LLM", session_id)
                _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
                while not text_queue.empty():
                    try:
                        text_queue.get_nowait()
                    except Exception:
                        break
                trace_recorder.emit(
                    trace_id,
                    session_id=session_id,
                    round_seq=round_seq,
                    robot_id=robot_id,
                    bot_id=bot_id,
                    bot_name=bot_name,
                    stage="ws_backpressure",
                    status="error",
                    duration_ms=exc.send_time_ms,
                    summary=_build_ws_backpressure_summary(
                        round_id=round_id,
                        playback_id=playback_id,
                        reason=exc.reason,
                        send_time_ms=exc.send_time_ms,
                        ws_slow_send_strikes=ws_slow_send_strikes,
                        close_client=close_client_for_backpressure,
                    ),
                )
                logger.warning(
                    "会话 %s: WebSocket 音频发送触发 backpressure reason=%s elapsed=%.0fms strikes=%s/%s",
                    session_id,
                    exc.reason,
                    exc.send_time_ms,
                    ws_slow_send_strikes,
                    GATEWAY_WS_SLOW_SEND_MAX_STRIKES,
                )
                break
            ws_send_ms_total += ws_send_ms
            ws_send_ms_max = max(ws_send_ms_max, ws_send_ms)
            ws_send_count += 1
            audio_chunks += 1
            audio_bytes += len(audio_chunk.audio_data)
            if audio_chunks == 1:
                tts_first_audio_ms = (time.time() - start_time) * 1000
                gateway_first_audio_epoch_ms = time.time() * 1000.0
                tts_first_pcm_epoch_ms = float(getattr(audio_chunk, "tts_first_pcm_epoch_ms", 0.0) or 0.0)
                tts_gateway_after_server_pcm_ms = (
                    max(0.0, gateway_first_audio_epoch_ms - tts_first_pcm_epoch_ms)
                    if tts_first_pcm_epoch_ms
                    else None
                )
                tts_internal_first_pcm_ms = float(
                    getattr(audio_chunk, "tts_internal_first_pcm_ms", 0.0) or 0.0
                )
                tts_first_text_to_first_pcm_ms = float(
                    getattr(audio_chunk, "tts_first_text_to_first_pcm_ms", 0.0) or 0.0
                )
                tts_request_to_grpc_yield_ms = float(
                    getattr(audio_chunk, "tts_request_to_grpc_yield_ms", 0.0) or 0.0
                )
                logger.info(
                    "会话 %s: 收到首个 TTS 音频块，用时 %.0fms, bytes=%s, tts_internal_first_pcm=%.1fms, first_text_to_pcm=%.1fms, gateway_after_server_pcm=%.1fms",
                    session_id,
                    tts_first_audio_ms,
                    len(audio_chunk.audio_data),
                    tts_internal_first_pcm_ms,
                    tts_first_text_to_first_pcm_ms,
                    tts_gateway_after_server_pcm_ms or 0.0,
                )
                trace_recorder.emit(
                    trace_id,
                    session_id=session_id,
                    round_seq=round_seq,
                    robot_id=robot_id,
                    bot_id=bot_id,
                    bot_name=bot_name,
                    stage="tts_first_audio",
                    duration_ms=tts_first_audio_ms,
                    summary=_build_tts_first_audio_summary(
                        round_id=round_id,
                        playback_id=playback_id,
                        tts_first_audio_ms=tts_first_audio_ms,
                        chunk_bytes=len(audio_chunk.audio_data),
                        sample_rate=sample_rate,
                        ws_send_ms=ws_send_ms,
                        tts_internal_first_pcm_ms=tts_internal_first_pcm_ms,
                        tts_first_text_to_first_pcm_ms=tts_first_text_to_first_pcm_ms,
                        tts_gateway_after_server_pcm_ms=tts_gateway_after_server_pcm_ms,
                        tts_request_to_grpc_yield_ms=tts_request_to_grpc_yield_ms,
                        tts_trace_id=getattr(audio_chunk, "trace_id", "") or "",
                        tts_round_id=getattr(audio_chunk, "round_id", "") or "",
                        tts_playback_id=getattr(audio_chunk, "playback_id", "") or "",
                    ),
                )

        # 等待线程结束（避免线程泄漏）
        try:
            await _await_gateway_stream_thread(
                session_id,
                "LLM+TTS",
                thread_future,
                LLM_TTS_THREAD_JOIN_TIMEOUT_SEC,
            )
        except asyncio.CancelledError:
            stop_requested.set()
            session_manager.set_interrupted(session_id, True)
            _cancel_grpc_call_holder(llm_call_holder, "LLM", session_id)
            _cancel_grpc_call_holder(tts_call_holder, "TTS", session_id)
            raise

        if selected_singing_asset_id and not interrupted and not stream_timed_out and not ws_backpressure:
            song_audio = await loop.run_in_executor(None, _load_singing_audio, selected_singing_asset_id)
            logger.info(
                "会话 %s: 开始歌曲播放 asset_id=%s title=%s duration=%.2fs opus_encoder=continuous",
                session_id,
                song_audio.asset_id,
                song_audio.title,
                song_audio.duration_seconds,
            )
            trace_recorder.emit(
                trace_id,
                session_id=session_id,
                round_seq=round_seq,
                robot_id=robot_id,
                bot_id=bot_id,
                bot_name=bot_name,
                stage="singing_playback_start",
                summary={
                    "round_id": round_id,
                    "playback_id": playback_id,
                    "asset_id": song_audio.asset_id,
                    "voice_id": song_audio.voice_id,
                    "song_id": song_audio.song_id,
                    "title": song_audio.title,
                    "duration_seconds": round(song_audio.duration_seconds, 3),
                    "opus_encoder": "continuous",
                },
            )
            song_started = loop.time()
            song_sent_seconds = 0.0
            song_opus_encoder = OpusPCMStreamEncoder(
                sample_rate=song_audio.sample_rate,
                channels=song_audio.channels,
            )
            for payload in song_audio.chunks(chunk_ms=100):
                if _round_cancelled():
                    interrupted = True
                    logger.info(
                        "会话 %s: 歌曲播放被打断 song_id=%s sent=%.2fs",
                        session_id,
                        song_audio.song_id,
                        song_sent_seconds,
                    )
                    break
                chunk_duration_ms = _pcm16_duration_ms(payload, song_audio.sample_rate)
                finalize_opus_stream = (
                    song_sent_seconds + chunk_duration_ms / 1000.0
                    >= song_audio.duration_seconds - 1e-6
                )
                chunk_seq = audio_chunks + 1
                try:
                    ws_send_ms = await send_audio_message(
                        websocket,
                        payload=payload,
                        trace_id=trace_id,
                        round_id=round_id,
                        playback_id=playback_id,
                        chunk_seq=chunk_seq,
                        seq=chunk_seq,
                        sample_rate=song_audio.sample_rate,
                        channels=song_audio.channels,
                        duration_ms=round(chunk_duration_ms, 2),
                        opus_stream_encoder=song_opus_encoder,
                        finalize_opus_stream=finalize_opus_stream,
                    )
                except WebSocketBackpressureError as exc:
                    interrupted = True
                    ws_backpressure = True
                    ws_backpressure_reason = exc.reason
                    ws_backpressure_send_ms = exc.send_time_ms
                    ws_send_ms_total += exc.send_time_ms
                    ws_send_ms_max = max(ws_send_ms_max, exc.send_time_ms)
                    ws_send_count += 1
                    session_manager.set_interrupted(session_id, True)
                    logger.warning(
                        "会话 %s: 歌曲发送触发 backpressure reason=%s elapsed=%.0fms",
                        session_id,
                        exc.reason,
                        exc.send_time_ms,
                    )
                    break
                now = time.time()
                ws_send_gap.observe(now, chunk_duration_ms)
                ws_send_ms_total += ws_send_ms
                ws_send_ms_max = max(ws_send_ms_max, ws_send_ms)
                ws_send_count += 1
                audio_chunks += 1
                audio_bytes += len(payload)
                audio_sample_rate = song_audio.sample_rate
                song_sent_seconds += chunk_duration_ms / 1000.0
                # 最多只在 Go 侧预送约 300ms，保证歌曲播放期间仍能快速打断。
                target_elapsed = max(0.0, song_sent_seconds - 0.3)
                sleep_seconds = target_elapsed - (loop.time() - song_started)
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)

            trace_recorder.emit(
                trace_id,
                session_id=session_id,
                round_seq=round_seq,
                robot_id=robot_id,
                bot_id=bot_id,
                bot_name=bot_name,
                stage="singing_playback_interrupted" if interrupted else "singing_playback_done",
                status="error" if ws_backpressure else "ok",
                summary={
                    "round_id": round_id,
                    "playback_id": playback_id,
                    "asset_id": song_audio.asset_id,
                    "voice_id": song_audio.voice_id,
                    "song_id": song_audio.song_id,
                    "title": song_audio.title,
                    "sent_seconds": round(song_sent_seconds, 3),
                },
            )

        # TTS 线程可能在主协程下一次轮询前就观察到打断并退出；此处再次
        # 读取轮次状态，避免把已取消且零音频的旧轮次误记为正常完成。
        if _round_cancelled():
            interrupted = True

        if not interrupted and not stream_timed_out:
            reset_slow_send = getattr(session_manager, "reset_slow_ws_send_strikes", None)
            if callable(reset_slow_send):
                reset_slow_send(session_id)

        # 重置打断标志
        session_manager.set_interrupted(session_id, False)

        # 保存到历史（即使被打断也保存已生成的部分）；不要把语音上下文前缀写入历史。
        session_manager.add_message(session_id, "user", history_query or query)
        if llm_text_for_history:
            session_manager.add_message(session_id, "assistant", llm_text_for_history)

        # 发送完成消息（带退出标记）
        if ws_backpressure:
            logger.warning(
                "会话 %s: 本轮因 WebSocket backpressure 取消，不再发送 done/error",
                session_id,
            )
        elif stream_timed_out:
            await send_error(websocket, "LLM_TTS_TIMEOUT", "本轮响应超时，已自动恢复")
        else:
            logger.info(f"会话 {session_id}: 发送 done exit={should_exit}")
            await send_message(
                websocket,
                "done",
                exit=should_exit,
                trace_id=trace_id,
                round_id=round_id,
                playback_id=playback_id,
            )

        audio_duration_sec = _audio_duration_seconds(audio_bytes, audio_sample_rate)
        logger.info(
            f"会话 {session_id}: Gateway 节奏统计 - "
            f"grpc={audio_chunks}块/{audio_bytes}B/{audio_duration_sec:.2f}s, "
            f"grpc_gap_max={grpc_recv_gap.max_ms:.1f}ms, "
            f"grpc_gap_excess_max={grpc_recv_gap.excess_max_ms:.1f}ms, "
            f"grpc_gap_excess_count={grpc_recv_gap.excess_count}, "
            f"ws_gap_max={ws_send_gap.max_ms:.1f}ms, "
            f"ws_gap_excess_max={ws_send_gap.excess_max_ms:.1f}ms, "
            f"ws_gap_excess_count={ws_send_gap.excess_count}"
        )

        total_time = time.time() - start_time
        final_status = "timeout" if stream_timed_out else "ok"
        final_stage = "llm_tts_timeout" if stream_timed_out else "llm_tts_done"
        if ws_backpressure:
            final_status = "error"
            final_stage = "llm_tts_backpressure"
        elif interrupted:
            final_stage = "llm_tts_interrupted"
        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            stage=final_stage,
            status=final_status,
            duration_ms=total_time * 1000,
            summary=_build_llm_tts_summary(
                round_id=round_id,
                playback_id=playback_id,
                audio_chunks=audio_chunks,
                audio_bytes=audio_bytes,
                audio_sample_rate=audio_sample_rate,
                tts_empty_audio_chunks=tts_empty_audio_chunks,
                llm_first_token_ms=llm_first_token_ms,
                llm_total_ms=llm_total_ms,
                llm_internal_metrics=llm_internal_metrics,
                tts_connect_ms=tts_connect_ms,
                tts_first_commit_ms=tts_first_commit_ms,
                tts_first_audio_ms=tts_first_audio_ms,
                ws_send_ms_max=ws_send_ms_max,
                ws_send_ms_total=ws_send_ms_total,
                ws_send_count=ws_send_count,
                ws_backpressure=ws_backpressure,
                ws_backpressure_send_ms=ws_backpressure_send_ms,
                ws_backpressure_reason=ws_backpressure_reason,
                ws_slow_send_strikes=ws_slow_send_strikes,
                interrupted=interrupted,
                stream_timed_out=stream_timed_out,
                should_exit=should_exit,
            ),
        )
        if close_client_for_backpressure:
            try:
                await websocket.close(code=1011, reason="WebSocket audio send backpressure")
                logger.warning("会话 %s: 因连续慢发送关闭 WebSocket", session_id)
            except Exception as exc:
                logger.warning("会话 %s: 关闭慢客户端 WebSocket 失败: %s", session_id, exc)

        if ws_backpressure:
            logger.info(
                f"会话 {session_id}: WebSocket backpressure 取消 - 音频块: {audio_chunks}, 耗时: {total_time:.2f}s"
            )
        elif stream_timed_out:
            logger.info(f"会话 {session_id}: 超时恢复 - 音频块: {audio_chunks}, 耗时: {total_time:.2f}s")
        elif interrupted:
            logger.info(f"会话 {session_id}: 被打断 - 音频块: {audio_chunks}, 耗时: {total_time:.2f}s")
        elif should_exit:
            logger.info(f"会话 {session_id}: 本轮要求退出 - 音频块: {audio_chunks}, 耗时: {total_time:.2f}s")
        else:
            logger.info(f"会话 {session_id}: 完成 - 音频块: {audio_chunks}, 耗时: {total_time:.2f}s")

    except Exception as e:
        expected_cancellation = _is_expected_stream_cancellation(e)
        await _cancel_gateway_stream_thread(
            session_id,
            "LLM+TTS",
            thread_future,
            stop_requested,
            [("LLM", llm_call_holder), ("TTS", tts_call_holder)],
        )
        if expected_cancellation:
            session_manager.set_interrupted(session_id, False)
            trace_recorder.emit(
                trace.get("trace_id") if trace else None,
                session_id=session_id,
                round_seq=trace.get("round_seq") if trace else None,
                robot_id=trace.get("robot_id") if trace else None,
                bot_id=bot_id,
                bot_name=trace.get("bot_name") if trace else None,
                stage="llm_tts_interrupted",
                status="ok",
                error=None,
                summary=_build_llm_tts_cancelled_summary(
                    round_id=trace.get("round_id") if trace else None,
                    playback_id=trace.get("playback_id") if trace else None,
                ),
            )
            logger.info("会话 %s: LLM/TTS gRPC 流已按打断请求取消", session_id)
            await send_message(
                websocket,
                "done",
                exit=False,
                trace_id=trace.get("trace_id") if trace else None,
                round_id=trace.get("round_id") if trace else None,
                playback_id=trace.get("playback_id") if trace else None,
            )
            return
        trace_recorder.emit(
            trace.get("trace_id") if trace else None,
            session_id=session_id,
            round_seq=trace.get("round_seq") if trace else None,
            robot_id=trace.get("robot_id") if trace else None,
            bot_id=bot_id,
            bot_name=trace.get("bot_name") if trace else None,
            stage="llm_tts_error",
            status="error",
            error=str(e),
            summary=_build_llm_tts_error_summary(
                round_id=trace.get("round_id") if trace else None,
                playback_id=trace.get("playback_id") if trace else None,
            ),
        )
        logger.error(f"会话 {session_id}: LLM+TTS 处理失败 - {e}")
        await send_error(websocket, "LLM_TTS_FAILED", str(e))


# ============ WebSocket 处理 ============


class _SerializedInternalVoiceWebSocket:
    """Keep one writer for M1 while the endpoint reader stays responsive."""

    def __init__(self, websocket: WebSocket):
        self._websocket = websocket
        self._write_lock = asyncio.Lock()
        self._turn_control: dict[str, Any] | None = None

    def set_turn_control(self, control: dict[str, Any] | None) -> None:
        self._turn_control = control

    async def receive_json(self) -> Any:
        return await self._websocket.receive_json()

    async def receive(self) -> Any:
        return await self._websocket.receive()

    async def send_json(self, payload: Any) -> None:
        control = self._turn_control
        if control is not None and isinstance(payload, dict):
            message_type = str(payload.get("type") or "")
            if message_type == "error":
                control["legacy_error"] = {
                    "code": str(payload.get("code") or "PYTHON_GATEWAY_ERROR"),
                    "message": str(payload.get("message") or "Python Gateway error"),
                }
                return
            elif message_type == "done":
                control["legacy_done"] = dict(payload)
            elif message_type == "text" and (
                payload.get("asr_time_ms") is not None
                or control.get("input_text_commit")
            ):
                payload = InternalVoiceEnvelope.create(
                    TYPE_RESPONSE_ASR,
                    str(control.get("session_id") or ""),
                    trace_id=control.get("trace_id"),
                    utterance_id=control.get("utterance_id"),
                    round_id=control.get("round_id"),
                    playback_id=control.get("playback_id"),
                    payload={
                        "text": str(payload.get("content") or ""),
                        "valid": True,
                        "final": True,
                        "asr_time_ms": float(
                            payload.get("asr_time_ms")
                            if payload.get("asr_time_ms") is not None
                            else control.get("asr_time_ms") or 0.0
                        ),
                    },
                ).to_dict()
        async with self._write_lock:
            await self._websocket.send_json(payload)

    async def send_bytes(self, payload: bytes) -> None:
        control = self._turn_control
        if control is not None:
            header, audio_payload = decode_audio_frame(payload)
            header.update(
                {
                    "event_type": "response.audio",
                    "direction": "downlink",
                    "session_id": control.get("session_id"),
                    "trace_id": control.get("trace_id"),
                    "round_id": control.get("round_id"),
                    "playback_id": control.get("playback_id"),
                    "payload_bytes": len(audio_payload),
                }
            )
            payload = encode_audio_frame(audio_payload, **header)
        async with self._write_lock:
            await self._websocket.send_bytes(payload)

    async def send_text(self, payload: str) -> None:
        async with self._write_lock:
            await self._websocket.send_text(payload)

    async def close(self, *args, **kwargs) -> None:
        async with self._write_lock:
            await self._websocket.close(*args, **kwargs)


async def _send_internal_voice_envelope(
    websocket: Any,
    envelope: InternalVoiceEnvelope,
) -> None:
    await websocket.send_json(envelope.to_dict())


async def _send_internal_voice_protocol_error(
    websocket: WebSocket,
    session_id: str | None,
    message: str,
    *,
    code: str = "INVALID_ENVELOPE",
) -> None:
    await _send_internal_voice_envelope(
        websocket,
        InternalVoiceEnvelope.create(
            TYPE_PROTOCOL_ERROR,
            session_id or "unknown",
            payload={
                "code": code,
                "message": message,
            },
        ),
    )


def _decode_internal_voice_input_audio_batch(
    frame: bytes,
    *,
    session_id: str,
) -> tuple[dict[str, Any], bytes]:
    header, payload = decode_audio_frame(frame)
    if header.get("event_type") != TYPE_INPUT_AUDIO_BATCH:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FRAME",
            "internal voice 二进制帧 event_type 必须是 input_audio.batch",
        )
    if header.get("direction") != "uplink":
        raise AudioValidationError(
            "INVALID_AUDIO_FRAME",
            "internal voice 上行音频方向必须是 uplink",
        )
    if str(header.get("session_id") or "").strip() != session_id:
        raise AudioValidationError(
            "SESSION_MISMATCH",
            "internal voice 二进制音频 session_id 不匹配",
        )
    utterance_id = str(header.get("utterance_id") or "").strip()
    trace_id = str(header.get("trace_id") or "").strip()
    if not utterance_id or not trace_id:
        raise AudioValidationError(
            "INVALID_AUDIO_FRAME",
            "internal voice 二进制音频缺少 trace_id 或 utterance_id",
        )
    if header.get("encoding") != "opus":
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            "internal voice 上行音频仅支持 Opus",
        )
    packets = parse_opus_packet_stream(payload)
    try:
        declared_packets = int(header.get("packet_count") or 0)
        declared_bytes = int(header.get("payload_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise AudioValidationError(
            "INVALID_AUDIO_FRAME",
            "internal voice 二进制音频计数字段非法",
        ) from exc
    if declared_packets != len(packets):
        raise AudioValidationError(
            "INVALID_AUDIO_FRAME",
            "internal voice 二进制音频 packet_count 不匹配",
        )
    if declared_bytes != len(payload):
        raise AudioValidationError(
            "INVALID_AUDIO_FRAME",
            "internal voice 二进制音频 payload_bytes 不匹配",
        )
    return header, payload


def _internal_voice_ack_payload(
    payload: dict[str, Any],
    state_snapshot: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["session_state"] = state_snapshot
    return enriched


async def _ensure_internal_voice_active_session_registered(
    session_id: str,
    open_payload: dict[str, Any],
) -> None:
    if session_manager.is_registered(session_id):
        return
    robot_id = str(open_payload.get("robot_id") or "").strip()
    if not robot_id:
        raise ValueError("internal voice session.open payload.robot_id is required for active client_event")
    await register_robot_session(
        session_id,
        {
            "type": "register",
            "robot_id": robot_id,
            "robot_secret": str(open_payload.get("robot_secret") or "").strip(),
            "client_type": str(open_payload.get("client_type") or "").strip()
            or "go_voice_gateway",
        },
    )


async def _prewarm_internal_voice_active_session_registration(
    session_id: str,
    open_payload: dict[str, Any],
) -> bool:
    if GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE != "active":
        return False
    if session_manager.is_registered(session_id):
        return True
    if not str(open_payload.get("robot_secret") or "").strip():
        return False
    await _ensure_internal_voice_active_session_registered(session_id, open_payload)
    return True


def _internal_voice_client_event_turn_ids(envelope: InternalVoiceEnvelope) -> dict[str, str]:
    round_id = envelope.round_id or envelope.trace_id or (
        f"{envelope.session_id}:client_event:{envelope.timestamp_ms}"
    )
    playback_id = envelope.playback_id or f"{round_id}:playback"
    return {
        "trace_id": envelope.trace_id or round_id,
        "round_id": round_id,
        "playback_id": playback_id,
    }


def _internal_voice_audio_turn_ids(envelope: InternalVoiceEnvelope) -> dict[str, str]:
    round_id = envelope.trace_id or envelope.utterance_id
    if not round_id:
        round_id = f"{envelope.session_id}:audio:{envelope.timestamp_ms}"
    playback_id = f"{round_id}:playback"
    return {
        "trace_id": envelope.trace_id or round_id,
        "round_id": round_id,
        "playback_id": playback_id,
    }


async def _run_internal_voice_client_event_turn(
    websocket: Any,
    envelope: InternalVoiceEnvelope,
    *,
    event: str,
    session_open_payload: dict[str, Any],
    apply_state: Callable[[InternalVoiceEnvelope], dict[str, Any]],
    turn_control: dict[str, Any],
    turn_record: dict[str, Any] | None = None,
) -> None:
    turn_ids = _internal_voice_client_event_turn_ids(envelope)
    terminal_type = TYPE_RESPONSE_DONE
    terminal_payload: dict[str, Any] = {
        "exit": False,
        "reason": "completed",
        "event": event,
        "event_id": envelope.payload.get("event_id"),
    }
    try:
        await _ensure_internal_voice_active_session_registered(
            envelope.session_id,
            session_open_payload,
        )
        session_manager.start_round(
            envelope.session_id,
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
        )
        turn_control.update(
            {
                "session_id": envelope.session_id,
                "trace_id": turn_ids["trace_id"],
                "round_id": turn_ids["round_id"],
                "playback_id": turn_ids["playback_id"],
            }
        )
        websocket.set_turn_control(turn_control)
        await handle_client_event(
            websocket,
            envelope.session_id,
            {
                "type": "client_event",
                "event": event,
                "event_id": envelope.payload.get("event_id"),
                "source": envelope.payload.get("source") or "internal_voice_ws",
                "bot_id": envelope.payload.get("bot_id"),
            },
            trace_id=turn_ids["trace_id"],
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
            round_seq=None,
        )
        legacy_error = turn_control.get("legacy_error")
        if isinstance(legacy_error, dict):
            terminal_type = TYPE_RESPONSE_ERROR
            terminal_payload = {
                "origin": "python_gateway",
                "stage": "client_event",
                "code": legacy_error["code"],
                "message": legacy_error["message"],
                "retryable": False,
                "fatal": False,
            }
        elif turn_control.get("cancel_reason"):
            terminal_type = TYPE_RESPONSE_CANCELLED
            terminal_payload = {
                "reason": turn_control["cancel_reason"],
                "cancelled_stage": "tts",
                "expected": True,
            }
    except asyncio.CancelledError:
        terminal_type = TYPE_RESPONSE_CANCELLED
        terminal_payload = {
            "reason": turn_control.get("cancel_reason") or "cancelled",
            "cancelled_stage": "tts",
            "expected": True,
        }
    except Exception as exc:
        terminal_type = TYPE_RESPONSE_ERROR
        terminal_payload = {
            "origin": "python_gateway",
            "stage": "client_event",
            "code": "CLIENT_EVENT_ACTIVE_FAILED",
            "message": str(exc),
            "retryable": False,
            "fatal": False,
        }
        logger.error(
            "Internal voice client_event active failed: session=%s event=%s error=%s",
            envelope.session_id,
            event,
            exc,
        )
    finally:
        websocket.set_turn_control(None)
        session_manager.complete_round(envelope.session_id, turn_ids["round_id"])

    terminal = InternalVoiceEnvelope.create(
        terminal_type,
        envelope.session_id,
        trace_id=turn_ids["trace_id"],
        round_id=turn_ids["round_id"],
        playback_id=turn_ids["playback_id"],
        payload=terminal_payload,
    )
    state_snapshot = apply_state(terminal)
    if turn_record is not None:
        turn_record.update(
            {
                "status": "terminal",
                "terminal_type": terminal_type,
                "terminal_payload": dict(terminal_payload),
                **turn_ids,
            }
        )
    await _send_internal_voice_envelope(
        websocket,
        InternalVoiceEnvelope.create(
            terminal_type,
            envelope.session_id,
            trace_id=turn_ids["trace_id"],
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
            payload=_internal_voice_ack_payload(
                {
                    **terminal_payload,
                    "protocol": "internal_voice_ws",
                    "mode": "client_event_active",
                    "source": envelope.payload.get("source"),
                },
                state_snapshot,
            ),
        ),
    )
    logger.info(
        "Internal voice client_event active terminal: session=%s event=%s type=%s round_id=%s",
        envelope.session_id,
        event,
        terminal_type,
        turn_ids["round_id"],
    )


async def _run_internal_voice_audio_turn(
    websocket: _SerializedInternalVoiceWebSocket,
    envelope: InternalVoiceEnvelope,
    *,
    batch_header: dict[str, Any],
    batch_payload: bytes,
    turn_ids: dict[str, str],
    session_open_payload: dict[str, Any],
    apply_state: Callable[[InternalVoiceEnvelope], dict[str, Any]],
    turn_control: dict[str, Any],
    turn_record: dict[str, Any],
) -> None:
    terminal_type = TYPE_RESPONSE_DONE
    terminal_payload: dict[str, Any] = {
        "exit": False,
        "reason": "completed",
    }
    try:
        await _ensure_internal_voice_active_session_registered(
            envelope.session_id,
            session_open_payload,
        )
        session_manager.start_round(
            envelope.session_id,
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
        )
        turn_control.update(
            {
                "session_id": envelope.session_id,
                "trace_id": turn_ids["trace_id"],
                "utterance_id": envelope.utterance_id,
                "round_id": turn_ids["round_id"],
                "playback_id": turn_ids["playback_id"],
            }
        )
        websocket.set_turn_control(turn_control)
        await handle_audio(
            websocket,
            envelope.session_id,
            {
                "type": "audio",
                "audio_bytes": batch_payload,
                "audio_encoding": "opus",
                "audio_transport": "internal_voice_batch",
                "bot_id": batch_header.get("bot_id"),
                "trace_id": turn_ids["trace_id"],
                "utterance_id": envelope.utterance_id,
                "sample_rate": batch_header.get("sample_rate"),
                "channels": batch_header.get("channels"),
                "opus_frame_ms": batch_header.get("opus_frame_ms"),
                "packet_count": batch_header.get("packet_count"),
            },
            trace_id=turn_ids["trace_id"],
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
            round_seq=None,
        )
        legacy_error = turn_control.get("legacy_error")
        if isinstance(legacy_error, dict):
            terminal_type = TYPE_RESPONSE_ERROR
            terminal_payload = {
                "origin": "python_gateway",
                "stage": "orchestration",
                "code": legacy_error["code"],
                "message": legacy_error["message"],
                "retryable": False,
                "fatal": False,
            }
        elif turn_control.get("cancel_reason"):
            terminal_type = TYPE_RESPONSE_CANCELLED
            terminal_payload = {
                "reason": turn_control["cancel_reason"],
                "cancelled_stage": "orchestration",
                "expected": True,
            }
        elif isinstance(turn_control.get("legacy_done"), dict):
            terminal_payload["exit"] = bool(turn_control["legacy_done"].get("exit", False))
    except asyncio.CancelledError:
        terminal_type = TYPE_RESPONSE_CANCELLED
        terminal_payload = {
            "reason": turn_control.get("cancel_reason") or "cancelled",
            "cancelled_stage": "orchestration",
            "expected": True,
        }
    except Exception as exc:
        terminal_type = TYPE_RESPONSE_ERROR
        terminal_payload = {
            "origin": "python_gateway",
            "stage": "orchestration",
            "code": "INPUT_AUDIO_ACTIVE_FAILED",
            "message": str(exc),
            "retryable": False,
            "fatal": False,
        }
        logger.error(
            "Internal voice input_audio active failed: session=%s utterance_id=%s error=%s",
            envelope.session_id,
            envelope.utterance_id,
            exc,
        )
    finally:
        websocket.set_turn_control(None)
        session_manager.complete_round(envelope.session_id, turn_ids["round_id"])

    terminal = InternalVoiceEnvelope.create(
        terminal_type,
        envelope.session_id,
        trace_id=turn_ids["trace_id"],
        utterance_id=envelope.utterance_id,
        round_id=turn_ids["round_id"],
        playback_id=turn_ids["playback_id"],
        payload=terminal_payload,
    )
    state_snapshot = apply_state(terminal)
    turn_record.update(
        {
            "status": "terminal",
            "terminal_type": terminal_type,
            "terminal_payload": dict(terminal_payload),
            "trace_id": turn_ids["trace_id"],
            "round_id": turn_ids["round_id"],
            "playback_id": turn_ids["playback_id"],
        }
    )
    await _send_internal_voice_envelope(
        websocket,
        InternalVoiceEnvelope.create(
            terminal_type,
            envelope.session_id,
            trace_id=turn_ids["trace_id"],
            utterance_id=envelope.utterance_id,
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
            payload=_internal_voice_ack_payload(
                {
                    **terminal_payload,
                    "protocol": "internal_voice_ws",
                    "mode": "input_audio_active",
                },
                state_snapshot,
            ),
        ),
    )


async def _run_internal_voice_text_turn(
    websocket: _SerializedInternalVoiceWebSocket,
    envelope: InternalVoiceEnvelope,
    *,
    turn_ids: dict[str, str],
    session_open_payload: dict[str, Any],
    apply_state: Callable[[InternalVoiceEnvelope], dict[str, Any]],
    turn_control: dict[str, Any],
    turn_record: dict[str, Any],
) -> None:
    terminal_type = TYPE_RESPONSE_DONE
    terminal_payload: dict[str, Any] = {"exit": False, "reason": "completed"}
    try:
        await _ensure_internal_voice_active_session_registered(
            envelope.session_id, session_open_payload
        )
        session_manager.start_round(
            envelope.session_id,
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
        )
        turn_control.update(
            {
                "session_id": envelope.session_id,
                "trace_id": turn_ids["trace_id"],
                "utterance_id": envelope.utterance_id,
                "round_id": turn_ids["round_id"],
                "playback_id": turn_ids["playback_id"],
                "input_text_commit": True,
                "asr_time_ms": envelope.payload.get("asr_time_ms"),
            }
        )
        websocket.set_turn_control(turn_control)
        await handle_text(
            websocket,
            envelope.session_id,
            {
                "type": "text",
                "content": envelope.payload.get("content"),
                "source": envelope.payload.get("source") or "internal_voice_ws",
                "bot_id": envelope.payload.get("bot_id"),
                "trace_id": turn_ids["trace_id"],
                "utterance_id": envelope.utterance_id,
                "candidate_seq": envelope.payload.get("candidate_seq"),
                "speech_epoch": envelope.payload.get("speech_epoch"),
                "audio_watermark": envelope.payload.get("audio_watermark"),
            },
            trace_id=turn_ids["trace_id"],
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
            round_seq=None,
        )
        legacy_error = turn_control.get("legacy_error")
        if isinstance(legacy_error, dict):
            terminal_type = TYPE_RESPONSE_ERROR
            terminal_payload = {
                "origin": "python_gateway",
                "stage": "input_text",
                "code": legacy_error["code"],
                "message": legacy_error["message"],
                "retryable": False,
                "fatal": False,
            }
        elif turn_control.get("cancel_reason"):
            terminal_type = TYPE_RESPONSE_CANCELLED
            terminal_payload = {
                "reason": turn_control["cancel_reason"],
                "cancelled_stage": "orchestration",
                "expected": True,
            }
        elif isinstance(turn_control.get("legacy_done"), dict):
            terminal_payload["exit"] = bool(turn_control["legacy_done"].get("exit", False))
    except asyncio.CancelledError:
        terminal_type = TYPE_RESPONSE_CANCELLED
        terminal_payload = {
            "reason": turn_control.get("cancel_reason") or "cancelled",
            "cancelled_stage": "orchestration",
            "expected": True,
        }
    except Exception as exc:
        terminal_type = TYPE_RESPONSE_ERROR
        terminal_payload = {
            "origin": "python_gateway",
            "stage": "input_text",
            "code": "INPUT_TEXT_ACTIVE_FAILED",
            "message": str(exc),
            "retryable": False,
            "fatal": False,
        }
        logger.error(
            "Internal voice input_text active failed: session=%s utterance_id=%s error=%s",
            envelope.session_id,
            envelope.utterance_id,
            exc,
        )
    finally:
        websocket.set_turn_control(None)
        session_manager.complete_round(envelope.session_id, turn_ids["round_id"])

    terminal = InternalVoiceEnvelope.create(
        terminal_type,
        envelope.session_id,
        trace_id=turn_ids["trace_id"],
        utterance_id=envelope.utterance_id,
        round_id=turn_ids["round_id"],
        playback_id=turn_ids["playback_id"],
        payload=terminal_payload,
    )
    state_snapshot = apply_state(terminal)
    turn_record.update(
        {
            "status": "terminal",
            "terminal_type": terminal_type,
            "terminal_payload": dict(terminal_payload),
            **turn_ids,
        }
    )
    await _send_internal_voice_envelope(
        websocket,
        InternalVoiceEnvelope.create(
            terminal_type,
            envelope.session_id,
            trace_id=turn_ids["trace_id"],
            utterance_id=envelope.utterance_id,
            round_id=turn_ids["round_id"],
            playback_id=turn_ids["playback_id"],
            payload=_internal_voice_ack_payload(
                {
                    **terminal_payload,
                    "protocol": "internal_voice_ws",
                    "mode": "input_text_active",
                },
                state_snapshot,
            ),
        ),
    )


@app.websocket("/internal/voice/ws")
async def internal_voice_ws_endpoint(websocket: WebSocket):
    """M1 Go-Python internal voice protocol handshake endpoint."""
    if not GATEWAY_INTERNAL_VOICE_WS_ENABLED:
        await websocket.close(code=1008, reason="Internal voice protocol disabled")
        return

    await websocket.accept()
    websocket = _SerializedInternalVoiceWebSocket(websocket)
    session_id: str | None = None
    state_tracker: InternalVoiceSessionStateTracker | None = None
    session_open_payload: dict[str, Any] = {}
    pending_audio_batches: dict[str, tuple[dict[str, Any], bytes]] = {}
    accepted_audio_turns: dict[str, dict[str, Any]] = {}
    accepted_text_turns: dict[str, dict[str, Any]] = {}
    accepted_client_events: dict[str, dict[str, Any]] = {}
    active_turn: dict[str, Any] | None = None
    audio_round_seq = 0
    logger.info("Internal voice WS connected")

    def _apply_internal_voice_state(envelope: InternalVoiceEnvelope) -> dict[str, Any]:
        nonlocal state_tracker
        if state_tracker is None or state_tracker.session_id != envelope.session_id:
            state_tracker = InternalVoiceSessionStateTracker(envelope.session_id)
        return state_tracker.apply(envelope)

    async def _cancel_active_turn(reason: str, *, wait: bool) -> None:
        nonlocal active_turn
        await cancel_complex_workflow_session(session_id or "", reason=reason)
        control = active_turn
        if not control:
            return
        task = control.get("task")
        if not isinstance(task, asyncio.Task) or task.done():
            return
        control["cancel_reason"] = reason
        task.cancel()
        if wait:
            await asyncio.gather(task, return_exceptions=True)

    async def _run_and_clear_active_turn(
        envelope: InternalVoiceEnvelope,
        event: str,
        control: dict[str, Any],
        turn_record: dict[str, Any] | None,
    ) -> None:
        nonlocal active_turn
        try:
            await _run_internal_voice_client_event_turn(
                websocket,
                envelope,
                event=event,
                session_open_payload=session_open_payload,
                apply_state=_apply_internal_voice_state,
                turn_control=control,
                turn_record=turn_record,
            )
        finally:
            if active_turn is control:
                active_turn = None

    async def _run_and_clear_active_audio_turn(
        envelope: InternalVoiceEnvelope,
        *,
        batch_header: dict[str, Any],
        batch_payload: bytes,
        turn_ids: dict[str, str],
        control: dict[str, Any],
        turn_record: dict[str, Any],
    ) -> None:
        nonlocal active_turn
        try:
            await _run_internal_voice_audio_turn(
                websocket,
                envelope,
                batch_header=batch_header,
                batch_payload=batch_payload,
                turn_ids=turn_ids,
                session_open_payload=session_open_payload,
                apply_state=_apply_internal_voice_state,
                turn_control=control,
                turn_record=turn_record,
            )
        finally:
            if active_turn is control:
                active_turn = None

    async def _run_and_clear_active_text_turn(
        envelope: InternalVoiceEnvelope,
        *,
        turn_ids: dict[str, str],
        control: dict[str, Any],
        turn_record: dict[str, Any],
    ) -> None:
        nonlocal active_turn
        try:
            await _run_internal_voice_text_turn(
                websocket,
                envelope,
                turn_ids=turn_ids,
                session_open_payload=session_open_payload,
                apply_state=_apply_internal_voice_state,
                turn_control=control,
                turn_record=turn_record,
            )
        finally:
            if active_turn is control:
                active_turn = None

    try:
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                break
            except Exception as exc:
                await _send_internal_voice_protocol_error(
                    websocket,
                    session_id,
                    str(exc),
                    code="INVALID_JSON",
                )
                continue

            message_type = message.get("type")
            if message_type == "websocket.disconnect":
                break
            binary_payload = message.get("bytes")
            if binary_payload is not None:
                if not session_id:
                    await _send_internal_voice_protocol_error(
                        websocket,
                        session_id,
                        "internal voice session.open is required before binary audio",
                        code="SESSION_NOT_OPEN",
                    )
                    continue
                try:
                    header, audio_payload = _decode_internal_voice_input_audio_batch(
                        binary_payload,
                        session_id=session_id,
                    )
                except AudioValidationError as exc:
                    await _send_internal_voice_protocol_error(
                        websocket,
                        session_id,
                        exc.message,
                        code=exc.code,
                    )
                    continue
                utterance_id = str(header["utterance_id"])
                already_accepted = utterance_id in accepted_audio_turns
                duplicate = utterance_id in pending_audio_batches or already_accepted
                if not duplicate:
                    pending_audio_batches.clear()
                    pending_audio_batches[utterance_id] = (header, audio_payload)
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        TYPE_ORCHESTRATOR_STATUS,
                        session_id,
                        trace_id=str(header["trace_id"]),
                        utterance_id=utterance_id,
                        payload={
                            "status": "accepted",
                            "protocol": "internal_voice_ws",
                            "mode": "input_audio_batch_shadow",
                            "input_audio_type": TYPE_INPUT_AUDIO_BATCH,
                            "utterance_id": utterance_id,
                            "packet_count": int(header["packet_count"]),
                            "payload_bytes": len(audio_payload),
                            "duplicate": duplicate,
                        },
                    ),
                )
                continue
            text_payload = message.get("text")
            if text_payload is None:
                await _send_internal_voice_protocol_error(
                    websocket,
                    session_id,
                    "internal voice WebSocket message missing text or bytes",
                    code="INVALID_WEBSOCKET_MESSAGE",
                )
                continue
            try:
                payload = decode_ws_text_message(text_payload)
            except AudioValidationError as exc:
                await _send_internal_voice_protocol_error(
                    websocket,
                    session_id,
                    exc.message,
                    code=exc.code,
                )
                continue

            try:
                envelope = InternalVoiceEnvelope.from_dict(payload)
            except ValueError as exc:
                await _send_internal_voice_protocol_error(websocket, session_id, str(exc))
                continue

            if session_id and envelope.session_id != session_id:
                await _send_internal_voice_protocol_error(
                    websocket,
                    session_id,
                    (
                        "internal voice session mismatch: "
                        f"expected={session_id} actual={envelope.session_id}"
                    ),
                    code="SESSION_MISMATCH",
                )
                continue
            if not session_id and envelope.type != TYPE_SESSION_OPEN:
                await _send_internal_voice_protocol_error(
                    websocket,
                    envelope.session_id,
                    "internal voice session.open is required before session messages",
                    code="SESSION_NOT_OPEN",
                )
                continue
            session_id = envelope.session_id

            if envelope.type == TYPE_HEARTBEAT_PING:
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        TYPE_HEARTBEAT_PONG,
                        envelope.session_id,
                        trace_id=envelope.trace_id,
                        payload={"nonce": envelope.payload.get("nonce")},
                    ),
                )
                continue

            if envelope.type == TYPE_SESSION_OPEN:
                session_manager.ensure_session(envelope.session_id)
                session_open_payload = dict(envelope.payload)
                state_snapshot = _apply_internal_voice_state(envelope)
                pre_registered = False
                try:
                    pre_registered = await _prewarm_internal_voice_active_session_registration(
                        envelope.session_id,
                        session_open_payload,
                    )
                except Exception as exc:
                    logger.error(
                        "Internal voice session active pre-register failed: session=%s error=%s",
                        envelope.session_id,
                        exc,
                    )
                    await _send_internal_voice_protocol_error(
                        websocket,
                        envelope.session_id,
                        str(exc),
                        code="SESSION_OPEN_FAILED",
                    )
                    continue
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        "session.opened",
                        envelope.session_id,
                        trace_id=envelope.trace_id,
                        payload=_internal_voice_ack_payload(
                            {
                                "accepted": True,
                                "protocol": "internal_voice_ws",
                                "mode": "handshake_only",
                                "pre_registered": pre_registered,
                            },
                            state_snapshot,
                        ),
                    ),
                )
                logger.info("Internal voice session opened: %s", envelope.session_id)
                continue

            if envelope.type == TYPE_SESSION_CLOSE:
                await _cancel_active_turn("session_close", wait=True)
                state_snapshot = _apply_internal_voice_state(envelope)
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        "session.closed",
                        envelope.session_id,
                        trace_id=envelope.trace_id,
                        payload=_internal_voice_ack_payload(
                            {
                                "closed": True,
                                "protocol": "internal_voice_ws",
                            },
                            state_snapshot,
                        ),
                    ),
                )
                logger.info("Internal voice session closed: %s", envelope.session_id)
                session_manager.delete_session(envelope.session_id)
                break

            if envelope.type == TYPE_INPUT_TEXT_COMMIT:
                content = str(envelope.payload.get("content") or "").strip()
                if not content:
                    await _send_internal_voice_protocol_error(
                        websocket,
                        envelope.session_id,
                        "internal voice input_text.commit payload.content is required",
                        code="INPUT_TEXT_CONTENT_MISSING",
                    )
                    continue
                utterance_id = envelope.utterance_id or ""
                accepted_turn = accepted_text_turns.get(utterance_id)
                if accepted_turn is not None:
                    await _send_internal_voice_envelope(
                        websocket,
                        InternalVoiceEnvelope.create(
                            TYPE_ORCHESTRATOR_STATUS,
                            envelope.session_id,
                            trace_id=accepted_turn.get("trace_id") or envelope.trace_id,
                            utterance_id=envelope.utterance_id,
                            round_id=accepted_turn.get("round_id"),
                            playback_id=accepted_turn.get("playback_id"),
                            payload={
                                "status": accepted_turn.get("status", "accepted"),
                                "protocol": "internal_voice_ws",
                                "mode": "input_text_active_duplicate",
                                "duplicate": True,
                                "terminal_type": accepted_turn.get("terminal_type"),
                                "terminal_payload": accepted_turn.get("terminal_payload"),
                            },
                        ),
                    )
                    if not accepted_turn.get("terminal_type"):
                        accepted_task = accepted_turn.get("task")
                        if isinstance(accepted_task, asyncio.Task):
                            await asyncio.gather(
                                asyncio.shield(accepted_task),
                                return_exceptions=True,
                            )
                    if accepted_turn.get("terminal_type"):
                        await _send_internal_voice_envelope(
                            websocket,
                            InternalVoiceEnvelope.create(
                                accepted_turn["terminal_type"],
                                envelope.session_id,
                                trace_id=accepted_turn.get("trace_id"),
                                utterance_id=envelope.utterance_id,
                                round_id=accepted_turn.get("round_id"),
                                playback_id=accepted_turn.get("playback_id"),
                                payload=accepted_turn.get("terminal_payload") or {},
                            ),
                        )
                    continue
                await _cancel_active_turn("new_input_text", wait=True)
                turn_ids = _internal_voice_audio_turn_ids(envelope)
                state_snapshot = _apply_internal_voice_state(
                    InternalVoiceEnvelope.create(
                        TYPE_INPUT_TEXT_COMMIT,
                        envelope.session_id,
                        trace_id=turn_ids["trace_id"],
                        utterance_id=envelope.utterance_id,
                        round_id=turn_ids["round_id"],
                        playback_id=turn_ids["playback_id"],
                        payload=envelope.payload,
                    )
                )
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        TYPE_ORCHESTRATOR_STATUS,
                        envelope.session_id,
                        trace_id=turn_ids["trace_id"],
                        utterance_id=envelope.utterance_id,
                        round_id=turn_ids["round_id"],
                        playback_id=turn_ids["playback_id"],
                        payload=_internal_voice_ack_payload(
                            {
                                "status": "accepted",
                                "protocol": "internal_voice_ws",
                                "mode": "input_text_active_accepted",
                                "source": envelope.payload.get("source"),
                            },
                            state_snapshot,
                        ),
                    ),
                )
                control = {"cancel_reason": None}
                turn_record = {"status": "accepted", **turn_ids}
                accepted_text_turns[utterance_id] = turn_record
                while len(accepted_text_turns) > 32:
                    accepted_text_turns.pop(next(iter(accepted_text_turns)))
                task = asyncio.create_task(
                    _run_and_clear_active_text_turn(
                        envelope,
                        turn_ids=turn_ids,
                        control=control,
                        turn_record=turn_record,
                    )
                )
                control["task"] = task
                turn_record["task"] = task
                active_turn = control
                continue

            if envelope.type in {
                TYPE_INPUT_AUDIO_START,
                TYPE_INPUT_AUDIO_END,
                TYPE_INPUT_AUDIO_CANCEL,
            }:
                retained_batch = pending_audio_batches.get(envelope.utterance_id or "")
                accepted_turn = accepted_audio_turns.get(envelope.utterance_id or "")
                active_audio_request = (
                    envelope.type == TYPE_INPUT_AUDIO_END
                    and envelope.payload.get("orchestration_mode") == "active"
                )
                if active_audio_request and accepted_turn is not None:
                    await _send_internal_voice_envelope(
                        websocket,
                        InternalVoiceEnvelope.create(
                            TYPE_ORCHESTRATOR_STATUS,
                            envelope.session_id,
                            trace_id=accepted_turn.get("trace_id") or envelope.trace_id,
                            utterance_id=envelope.utterance_id,
                            round_id=accepted_turn.get("round_id"),
                            playback_id=accepted_turn.get("playback_id"),
                            payload={
                                "status": accepted_turn.get("status", "accepted"),
                                "protocol": "internal_voice_ws",
                                "mode": "input_audio_active_duplicate",
                                "duplicate": True,
                                "terminal_type": accepted_turn.get("terminal_type"),
                                "terminal_payload": accepted_turn.get("terminal_payload"),
                            },
                        ),
                    )
                    continue
                if active_audio_request and retained_batch is None:
                    await _send_internal_voice_protocol_error(
                        websocket,
                        envelope.session_id,
                        "input_audio.end active mode requires a retained input_audio.batch",
                        code="INPUT_AUDIO_BATCH_MISSING",
                    )
                    continue
                if envelope.type == TYPE_INPUT_AUDIO_START:
                    pending_audio_batches.pop(envelope.utterance_id or "", None)
                elif envelope.type in {TYPE_INPUT_AUDIO_END, TYPE_INPUT_AUDIO_CANCEL}:
                    pending_audio_batches.pop(envelope.utterance_id or "", None)
                if (
                    envelope.type == TYPE_INPUT_AUDIO_CANCEL
                    and envelope.payload.get("ownership_mode") == "active"
                ):
                    _cancel_current_round_for_new_input(envelope.session_id)
                    await _cancel_active_turn(
                        str(envelope.payload.get("reason") or "audio_cancel"),
                        wait=False,
                    )
                state_snapshot = _apply_internal_voice_state(envelope)
                ack_payload = {
                    "status": "accepted",
                    "protocol": "internal_voice_ws",
                    "mode": "input_audio_metadata_ack_only",
                    "input_audio_type": envelope.type,
                    "utterance_id": envelope.utterance_id,
                }
                if envelope.type == TYPE_INPUT_AUDIO_START:
                    ack_payload.update(
                        {
                            "codec": envelope.payload.get("codec"),
                            "packet_format": envelope.payload.get("packet_format"),
                            "sample_rate": envelope.payload.get("sample_rate"),
                            "channels": envelope.payload.get("channels"),
                            "frame_ms": envelope.payload.get("frame_ms"),
                            "ice_route": envelope.payload.get("ice_route"),
                        }
                    )
                elif envelope.type == TYPE_INPUT_AUDIO_END:
                    ack_payload.update(
                        {
                            "packet_count": envelope.payload.get("packet_count", 0),
                            "payload_bytes": envelope.payload.get("payload_bytes", 0),
                            "duration_ms": envelope.payload.get("duration_ms", 0),
                            "lossy": envelope.payload.get("lossy", False),
                        }
                    )
                    if retained_batch is not None:
                        batch_header, batch_payload = retained_batch
                        ack_payload.update(
                            {
                                "mode": "input_audio_batch_shadow_ready",
                                "packet_count": int(batch_header["packet_count"]),
                                "payload_bytes": len(batch_payload),
                            }
                        )
                    if active_audio_request:
                        ack_payload["mode"] = "input_audio_active_accepted"
                else:
                    ack_payload.update(
                        {
                            "reason": envelope.payload.get("reason"),
                            "source": envelope.payload.get("source"),
                            "transport": envelope.payload.get("transport"),
                        }
                    )
                    if envelope.payload.get("ownership_mode") == "active":
                        ack_payload["mode"] = "input_audio_cancel_active"
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        TYPE_ORCHESTRATOR_STATUS,
                        envelope.session_id,
                        trace_id=envelope.trace_id,
                        utterance_id=envelope.utterance_id,
                        payload=_internal_voice_ack_payload(ack_payload, state_snapshot),
                    ),
                )
                logger.info(
                    "Internal voice input_audio accepted: session=%s type=%s utterance_id=%s",
                    envelope.session_id,
                    envelope.type,
                    envelope.utterance_id,
                )
                if active_audio_request:
                    await _cancel_active_turn("new_input_audio", wait=True)
                    audio_round_seq += 1
                    batch_header, batch_payload = retained_batch
                    turn_ids = _internal_voice_audio_turn_ids(envelope)
                    control = {"cancel_reason": None}
                    turn_record = {
                        "status": "accepted",
                        **turn_ids,
                    }
                    accepted_audio_turns[envelope.utterance_id or ""] = turn_record
                    while len(accepted_audio_turns) > 32:
                        accepted_audio_turns.pop(next(iter(accepted_audio_turns)))
                    task = asyncio.create_task(
                        _run_and_clear_active_audio_turn(
                            envelope,
                            batch_header=batch_header,
                            batch_payload=batch_payload,
                            turn_ids=turn_ids,
                            control=control,
                            turn_record=turn_record,
                        )
                    )
                    control["task"] = task
                    active_turn = control
                continue

            if envelope.type == TYPE_CLIENT_EVENT:
                event = str(envelope.payload.get("event") or "").strip()
                event_id = str(envelope.payload.get("event_id") or "").strip()
                if not event:
                    await _send_internal_voice_protocol_error(
                        websocket,
                        envelope.session_id,
                        "internal voice client_event payload.event is required",
                    )
                    continue
                if GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE == "active":
                    accepted_event = accepted_client_events.get(event_id) if event_id else None
                    if accepted_event is not None:
                        await _send_internal_voice_envelope(
                            websocket,
                            InternalVoiceEnvelope.create(
                                TYPE_ORCHESTRATOR_STATUS,
                                envelope.session_id,
                                trace_id=accepted_event.get("trace_id") or envelope.trace_id,
                                round_id=accepted_event.get("round_id"),
                                playback_id=accepted_event.get("playback_id"),
                                payload={
                                    "status": accepted_event.get("status", "accepted"),
                                    "protocol": "internal_voice_ws",
                                    "mode": "client_event_active_duplicate",
                                    "event": event,
                                    "event_id": event_id,
                                    "duplicate": True,
                                },
                            ),
                        )
                        terminal_type = accepted_event.get("terminal_type")
                        if terminal_type:
                            await _send_internal_voice_envelope(
                                websocket,
                                InternalVoiceEnvelope.create(
                                    terminal_type,
                                    envelope.session_id,
                                    trace_id=accepted_event.get("trace_id"),
                                    round_id=accepted_event.get("round_id"),
                                    playback_id=accepted_event.get("playback_id"),
                                    payload=accepted_event.get("terminal_payload") or {},
                                ),
                            )
                        continue
                    await _cancel_active_turn("new_client_event", wait=True)
                    turn_ids = _internal_voice_client_event_turn_ids(envelope)
                    state_snapshot = _apply_internal_voice_state(
                        InternalVoiceEnvelope.create(
                            TYPE_CLIENT_EVENT,
                            envelope.session_id,
                            trace_id=turn_ids["trace_id"],
                            round_id=turn_ids["round_id"],
                            playback_id=turn_ids["playback_id"],
                            payload=envelope.payload,
                        )
                    )
                    await _send_internal_voice_envelope(
                        websocket,
                        InternalVoiceEnvelope.create(
                            TYPE_ORCHESTRATOR_STATUS,
                            envelope.session_id,
                            trace_id=turn_ids["trace_id"],
                            round_id=turn_ids["round_id"],
                            playback_id=turn_ids["playback_id"],
                            payload=_internal_voice_ack_payload(
                                {
                                    "status": "accepted",
                                    "protocol": "internal_voice_ws",
                                    "mode": "client_event_active_accepted",
                                    "event": event,
                                    "event_id": envelope.payload.get("event_id"),
                                },
                                state_snapshot,
                            ),
                        ),
                    )
                    control: dict[str, Any] = {"cancel_reason": None}
                    turn_record = None
                    if event_id:
                        turn_record = {
                            "status": "accepted",
                            **turn_ids,
                        }
                        accepted_client_events[event_id] = turn_record
                        while len(accepted_client_events) > 32:
                            accepted_client_events.pop(next(iter(accepted_client_events)))
                    task = asyncio.create_task(
                        _run_and_clear_active_turn(
                            envelope,
                            event,
                            control,
                            turn_record,
                        )
                    )
                    control["task"] = task
                    active_turn = control
                    continue
                state_snapshot = _apply_internal_voice_state(envelope)
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        TYPE_ORCHESTRATOR_STATUS,
                        envelope.session_id,
                        trace_id=envelope.trace_id,
                        payload=_internal_voice_ack_payload(
                            {
                                "status": "accepted",
                                "protocol": "internal_voice_ws",
                                "mode": "client_event_ack_only",
                                "event": event,
                                "event_id": envelope.payload.get("event_id"),
                                "source": envelope.payload.get("source"),
                            },
                            state_snapshot,
                        ),
                    ),
                )
                logger.info(
                    "Internal voice client_event accepted: session=%s event=%s",
                    envelope.session_id,
                    event,
                )
                continue

            if envelope.type == TYPE_INTERRUPT:
                cancelled_round = _cancel_current_round_for_new_input(envelope.session_id)
                await _cancel_active_turn(
                    str(envelope.payload.get("reason") or "interrupt"),
                    wait=True,
                )
                state_snapshot = _apply_internal_voice_state(envelope)
                interrupt_ack = {
                    "status": "accepted",
                    "protocol": "internal_voice_ws",
                    "mode": "interrupt_ack_only",
                    "reason": envelope.payload.get("reason"),
                    "source": envelope.payload.get("source"),
                    "transport": envelope.payload.get("transport"),
                }
                if envelope.payload.get("ownership_mode") == "active":
                    interrupt_ack["mode"] = "interrupt_active"
                if cancelled_round is not None:
                    interrupt_ack["cancelled_round"] = cancelled_round
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        TYPE_ORCHESTRATOR_STATUS,
                        envelope.session_id,
                        trace_id=envelope.trace_id,
                        utterance_id=envelope.utterance_id,
                        payload=_internal_voice_ack_payload(
                            interrupt_ack,
                            state_snapshot,
                        ),
                    ),
                )
                logger.info(
                    "Internal voice interrupt accepted: session=%s reason=%s",
                    envelope.session_id,
                    envelope.payload.get("reason") or "-",
                )
                continue

            if envelope.type == TYPE_PLAYBACK_REPORT:
                report_type = str(envelope.payload.get("report_type") or "").strip()
                if envelope.payload.get("ownership_mode") == "active":
                    _handle_playback_report_message(
                        envelope.session_id,
                        {
                            **dict(envelope.payload),
                            "trace_id": envelope.trace_id,
                            "round_id": envelope.round_id,
                            "playback_id": envelope.playback_id,
                        },
                        report_type,
                    )
                state_snapshot = _apply_internal_voice_state(envelope)
                await _send_internal_voice_envelope(
                    websocket,
                    InternalVoiceEnvelope.create(
                        TYPE_ORCHESTRATOR_STATUS,
                        envelope.session_id,
                        trace_id=envelope.trace_id,
                        round_id=envelope.round_id,
                        playback_id=envelope.playback_id,
                        payload=_internal_voice_ack_payload(
                            {
                                "status": "accepted",
                                "protocol": "internal_voice_ws",
                                "mode": (
                                    "playback_report_active"
                                    if envelope.payload.get("ownership_mode") == "active"
                                    else "playback_report_ack_only"
                                ),
                                "report_type": report_type,
                                "reason": envelope.payload.get("reason"),
                                "round_id": envelope.round_id,
                                "playback_id": envelope.playback_id,
                            },
                            state_snapshot,
                        ),
                    ),
                )
                logger.info(
                    "Internal voice playback report accepted: session=%s report_type=%s round_id=%s",
                    envelope.session_id,
                    envelope.payload.get("report_type") or "-",
                    envelope.round_id or "-",
                )
                continue

            state_snapshot = _apply_internal_voice_state(envelope)
            await _send_internal_voice_envelope(
                websocket,
                InternalVoiceEnvelope.create(
                    TYPE_ORCHESTRATOR_STATUS,
                    envelope.session_id,
                    trace_id=envelope.trace_id,
                    utterance_id=envelope.utterance_id,
                    round_id=envelope.round_id,
                    playback_id=envelope.playback_id,
                    payload=_internal_voice_ack_payload(
                        {
                            "status": "ignored",
                            "reason": "internal voice protocol skeleton only",
                        },
                        state_snapshot,
                    ),
                ),
            )
    except WebSocketDisconnect:
        pass
    finally:
        pending_audio_batches.clear()
        accepted_audio_turns.clear()
        accepted_text_turns.clear()
        accepted_client_events.clear()
        await _cancel_active_turn("connection_closed", wait=True)
        if session_id:
            session_manager.delete_session(session_id)
        logger.info("Internal voice WS disconnected: %s", session_id or "-")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket 连接处理"""
    global active_connections

    runtime_settings = get_gateway_settings()

    # 检查连接数限制（先检查再操作，避免在锁内 await）
    with _connections_lock:
        if active_connections >= runtime_settings["max_connections"]:
            # 标记需要拒绝，但在锁外执行 await
            should_reject = True
        else:
            # 先增加计数，确保不会超限
            active_connections += 1
            should_reject = False

    if should_reject:
        await websocket.close(code=1008, reason="Too many connections")
        logger.warning("拒绝连接: 超过最大连接数")
        return

    # 在锁外接受连接
    try:
        await websocket.accept()
    except Exception as e:
        # 接受失败，回滚计数
        with _connections_lock:
            active_connections -= 1
        logger.error(f"接受 WebSocket 连接失败: {e}")
        return

    # 创建会话
    session_id = session_manager.create_session()
    await send_message(websocket, "connected", session_id=session_id)

    logger.info(f"新连接: {session_id}, 当前连接数: {active_connections}")

    request_queue: asyncio.Queue = asyncio.Queue(maxsize=GATEWAY_REQUEST_QUEUE_MAXSIZE)
    last_interrupt_time: float = 0  # 上次打断时间戳
    round_seq: int = 0
    audio_streams: dict[str, AudioStreamAssembler] = {}

    async def _recv_loop():
        """持续接收 WebSocket 消息，确保 interrupt 能被实时处理"""
        nonlocal last_interrupt_time
        while True:
            try:
                data = await receive_client_ws_message(websocket)
            except AudioValidationError as exc:
                await send_error(websocket, exc.code, exc.message)
                continue
            msg_type = data.get("type")

            if await _handle_register_message(
                websocket,
                session_id=session_id,
                data=data,
                msg_type=msg_type,
            ):
                continue
            if await _handle_rtc_signaling_message(
                websocket,
                session_id=session_id,
                data=data,
                msg_type=msg_type,
            ):
                continue
            if await _handle_turn_candidate_shadow_message(
                websocket,
                session_id=session_id,
                data=data,
                msg_type=msg_type,
            ):
                continue
            if await _handle_barge_in_probe_message(
                websocket,
                session_id=session_id,
                data=data,
                msg_type=msg_type,
            ):
                continue
            if await _handle_audio_stream_message(
                websocket,
                request_queue,
                audio_streams,
                session_id=session_id,
                data=data,
                msg_type=msg_type,
            ):
                continue
            if await _handle_queued_user_input_message(
                websocket,
                request_queue,
                audio_streams,
                session_id=session_id,
                data=data,
                msg_type=msg_type,
            ):
                continue
            if await _handle_keepalive_message(websocket, session_id, msg_type):
                continue
            if _handle_playback_report_message(session_id, data, msg_type):
                continue
            handled_interrupt, last_interrupt_time = await _handle_interrupt_message(
                websocket,
                audio_streams,
                session_id=session_id,
                msg_type=msg_type,
                last_interrupt_time=last_interrupt_time,
            )
            if handled_interrupt:
                continue
            await send_error(websocket, "UNKNOWN_TYPE", f"未知消息类型: {msg_type}")

    async def _process_loop():
        """从队列取请求并处理（阻塞不影响 _recv_loop）"""
        nonlocal round_seq
        while True:
            data = await request_queue.get()
            round_seq += 1
            turn_ids = _start_queued_request_round(
                request_queue,
                data,
                session_id=session_id,
                round_seq=round_seq,
            )
            trace_id = turn_ids["trace_id"]
            round_id = turn_ids["round_id"]
            playback_id = turn_ids["playback_id"]
            try:
                await _process_queued_request(
                    websocket,
                    session_id=session_id,
                    data=data,
                    trace_id=trace_id,
                    round_id=round_id,
                    playback_id=playback_id,
                    round_seq=round_seq,
                )
            finally:
                session_manager.complete_round(session_id, round_id)

    recv_task = None
    process_task = None

    try:
        recv_task = asyncio.create_task(_recv_loop())
        process_task = asyncio.create_task(_process_loop())
        done, pending = await asyncio.wait(
            [recv_task, process_task],
            return_when=asyncio.FIRST_EXCEPTION
        )
        # 取消还在跑的任务
        for task in pending:
            task.cancel()
        # 抛出已完成任务的异常（如果有）
        for task in done:
            task.result()
    except WebSocketDisconnect:
        logger.info(f"连接断开: {session_id}")
    except asyncio.CancelledError:
        logger.info(f"任务被取消: {session_id}")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
    finally:
        session_manager.set_interrupted(session_id, True)
        cleanup_tasks = [
            task
            for task in (recv_task, process_task)
            if task is not None and not task.done()
        ]
        for task in cleanup_tasks:
            task.cancel()
        if cleanup_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*cleanup_tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                logger.warning("会话 %s: WebSocket 清理等待任务取消超时", session_id)

        # 减少连接计数（计数在接受前已增加）
        with _connections_lock:
            active_connections -= 1

        await cleanup_llm_session(session_id)

        # 清理会话数据
        session_manager.delete_session(session_id)

        logger.info(f"清理会话: {session_id}, 剩余连接数: {active_connections}")


async def handle_audio(
    websocket: WebSocket,
    session_id: str,
    data: dict,
    *,
    trace_id: str | None = None,
    round_id: str | None = None,
    playback_id: str | None = None,
    round_seq: int | None = None,
):
    """处理音频消息（唤醒词检测已移至客户端）"""
    trace_context = session_manager.get_trace_context(session_id)
    trace = _build_turn_trace(
        data,
        trace_context,
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
        round_seq=round_seq,
    )
    round_id = trace["round_id"]
    playback_id = trace["playback_id"]
    try:
        _record_audio_received_trace(
            trace_id=trace_id,
            session_id=session_id,
            round_seq=round_seq,
            trace=trace,
        )
        if ROBOT_SECRET_REQUIRED and (
            await _send_register_required_if_unregistered(
                websocket,
                session_id=session_id,
                trace_id=trace_id,
                round_seq=round_seq,
            )
        ):
            return
        # 重置打断标志（新请求开始，清除上一次的打断状态）
        session_manager.set_interrupted(session_id, False)

        try:
            decoded_audio = _decode_audio_request_payload(data)
        except AudioValidationError as exc:
            await _send_audio_rejected_error(
                websocket,
                trace_id=trace_id,
                session_id=session_id,
                round_seq=round_seq,
                trace=trace,
                error=exc,
            )
            return
        audio_data = decoded_audio.audio_data
        audio_metadata = decoded_audio.audio_metadata
        audio_transport = decoded_audio.audio_transport
        audio_encoding = decoded_audio.audio_encoding
        _warn_if_audio_truncated(session_id, audio_metadata)

        runtime_context = await _resolve_turn_runtime_context(session_id, data, trace)
        robot_id = runtime_context["robot_id"]
        bot_id = runtime_context["bot_id"]
        bot_name = runtime_context["bot_name"]
        bot_tts_settings = runtime_context["bot_tts_settings"]

        _record_audio_decoded_trace_and_logs(
            trace_id=trace_id,
            session_id=session_id,
            round_seq=round_seq,
            round_id=round_id,
            playback_id=playback_id,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            audio_data=audio_data,
            audio_metadata=audio_metadata,
            audio_transport=audio_transport,
            audio_encoding=audio_encoding,
        )

        asr_trace_summary = _build_asr_trace_summary_for_decoded_audio(
            decoded_audio,
            trace_id=trace_id,
            round_id=round_id,
            playback_id=playback_id,
        )

        # 1. ASR 识别
        await _send_asr_start_status_and_trace(
            websocket,
            trace_id=trace_id,
            session_id=session_id,
            round_seq=round_seq,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            asr_trace_summary=asr_trace_summary,
        )
        text, asr_time_ms, asr_metadata = await process_asr(audio_data, session_id)

        if session_manager.is_interrupted(session_id):
            _record_interrupted_asr_result(
                trace_id=trace_id,
                session_id=session_id,
                round_seq=round_seq,
                robot_id=robot_id,
                bot_id=bot_id,
                bot_name=bot_name,
                asr_time_ms=asr_time_ms,
                asr_trace_summary=asr_trace_summary,
                asr_metadata=asr_metadata,
            )
            return

        if not text:
            await _send_no_valid_asr_result(
                websocket,
                trace_id=trace_id,
                session_id=session_id,
                round_seq=round_seq,
                round_id=round_id,
                playback_id=playback_id,
                stage="asr_empty",
                robot_id=robot_id,
                bot_id=bot_id,
                bot_name=bot_name,
                asr_time_ms=asr_time_ms,
                asr_trace_summary=asr_trace_summary,
                asr_metadata=asr_metadata,
            )
            logger.info("会话 %s: ASR 未返回文本，按无有效语音处理: metadata=%s", session_id, asr_metadata)
            return

        # 过滤无效识别结果（误触发）
        if not is_valid_recognition(text):
            await _send_no_valid_asr_result(
                websocket,
                trace_id=trace_id,
                session_id=session_id,
                round_seq=round_seq,
                round_id=round_id,
                playback_id=playback_id,
                stage="asr_invalid",
                robot_id=robot_id,
                bot_id=bot_id,
                bot_name=bot_name,
                asr_time_ms=asr_time_ms,
                asr_trace_summary=asr_trace_summary,
                asr_metadata=asr_metadata,
                text=text,
            )
            logger.info(f"会话 {session_id}: 识别结果无效，已过滤: '{text}', metadata={asr_metadata}")
            return

        # 发送识别结果
        await _send_valid_asr_result(
            websocket,
            trace_id=trace_id,
            session_id=session_id,
            round_seq=round_seq,
            round_id=round_id,
            playback_id=playback_id,
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            text=text,
            asr_time_ms=asr_time_ms,
            asr_trace_summary=asr_trace_summary,
            asr_metadata=asr_metadata,
        )

        # 2. LLM + TTS 流式处理（直接使用识别文本，不再检测唤醒词）
        await _stream_llm_tts_for_asr_result(
            text=text,
            session_id=session_id,
            websocket=websocket,
            bot_id=bot_id,
            bot_tts_settings=bot_tts_settings,
            trace=trace,
            asr_metadata=asr_metadata,
        )

    except Exception as e:
        _emit_process_error_trace(
            trace_id=trace_id,
            session_id=session_id,
            round_seq=round_seq,
            trace=trace,
            error=e,
        )
        logger.error(f"会话 {session_id}: 处理音频失败 - {e}")
        await send_error(websocket, "PROCESS_FAILED", str(e))


async def handle_client_event(
    websocket: WebSocket,
    session_id: str,
    data: dict,
    *,
    trace_id: str | None = None,
    round_id: str | None = None,
    playback_id: str | None = None,
    round_seq: int | None = None,
):
    """处理客户端状态事件播报，绕过 ASR/LLM/Router/历史，只走 TTS。"""
    trace_context = session_manager.get_trace_context(session_id)
    trace = _build_turn_trace(
        data,
        trace_context,
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
        round_seq=round_seq,
    )
    round_id = trace["round_id"]
    playback_id = trace["playback_id"]
    try:
        session_manager.touch_session(session_id)
        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            stage="client_event_received",
            robot_id=trace.get("robot_id"),
            bot_id=trace.get("bot_id"),
            bot_name=trace.get("bot_name"),
            summary=_build_client_event_received_summary(data),
        )
        if await _send_register_required_if_unregistered(
            websocket,
            session_id=session_id,
            trace_id=trace_id,
            round_seq=round_seq,
        ):
            return

        session_manager.set_interrupted(session_id, False)
        event_type = str(data.get("event") or "").strip()
        if event_type not in CLIENT_EVENT_TYPES:
            await send_error(websocket, "INVALID_EVENT", f"未知客户端事件: {event_type}")
            return

        phrase = pick_client_event_phrase(event_type, session_id)
        runtime_context = await _resolve_turn_runtime_context(session_id, data, trace)
        robot_id = runtime_context["robot_id"]
        bot_id = runtime_context["bot_id"]
        bot_name = runtime_context["bot_name"]
        bot_tts_settings = runtime_context["bot_tts_settings"]

        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            stage="client_event_phrase_selected",
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            summary=_build_client_event_phrase_summary(event_type, phrase, data),
        )
        logger.info(
            "会话 %s: 客户端事件播报 event=%s phrase=%r bot_id=%s bot_name=%s robot_id=%s",
            session_id,
            event_type,
            phrase,
            bot_id,
            bot_name,
            robot_id,
        )

        await process_direct_tts_stream(
            phrase,
            session_id,
            websocket,
            bot_id,
            bot_tts_settings,
            trace=trace,
            event_type=event_type,
            exit_after=(event_type == "sleep_exit"),
        )

    except Exception as e:
        _emit_process_error_trace(
            trace_id=trace_id,
            session_id=session_id,
            round_seq=round_seq,
            trace=trace,
            error=e,
        )
        logger.error("会话 %s: 处理客户端事件失败 - %s", session_id, e)
        await send_error(websocket, "PROCESS_FAILED", str(e))


async def handle_text(
    websocket: WebSocket,
    session_id: str,
    data: dict,
    *,
    trace_id: str | None = None,
    round_id: str | None = None,
    playback_id: str | None = None,
    round_seq: int | None = None,
):
    """处理客户端直发文本消息，用于环境感知等绕过 ASR 的入口。"""
    trace_context = session_manager.get_trace_context(session_id)
    trace = _build_turn_trace(
        data,
        trace_context,
        trace_id=trace_id,
        round_id=round_id,
        playback_id=playback_id,
        round_seq=round_seq,
    )
    round_id = trace["round_id"]
    playback_id = trace["playback_id"]
    try:
        session_manager.touch_session(session_id)
        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            stage="text_received",
            robot_id=trace.get("robot_id"),
            bot_id=trace.get("bot_id"),
            bot_name=trace.get("bot_name"),
        )
        # 文本直发是新协议入口，只允许完成 Robot 注册的客户端使用。
        # 旧协议兼容只保留在 audio 路径，避免外部 WS 客户端只带 bot_id 就绕过 robot 绑定。
        if await _send_register_required_if_unregistered(
            websocket,
            session_id=session_id,
            trace_id=trace_id,
            round_seq=round_seq,
        ):
            return

        session_manager.set_interrupted(session_id, False)

        content, validation_error = _extract_direct_text_content(data)
        if validation_error:
            trace_recorder.emit(
                trace_id,
                session_id=session_id,
                round_seq=round_seq,
                stage="invalid_text",
                status="error",
                error=validation_error,
            )
            await send_error(websocket, "INVALID_DATA", validation_error)
            return

        runtime_context = await _resolve_turn_runtime_context(session_id, data, trace)
        robot_id = runtime_context["robot_id"]
        bot_id = runtime_context["bot_id"]
        bot_name = runtime_context["bot_name"]
        bot_tts_settings = runtime_context["bot_tts_settings"]

        trace_recorder.emit(
            trace_id,
            session_id=session_id,
            round_seq=round_seq,
            stage="direct_text_ready",
            robot_id=robot_id,
            bot_id=bot_id,
            bot_name=bot_name,
            summary=_build_direct_text_summary(content, data),
        )
        logger.info(
            "会话 %s: 收到文本直发输入 - trace_id=%s, content=%r, bot_id=%s, bot_name=%s, robot_id=%s",
            session_id,
            trace_id,
            content,
            bot_id,
            bot_name,
            robot_id,
        )

        await send_message(
            websocket,
            "text",
            content=content,
            asr_time_ms=None,
            asr_metadata=_build_direct_text_asr_metadata(data),
            round_id=round_id,
        )
        await send_message(websocket, "status", message="思考中...")

        if await _try_process_complex_workflow(
            text=content,
            session_id=session_id,
            websocket=websocket,
            bot_id=bot_id,
            bot_tts_settings=bot_tts_settings,
            trace=trace,
        ):
            return

        await process_llm_tts_stream(
            content,
            session_id,
            websocket,
            bot_id,
            bot_tts_settings,
            trace=trace,
            history_query=content,
        )

    except Exception as e:
        _emit_process_error_trace(
            trace_id=trace_id,
            session_id=session_id,
            round_seq=round_seq,
            trace=trace,
            error=e,
        )
        logger.error(f"会话 {session_id}: 处理文本直发失败 - {e}")
        await send_error(websocket, "PROCESS_FAILED", str(e))


# ============ 启动服务 ============

if __name__ == "__main__":
    import uvicorn

    uvicorn_loop = "asyncio"
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        uvicorn_loop = "uvloop"
    except ImportError:
        logger.warning("未检测到 uvloop，回退到 asyncio 事件循环")

    logger.info("=" * 50)
    logger.info("FastAPI Gateway 启动")
    logger.info(f"监听地址: {GATEWAY_HOST}:{GATEWAY_PORT}")
    gateway_settings = get_gateway_settings()
    logger.info(f"STT 服务: {gateway_settings['stt_service_url']}")
    logger.info(f"LLM 服务: {gateway_settings['llm_service_url']}")
    logger.info(f"TTS 服务: {gateway_settings['tts_service_url']}")
    logger.info(f"最大连接数: {gateway_settings['max_connections']}")
    logger.info(f"事件循环: {uvicorn_loop}")
    logger.info("=" * 50)

    uvicorn.run(
        app,
        host=GATEWAY_HOST,
        port=GATEWAY_PORT,
        log_level="info",
        loop=uvicorn_loop,
        log_config=None,
    )
