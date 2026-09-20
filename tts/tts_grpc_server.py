import grpc
from concurrent import futures
import asyncio
import json
import logging
import queue
import threading
import time
import sys
import os

import websocket

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts import tts_service_pb2
from tts import tts_service_pb2_grpc
from tts.audio_resampler import resample_pcm16le
from tts.config_admin_http import ConfigAdminHTTPServer
from tts.runtime_state import TTSRuntimeState
from tts.text_normalizer import clean_text_for_tts
from server_config.models import TTSProfileConfig
from config import (
    CONFIG_DATABASE_URL,
    LOCAL_QWEN3_TTS_CONNECT_TIMEOUT_SEC,
    LOCAL_QWEN3_TTS_INSTRUCTIONS,
    LOCAL_QWEN3_TTS_LANGUAGE,
    LOCAL_QWEN3_TTS_RECV_TIMEOUT_SEC,
    LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE,
    LOCAL_QWEN3_TTS_SUPPORTED_VOICES,
    LOCAL_QWEN3_TTS_VOICE,
    QWEN3_TTS_BASE_API_KEY,
    QWEN3_TTS_BASE_MODEL,
    QWEN3_TTS_BASE_WS_URL,
    QWEN3_TTS_CUSTOM_VOICE_API_KEY,
    QWEN3_TTS_CUSTOM_VOICE_MODEL,
    QWEN3_TTS_CUSTOM_VOICE_WS_URL,
    TTS_ADMIN_PORT,
    TTS_ADMIN_BIND_HOST,
    TTS_GRPC_BIND_HOST,
    TTS_GRPC_SERVER_PORT,
    TTS_GRPC_MAX_WORKERS,
    TTS_AUDIO_QUEUE_MAXSIZE,
    TTS_AUDIO_QUEUE_PUT_TIMEOUT_SEC,
    is_missing_secret,
)
from voice_logging import configure_logging

configure_logging("tts", force=True)
logger = logging.getLogger(__name__)

tts_runtime_state = TTSRuntimeState(database_url=CONFIG_DATABASE_URL) if CONFIG_DATABASE_URL else None
_tts_admin_server = None

LOCAL_QWEN3_TTS_OUTPUT_SAMPLE_RATE = LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE
TTS_OUTPUT_SAMPLE_RATE = 16000
LOCAL_QWEN3_TTS_VOICE_MAP = {voice.lower(): voice for voice in LOCAL_QWEN3_TTS_SUPPORTED_VOICES}


def _epoch_ms() -> float:
    return time.time() * 1000.0


def _elapsed_ms(start_epoch_ms: float | None, end_epoch_ms: float | None = None) -> float:
    if not start_epoch_ms:
        return 0.0
    if end_epoch_ms is not None and end_epoch_ms <= 0:
        return 0.0
    return (end_epoch_ms if end_epoch_ms is not None else _epoch_ms()) - start_epoch_ms


class TTSAudioQueueFullError(RuntimeError):
    """Raised when provider audio arrives faster than the gRPC stream can drain it."""


def _format_tts_callback_error(error) -> str:
    if isinstance(error, TTSAudioQueueFullError):
        return str(error)
    if isinstance(error, dict):
        message = error.get("message") or error.get("msg") or error.get("code")
        if message:
            return str(message)
    return str(error)


def _grpc_status_for_tts_callback_error(error) -> grpc.StatusCode:
    if isinstance(error, TTSAudioQueueFullError):
        return grpc.StatusCode.RESOURCE_EXHAUSTED
    return grpc.StatusCode.INTERNAL


def _resolve_local_qwen3_voice(requested_voice: str | None) -> str:
    fallback_voice = (
        LOCAL_QWEN3_TTS_VOICE_MAP.get((LOCAL_QWEN3_TTS_VOICE or "").strip().lower())
        or LOCAL_QWEN3_TTS_SUPPORTED_VOICES[0]
    )
    requested = (requested_voice or "").strip()
    if not requested:
        return fallback_voice

    canonical = LOCAL_QWEN3_TTS_VOICE_MAP.get(requested.lower())
    if canonical:
        return canonical

    logger.warning(
        "TTS voice=%r 不支持本地 Qwen3，回退到 %s；supported=%s",
        requested,
        fallback_voice,
        ",".join(LOCAL_QWEN3_TTS_SUPPORTED_VOICES),
    )
    return fallback_voice


def _file_uri_from_path(path: str) -> str:
    raw_path = (path or "").strip()
    if raw_path.startswith(("file://", "http://", "https://", "data:")):
        return raw_path
    if not raw_path.startswith("/"):
        raise ValueError(f"Qwen3 Base ref_audio path 必须是绝对路径: {raw_path}")
    return f"file://{raw_path}"


def _default_tts_profile(profile_id: str) -> TTSProfileConfig:
    return TTSProfileConfig(
        tts_id=profile_id or "default_tts_profile",
        tts_name="Default CustomVoice",
        provider_type="qwen3_custom_voice",
        speed=1.0,
        provider_config={
            "voice": LOCAL_QWEN3_TTS_VOICE,
            "instruct": LOCAL_QWEN3_TTS_INSTRUCTIONS,
        },
        enabled=True,
    )


def _resolve_tts_profile(text_chunk) -> TTSProfileConfig:
    config = getattr(text_chunk, "config", None)
    profile_id = str(getattr(config, "tts_profile_id", "") or "").strip()
    if not profile_id:
        raise ValueError("TTS 请求缺少 tts_profile_id")
    if tts_runtime_state is not None:
        return tts_runtime_state.get_tts_profile(profile_id)
    if profile_id == "default_tts_profile":
        return _default_tts_profile(profile_id)
    raise ValueError(f"TTS runtime 未启用，无法解析 TTS Profile: {profile_id}")


def _build_provider_session(profile: TTSProfileConfig) -> tuple[dict[str, object], dict[str, object]]:
    if profile.provider_type == "qwen3_custom_voice":
        voice = _resolve_local_qwen3_voice(profile.voice)
        connection = {
            "provider_type": profile.provider_type,
            "tts_profile_id": profile.tts_id,
            "tts_name": profile.tts_name,
            "ws_url": QWEN3_TTS_CUSTOM_VOICE_WS_URL,
            "api_key": QWEN3_TTS_CUSTOM_VOICE_API_KEY,
            "model": QWEN3_TTS_CUSTOM_VOICE_MODEL,
        }
        payload = {
            "type": "session.config",
            "model": QWEN3_TTS_CUSTOM_VOICE_MODEL,
            "voice": voice,
            "language": LOCAL_QWEN3_TTS_LANGUAGE,
            "response_format": "pcm",
            "task_type": "CustomVoice",
            "instructions": profile.instruct,
            "speed": profile.speed,
            "stream_audio": True,
        }
        return connection, payload

    if profile.provider_type == "qwen3_base":
        connection = {
            "provider_type": profile.provider_type,
            "tts_profile_id": profile.tts_id,
            "tts_name": profile.tts_name,
            "ws_url": QWEN3_TTS_BASE_WS_URL,
            "api_key": QWEN3_TTS_BASE_API_KEY,
            "model": QWEN3_TTS_BASE_MODEL,
        }
        payload = {
            "type": "session.config",
            "model": QWEN3_TTS_BASE_MODEL,
            "language": LOCAL_QWEN3_TTS_LANGUAGE,
            "response_format": "pcm",
            "task_type": "Base",
            "ref_audio": _file_uri_from_path(profile.path),
            "ref_text": profile.content,
            "speed": profile.speed,
            "stream_audio": True,
        }
        return connection, payload

    raise ValueError(f"不支持的 TTS provider_type: {profile.provider_type}")


class LocalQwen3TTSBridge:
    """Bridge local Qwen3-TTS WS events into the gRPC audio queue."""

    def __init__(
        self,
        audio_queue,
        *,
        source_sample_rate: int = LOCAL_QWEN3_TTS_OUTPUT_SAMPLE_RATE,
        request_started_epoch_ms: float | None = None,
    ):
        self.audio_queue = audio_queue
        self.source_sample_rate = source_sample_rate
        self.request_started_epoch_ms = request_started_epoch_ms or _epoch_ms()
        self.trace_id = ""
        self.round_id = ""
        self.playback_id = ""
        self.tts_profile_id = ""
        self.tts_name = ""
        self.provider_type = ""
        self.gateway_first_text_send_epoch_ms = 0.0
        self.first_text_received_epoch_ms = 0.0
        self.first_text_sent_epoch_ms = 0.0
        self.first_pcm_epoch_ms = 0.0
        self.complete_event = threading.Event()
        self.error = None
        self.delta_chunks = 0
        self.delta_bytes = 0
        self.delta_last_at = None
        self.delta_gap_max_ms = 0.0
        self.delta_gap_excess_max_ms = 0.0
        self.delta_gap_excess_count = 0
        self.empty_binary_payloads = 0
        self.stop_requested = False
        self.submitted_text_chunks = 0

    def bind_trace(self, text_chunk) -> None:
        self.trace_id = getattr(text_chunk, "trace_id", "") or self.trace_id
        self.round_id = getattr(text_chunk, "round_id", "") or self.round_id
        self.playback_id = getattr(text_chunk, "playback_id", "") or self.playback_id
        gateway_send_epoch_ms = float(getattr(text_chunk, "gateway_send_epoch_ms", 0.0) or 0.0)
        if gateway_send_epoch_ms and not self.gateway_first_text_send_epoch_ms:
            self.gateway_first_text_send_epoch_ms = gateway_send_epoch_ms

    def mark_first_text_received(self, text_chunk) -> None:
        self.bind_trace(text_chunk)
        if self.first_text_received_epoch_ms:
            return
        self.first_text_received_epoch_ms = _epoch_ms()
        logger.info(
            "TTS 首段文本到达: trace_id=%s round_id=%s playback_id=%s since_request=%.1fms gateway_to_tts=%.1fms",
            self.trace_id or "-",
            self.round_id or "-",
            self.playback_id or "-",
            _elapsed_ms(self.request_started_epoch_ms, self.first_text_received_epoch_ms),
            (
                self.first_text_received_epoch_ms - self.gateway_first_text_send_epoch_ms
                if self.gateway_first_text_send_epoch_ms
                else 0.0
            ),
        )

    def bind_profile(self, profile: TTSProfileConfig) -> None:
        self.tts_profile_id = profile.tts_id
        self.tts_name = profile.tts_name
        self.provider_type = profile.provider_type

    def mark_first_text_sent(self) -> None:
        if not self.first_text_sent_epoch_ms:
            self.first_text_sent_epoch_ms = _epoch_ms()

    def add_audio(self, audio_data: bytes) -> None:
        if self.error is not None or self.stop_requested:
            return
        if not audio_data:
            self.empty_binary_payloads += 1
            logger.debug(
                "忽略 Local Qwen3 TTS 空二进制音频包: trace_id=%s round_id=%s count=%s",
                self.trace_id or "-",
                self.round_id or "-",
                self.empty_binary_payloads,
            )
            return

        now = time.time()
        now_epoch_ms = now * 1000.0
        if not self.first_pcm_epoch_ms:
            self.first_pcm_epoch_ms = now_epoch_ms
            logger.info(
                "TTS 首个 Local Qwen3 PCM: trace_id=%s round_id=%s playback_id=%s since_request=%.1fms first_text_to_pcm=%.1fms bytes=%s",
                self.trace_id or "-",
                self.round_id or "-",
                self.playback_id or "-",
                _elapsed_ms(self.request_started_epoch_ms, self.first_pcm_epoch_ms),
                (
                    self.first_pcm_epoch_ms - self.first_text_received_epoch_ms
                    if self.first_text_received_epoch_ms
                    else 0.0
                ),
                len(audio_data),
            )
        chunk_duration_ms = len(audio_data) / 2 / self.source_sample_rate * 1000.0
        if self.delta_last_at is not None:
            gap_ms = (now - self.delta_last_at) * 1000.0
            self.delta_gap_max_ms = max(self.delta_gap_max_ms, gap_ms)
            gap_excess_ms = max(0.0, gap_ms - chunk_duration_ms)
            self.delta_gap_excess_max_ms = max(self.delta_gap_excess_max_ms, gap_excess_ms)
            if gap_excess_ms > 20.0:
                self.delta_gap_excess_count += 1
        self.delta_last_at = now
        self.delta_chunks += 1
        self.delta_bytes += len(audio_data)
        try:
            self.audio_queue.put(audio_data, timeout=TTS_AUDIO_QUEUE_PUT_TIMEOUT_SEC)
        except queue.Full:
            maxsize = getattr(self.audio_queue, "maxsize", 0)
            self.mark_error(
                TTSAudioQueueFullError(
                    "TTS audio queue full "
                    f"(maxsize={maxsize}, put_timeout={TTS_AUDIO_QUEUE_PUT_TIMEOUT_SEC:.3f}s)"
                )
            )

    def mark_complete(self) -> None:
        self.complete_event.set()

    def mark_error(self, error) -> None:
        if self.error is None:
            self.error = error
            logger.error("Local Qwen3 TTS 错误: %s", _format_tts_callback_error(error))
        self.complete_event.set()

    def request_stop(self) -> None:
        self.stop_requested = True
        self.complete_event.set()


class TTSServiceServicer(tts_service_pb2_grpc.TTSServiceServicer):
    """TTS gRPC 服务实现"""

    def __init__(self, *, ws_connect=None):
        self._ws_connect = ws_connect or websocket.create_connection

    def StreamTextToSpeech(self, request_iterator, context):
        """
        双向流式 TTS 服务

        Args:
            request_iterator: 客户端发送的文本流
            context: gRPC 上下文

        Yields:
            AudioChunk: 音频数据块
        """
        ws = None
        bridge = None
        text_thread = None
        receive_thread = None
        had_error = False
        stream_status_error = None
        ws_closed = False
        ws_lock = threading.Lock()
        threads_joined = False

        def close_ws(reason: str = ""):
            nonlocal ws_closed
            if bridge is not None:
                bridge.request_stop()
            with ws_lock:
                if ws_closed:
                    return
                ws_closed = True
                local_ws = ws
            if local_ws is not None:
                try:
                    local_ws.close()
                    logger.info("Local Qwen3 TTS WS 连接已关闭%s", reason)
                except Exception as e:
                    logger.warning("关闭 Local Qwen3 TTS WS 连接失败: %s", e)

        def join_worker_threads():
            nonlocal threads_joined
            if threads_joined:
                return
            threads_joined = True
            if text_thread is not None:
                text_thread.join(timeout=5)
            if receive_thread is not None:
                receive_thread.join(timeout=5)
            if text_thread is not None and text_thread.is_alive():
                logger.warning("文本处理线程在 5 秒后仍在运行，可能存在线程泄漏")
            if receive_thread is not None and receive_thread.is_alive():
                logger.warning("TTS 接收线程在 5 秒后仍在运行，可能存在线程泄漏")
            if (
                text_thread is not None
                and not text_thread.is_alive()
                and (receive_thread is None or not receive_thread.is_alive())
            ):
                logger.info("TTS 请求处理完成")

        try:
            request_started_epoch_ms = _epoch_ms()
            logger.info("收到新的 TTS 流式请求 request_epoch_ms=%.1f", request_started_epoch_ms)
            if tts_runtime_state:
                tts_runtime_state.mark_request_started()

            # 创建音频队列（限制大小避免内存无限增长）
            audio_queue = queue.Queue(maxsize=TTS_AUDIO_QUEUE_MAXSIZE)
            bridge = LocalQwen3TTSBridge(
                audio_queue,
                request_started_epoch_ms=request_started_epoch_ms,
            )
            add_cancel_callback = getattr(context, "add_callback", None)
            if callable(add_cancel_callback):
                def _cancel_local_qwen3_ws():
                    logger.info("TTS gRPC context 已取消，关闭 Local Qwen3 TTS WS")
                    close_ws("（gRPC取消）")

                if add_cancel_callback(_cancel_local_qwen3_ws) is False:
                    close_ws("（gRPC已取消）")

            def get_or_start_ws(connection: dict[str, object]):
                nonlocal ws, receive_thread
                if ws is not None and not ws_closed:
                    return ws
                provider_type = str(connection.get("provider_type") or "unknown")
                api_key = str(connection.get("api_key") or "")
                if is_missing_secret(api_key):
                    raise ValueError(f"{provider_type} TTS API_KEY 未正确配置，TTS 服务无法启动")
                with ws_lock:
                    if ws_closed:
                        raise RuntimeError("TTS request already closed")
                    if ws is not None:
                        return ws
                    ws = self._connect_local_qwen3_ws(connection)
                    receive_thread = threading.Thread(
                        target=self._receive_local_qwen3_audio,
                        args=(ws, bridge),
                        daemon=True,
                    )
                    receive_thread.start()
                    return ws

            # 启动文本处理线程（会在第一个消息中获取配置）
            text_thread = threading.Thread(
                target=self._process_text_stream,
                args=(request_iterator, get_or_start_ws, context, bridge),
                daemon=True,
            )
            text_thread.start()

            grpc_yield_chunks = 0
            grpc_yield_bytes = 0
            grpc_yield_last_at = None
            grpc_yield_gap_max_ms = 0.0
            grpc_yield_gap_excess_max_ms = 0.0
            grpc_yield_gap_excess_count = 0

            def mark_callback_error(error):
                nonlocal had_error, stream_status_error
                if stream_status_error is not None:
                    return
                stream_status_error = error
                had_error = True
                details = _format_tts_callback_error(error)
                context.set_code(_grpc_status_for_tts_callback_error(error))
                context.set_details(details)
                if tts_runtime_state:
                    tts_runtime_state.mark_error(details)
                logger.warning("TTS 流式请求异常结束: %s", details)
                close_ws("（回调错误）")

            # 流式返回音频
            while True:
                # 检查客户端是否断开（被 cancel）
                if not context.is_active():
                    logger.info("客户端已取消，停止 TTS")
                    close_ws("（客户端取消）")
                    break

                if bridge.error is not None:
                    mark_callback_error(bridge.error)
                    break

                # 从队列获取音频数据
                try:
                    audio_data = audio_queue.get(timeout=0.05)
                except queue.Empty:
                    if bridge.complete_event.is_set():
                        if bridge.error is not None:
                            mark_callback_error(bridge.error)
                        elif bridge.submitted_text_chunks > 0 and bridge.delta_chunks == 0:
                            mark_callback_error("Local Qwen3 TTS completed without audio")
                        else:
                            logger.info("TTS 完成，准备发送最后标记")
                        break
                    continue

                if bridge.error is not None:
                    mark_callback_error(bridge.error)
                    break

                now = time.time()
                now_epoch_ms = now * 1000.0
                output_audio_data = resample_pcm16le(
                    audio_data,
                    LOCAL_QWEN3_TTS_OUTPUT_SAMPLE_RATE,
                    TTS_OUTPUT_SAMPLE_RATE,
                )
                chunk_duration_ms = (
                    len(output_audio_data) / 2 / TTS_OUTPUT_SAMPLE_RATE * 1000.0
                    if output_audio_data
                    else 0.0
                )
                if grpc_yield_last_at is not None:
                    gap_ms = (now - grpc_yield_last_at) * 1000.0
                    grpc_yield_gap_max_ms = max(grpc_yield_gap_max_ms, gap_ms)
                    gap_excess_ms = max(0.0, gap_ms - chunk_duration_ms)
                    grpc_yield_gap_excess_max_ms = max(grpc_yield_gap_excess_max_ms, gap_excess_ms)
                    if gap_excess_ms > 20.0:
                        grpc_yield_gap_excess_count += 1
                grpc_yield_last_at = now
                grpc_yield_chunks += 1
                grpc_yield_bytes += len(output_audio_data)

                # 返回音频块
                yield tts_service_pb2.AudioChunk(
                    audio_data=output_audio_data,
                    sample_rate=TTS_OUTPUT_SAMPLE_RATE,
                    channels=1,
                    sample_width=2,
                    is_final=False,
                    trace_id=bridge.trace_id,
                    round_id=bridge.round_id,
                    playback_id=bridge.playback_id,
                    tts_server_receive_epoch_ms=bridge.request_started_epoch_ms,
                    tts_first_text_receive_epoch_ms=bridge.first_text_received_epoch_ms,
                    tts_first_text_send_epoch_ms=bridge.first_text_sent_epoch_ms,
                    tts_first_pcm_epoch_ms=bridge.first_pcm_epoch_ms,
                    tts_grpc_yield_epoch_ms=now_epoch_ms,
                    tts_internal_first_pcm_ms=(
                        bridge.first_pcm_epoch_ms - bridge.request_started_epoch_ms
                        if bridge.first_pcm_epoch_ms
                        else 0.0
                    ),
                    tts_first_text_to_first_pcm_ms=(
                        bridge.first_pcm_epoch_ms - bridge.first_text_received_epoch_ms
                        if bridge.first_pcm_epoch_ms and bridge.first_text_received_epoch_ms
                        else 0.0
                    ),
                    tts_request_to_grpc_yield_ms=now_epoch_ms - bridge.request_started_epoch_ms,
                )

            # 只在正常完成时发送最后标记（被取消时不发）
            if context.is_active() and stream_status_error is None:
                logger.info("发送最后标记 is_final=True")
                yield tts_service_pb2.AudioChunk(
                    audio_data=b'',
                    sample_rate=TTS_OUTPUT_SAMPLE_RATE,
                    channels=1,
                    sample_width=2,
                    is_final=True,
                    trace_id=bridge.trace_id if bridge is not None else "",
                    round_id=bridge.round_id if bridge is not None else "",
                    playback_id=bridge.playback_id if bridge is not None else "",
                )

            qwen_duration_sec = (
                bridge.delta_bytes / 2 / LOCAL_QWEN3_TTS_OUTPUT_SAMPLE_RATE
                if bridge.delta_bytes
                else 0.0
            )
            grpc_duration_sec = grpc_yield_bytes / 2 / TTS_OUTPUT_SAMPLE_RATE if grpc_yield_bytes else 0.0
            logger.info(
                "TTS 节奏统计: "
                f"trace_id={bridge.trace_id or '-'}, "
                f"profile={bridge.tts_profile_id or '-'}, "
                f"provider={bridge.provider_type or '-'}, "
                f"wall={_elapsed_ms(bridge.request_started_epoch_ms):.1f}ms, "
                f"first_text={_elapsed_ms(bridge.request_started_epoch_ms, bridge.first_text_received_epoch_ms):.1f}ms, "
                f"first_pcm={_elapsed_ms(bridge.request_started_epoch_ms, bridge.first_pcm_epoch_ms):.1f}ms, "
                f"local_qwen3={bridge.delta_chunks}块/{bridge.delta_bytes}B/{qwen_duration_sec:.2f}s(audio), "
                f"local_qwen3_gap_max={bridge.delta_gap_max_ms:.1f}ms, "
                f"local_qwen3_gap_excess_max={bridge.delta_gap_excess_max_ms:.1f}ms, "
                f"local_qwen3_gap_excess_count={bridge.delta_gap_excess_count}, "
                f"local_qwen3_empty_binary_payloads={bridge.empty_binary_payloads}, "
                f"grpc={grpc_yield_chunks}块/{grpc_yield_bytes}B/{grpc_duration_sec:.2f}s(audio), "
                f"grpc_gap_max={grpc_yield_gap_max_ms:.1f}ms, "
                f"grpc_gap_excess_max={grpc_yield_gap_excess_max_ms:.1f}ms, "
                f"grpc_gap_excess_count={grpc_yield_gap_excess_count}"
            )

            close_ws("（请求结束）")

            join_worker_threads()

        except Exception as e:
            logger.error(f"TTS 服务错误: {e}")
            had_error = True
            if tts_runtime_state:
                tts_runtime_state.mark_error(str(e))
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))

        finally:
            # 清理资源：关闭本地 WS，避免超时后后台线程继续存活。
            close_ws()
            join_worker_threads()
            if tts_runtime_state and not had_error:
                tts_runtime_state.clear_error()
            logger.info("清理 TTS 资源完成")

    def _connect_local_qwen3_ws(self, connection: dict[str, object]):
        ws_url = str(connection.get("ws_url") or "")
        api_key = str(connection.get("api_key") or "")
        model = str(connection.get("model") or "")
        provider_type = str(connection.get("provider_type") or "")
        tts_profile_id = str(connection.get("tts_profile_id") or "")
        tts_name = str(connection.get("tts_name") or "")
        headers = [f"Authorization: Bearer {api_key}"]
        logger.info(
            "连接 Local Qwen3 TTS WS: provider=%s profile=%s tts_name=%s url=%s model=%s source_rate=%s connect_timeout=%.1fs recv_timeout=%.1fs",
            provider_type,
            tts_profile_id,
            tts_name,
            ws_url,
            model,
            LOCAL_QWEN3_TTS_OUTPUT_SAMPLE_RATE,
            LOCAL_QWEN3_TTS_CONNECT_TIMEOUT_SEC,
            LOCAL_QWEN3_TTS_RECV_TIMEOUT_SEC,
        )
        ws = self._ws_connect(
            ws_url,
            header=headers,
            timeout=LOCAL_QWEN3_TTS_CONNECT_TIMEOUT_SEC,
        )
        if hasattr(ws, "settimeout"):
            ws.settimeout(LOCAL_QWEN3_TTS_RECV_TIMEOUT_SEC)
        return ws

    def _receive_local_qwen3_audio(self, ws, bridge: LocalQwen3TTSBridge):
        try:
            while not bridge.complete_event.is_set():
                message = ws.recv()
                if message is None:
                    if not bridge.stop_requested:
                        bridge.mark_error("Local Qwen3 TTS WS closed before session.done")
                    return
                if isinstance(message, str):
                    self._handle_local_qwen3_event(message, bridge)
                    continue
                if isinstance(message, bytearray):
                    message = bytes(message)
                if isinstance(message, bytes):
                    bridge.add_audio(message)
                    continue
                logger.debug("忽略未知 Local Qwen3 TTS WS 消息类型: %s", type(message).__name__)
        except websocket.WebSocketConnectionClosedException:
            if not bridge.stop_requested and bridge.error is None:
                bridge.mark_error("Local Qwen3 TTS WS closed before session.done")
        except Exception as e:
            if not bridge.stop_requested and bridge.error is None:
                bridge.mark_error(e)

    def _handle_local_qwen3_event(self, raw_message: str, bridge: LocalQwen3TTSBridge) -> None:
        try:
            event = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.debug("忽略非 JSON Local Qwen3 TTS 文本消息: %s", raw_message[:120])
            return

        event_type = event.get("type")
        if event_type == "session.done":
            logger.info("Local Qwen3 TTS session.done")
            bridge.mark_complete()
            return
        if event_type == "error":
            bridge.mark_error(event.get("error") or event)
            return
        logger.debug("Local Qwen3 TTS event: %s", event)

    def _send_local_qwen3_event(self, ws, payload: dict) -> None:
        ws.send(json.dumps(payload, ensure_ascii=False))

    def _process_text_stream(self, request_iterator, get_ws, context, bridge: LocalQwen3TTSBridge):
        """
        处理客户端发送的文本流

        Args:
            request_iterator: 文本流迭代器
            get_ws: 延迟创建 Local Qwen3 TTS WebSocket 的回调
            context: gRPC 上下文
            bridge: Local Qwen3 TTS bridge
        """
        try:
            get_ws_callback = get_ws if callable(get_ws) else (lambda _connection: get_ws)
            ws = None
            first_message = True
            sent_text_chunks = 0
            session_configured = False
            input_done_sent = False

            def send_session_config(text_chunk):
                nonlocal session_configured, ws
                if session_configured:
                    return
                profile = _resolve_tts_profile(text_chunk)
                bridge.bind_profile(profile)
                connection, payload = _build_provider_session(profile)
                ws = get_ws_callback(connection)
                logger.info(
                    "TTS 配置: provider=%s profile=%s tts_name=%s model=%s speed=%.2f text_submit=incremental_char",
                    profile.provider_type,
                    profile.tts_id,
                    profile.tts_name,
                    payload.get("model"),
                    profile.speed,
                )
                self._send_local_qwen3_event(ws, payload)
                session_configured = True

            def send_text_delta(text_delta: str):
                nonlocal sent_text_chunks, ws
                if not text_delta:
                    return
                if not session_configured:
                    raise RuntimeError("TTS session.config was not sent before input.text")
                if getattr(bridge, "error", None) is not None:
                    raise RuntimeError(_format_tts_callback_error(bridge.error))
                if not context.is_active():
                    logger.info("客户端断开，停止提交 TTS 文本")
                    return

                sent_text_chunks += 1
                if sent_text_chunks <= 5 or sent_text_chunks % 20 == 0:
                    logger.info(
                        "TTS 增量提交文本 #%s chars=%s - %s",
                        sent_text_chunks,
                        len(text_delta),
                        text_delta[:80],
                    )
                bridge.mark_first_text_sent()
                self._send_local_qwen3_event(ws, {"type": "input.text", "text": text_delta})
                bridge.submitted_text_chunks += 1

            def send_text_incrementally(text: str) -> None:
                for char in text:
                    send_text_delta(char)

            def send_input_done():
                nonlocal input_done_sent
                if input_done_sent:
                    return
                if ws is None:
                    return
                self._send_local_qwen3_event(ws, {"type": "input.done"})
                input_done_sent = True

            for text_chunk in request_iterator:
                if getattr(bridge, "error", None) is not None:
                    logger.warning("TTS 回调已标记错误，停止接收文本")
                    break

                # 第一个消息：读取配置并初始化 TTS
                if first_message:
                    bridge.bind_trace(text_chunk)
                    send_session_config(text_chunk)
                    first_message = False

                # 检查客户端是否断开
                if not context.is_active():
                    logger.info("客户端断开，停止接收文本")
                    break

                if text_chunk.text:
                    bridge.bind_trace(text_chunk)
                    cleaned_text = clean_text_for_tts(text_chunk.text)
                    if cleaned_text:
                        bridge.mark_first_text_received(text_chunk)
                        send_text_incrementally(cleaned_text)

                if text_chunk.is_final:
                    if not session_configured:
                        send_session_config(text_chunk)
                    logger.info("TTS 文本流结束，发送增量文本数=%s，发送 input.done", sent_text_chunks)
                    send_input_done()
                    break
            else:
                if session_configured and not input_done_sent:
                    logger.info("TTS 文本流自然结束，发送增量文本数=%s，发送 input.done", sent_text_chunks)
                    send_input_done()
                elif not session_configured:
                    logger.info("TTS 文本流为空，直接结束")
                    bridge.mark_complete()

        except Exception as e:
            logger.error(f"处理文本流错误: {e}")
            if getattr(bridge, "error", None) is None:
                bridge.mark_error(e)
            else:
                complete_event = getattr(bridge, "complete_event", None)
                if complete_event is not None:
                    complete_event.set()
        finally:
            pass


def serve(port=50052, max_workers=5, host: str = "127.0.0.1"):
    """
    启动 gRPC 服务器

    Args:
        port: 监听端口
        max_workers: 最大并发工作线程数（建议 3-10，根据 Qwen TTS API 限制调整）
    """
    # 配置线程池：限制并发数，避免资源耗尽
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        options=[
            ('grpc.max_send_message_length', 50 * 1024 * 1024),  # 50MB
            ('grpc.max_receive_message_length', 50 * 1024 * 1024),  # 50MB
            ('grpc.keepalive_time_ms', 30000),  # 30秒
            ('grpc.keepalive_timeout_ms', 10000),  # 10秒
        ]
    )
    tts_service_pb2_grpc.add_TTSServiceServicer_to_server(
        TTSServiceServicer(), server
    )

    server.add_insecure_port(f'{host}:{port}')
    server.start()
    global _tts_admin_server
    if tts_runtime_state:
        class _TTSAdminActions:
            def get_config_status(self):
                return tts_runtime_state.get_status()

            def validate_runtime_config(self, version: int | None = None):
                return asyncio.run(tts_runtime_state.validate(version))

            def reload_runtime_config(self, version: int | None = None):
                return asyncio.run(tts_runtime_state.reload(version))

        _tts_admin_server = ConfigAdminHTTPServer(TTS_ADMIN_BIND_HOST, TTS_ADMIN_PORT, _TTSAdminActions())
        _tts_admin_server.start()

    logger.info(f"TTS gRPC 服务已启动")
    logger.info(f"监听地址: {host}:{port}")
    logger.info(f"最大并发数: {max_workers}")
    if tts_runtime_state:
        status = tts_runtime_state.get_status()
        logger.info(
            "TTS runtime 已加载: source=%s version=%s model=%s profiles=%s",
            status["source"],
            status["config_version"],
            status["tts_settings"]["realtime_model"],
            status.get("tts_profile_count", 0),
        )
    logger.info(f"准备接收客户端请求...")

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("正在关闭服务器...")
        if _tts_admin_server:
            _tts_admin_server.stop()
        server.stop(0)


if __name__ == '__main__':
    serve(port=TTS_GRPC_SERVER_PORT, max_workers=TTS_GRPC_MAX_WORKERS, host=TTS_GRPC_BIND_HOST)
