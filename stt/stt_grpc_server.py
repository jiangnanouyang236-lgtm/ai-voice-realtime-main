"""
STT gRPC 服务器

提供基于 Qwen3-ASR 的语音识别服务
"""

import json
import grpc
from concurrent.futures import ThreadPoolExecutor
import logging
import sys
import os
import threading
import time

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stt import stt_service_pb2
from stt import stt_service_pb2_grpc
from stt.asr_providers import create_stt_provider
from config import STT_GRPC_BIND_HOST, STT_GRPC_MAX_WORKERS, STT_GRPC_SERVER_PORT
from voice_logging import configure_logging

configure_logging("stt", force=True)
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是有效整数，使用默认值 %s", name, value, default)
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是有效数字，使用默认值 %s", name, value, default)
        return default


def _default_max_concurrent_inferences() -> int:
    return max(1, _env_int("STT_MAX_CONCURRENT_INFERENCES", 8))


def _default_inference_queue_timeout_sec() -> float:
    return max(0.0, _env_float("STT_INFERENCE_QUEUE_TIMEOUT_SEC", 30.0))


class STTInferenceQueueTimeout(TimeoutError):
    pass


class STTRequestCancelled(Exception):
    pass


def _context_is_active(context) -> bool:
    if context is None:
        return True
    is_active = getattr(context, "is_active", None)
    if not callable(is_active):
        return True
    return bool(is_active())


def _to_text_response(result: dict) -> stt_service_pb2.TextResponse:
    return stt_service_pb2.TextResponse(
        text=result.get("text") or "",
        confidence=float(result.get("confidence") or 0.0),
        is_final=True,
        language=result.get("language") or "",
        emotion=result.get("emotion") or "",
        event_type=result.get("event_type") or "",
        raw_text=result.get("raw_text") or "",
        tags_json=json.dumps(result.get("tags") or [], ensure_ascii=False, default=str),
        metadata_json=json.dumps(result.get("metadata") or {}, ensure_ascii=False, default=str),
        confidence_source=result.get("confidence_source") or "unavailable",
    )


class STTServiceServicer(stt_service_pb2_grpc.STTServiceServicer):
    """STT gRPC 服务实现（带并发控制）"""

    def __init__(self, max_concurrent_inferences=None, stt_client=None, inference_queue_timeout_sec=None):
        """
        初始化 STT 服务

        Args:
            max_concurrent_inferences: 最大并发推理数；None 时按 provider/config 自动选择
            stt_client: 可注入的 ASR provider，测试或灰度时使用
            inference_queue_timeout_sec: 等待推理槽位的最长时间，避免请求无限排队
        """
        self.stt_client = stt_client or create_stt_provider()
        max_concurrent_inferences = (
            max_concurrent_inferences
            if max_concurrent_inferences is not None
            else _default_max_concurrent_inferences()
        )
        self.inference_queue_timeout_sec = (
            inference_queue_timeout_sec
            if inference_queue_timeout_sec is not None
            else _default_inference_queue_timeout_sec()
        )
        # 使用信号量限制并发推理数
        self.inference_semaphore = threading.Semaphore(max_concurrent_inferences)
        logger.info(
            "STT 服务已初始化: provider=%s, 最大并发推理数=%s, 队列等待超时=%.1fs",
            self.stt_client.__class__.__name__,
            max_concurrent_inferences,
            self.inference_queue_timeout_sec,
        )

    def _recognize_pcm_with_metrics(
        self,
        *,
        pcm_data: bytes,
        sample_rate: int,
        language: str | None = None,
        request_kind: str,
        context=None,
    ) -> dict:
        """Run provider inference while recording semaphore wait and inference time."""
        provider_name = self.stt_client.__class__.__name__
        wait_started = time.perf_counter()
        acquired = False
        while not acquired:
            if not _context_is_active(context):
                raise STTRequestCancelled("STT request cancelled while waiting for inference slot")
            acquired = self.inference_semaphore.acquire(timeout=0.05)
            if acquired:
                break
            waited_sec = time.perf_counter() - wait_started
            if self.inference_queue_timeout_sec > 0 and waited_sec >= self.inference_queue_timeout_sec:
                raise STTInferenceQueueTimeout(
                    f"STT inference queue wait exceeded {self.inference_queue_timeout_sec:.1f}s"
                )
        queue_wait_ms = (time.perf_counter() - wait_started) * 1000.0
        inference_started = time.perf_counter()
        try:
            result = self.stt_client.recognize_from_pcm(
                pcm_data=pcm_data,
                sample_rate=sample_rate,
                language=language,
            )
            inference_ms = (time.perf_counter() - inference_started) * 1000.0
        except Exception:
            inference_ms = (time.perf_counter() - inference_started) * 1000.0
            logger.exception(
                "STT 推理失败: kind=%s provider=%s audio_bytes=%s sample_rate=%s "
                "queue_wait=%.1fms inference=%.1fms",
                request_kind,
                provider_name,
                len(pcm_data),
                sample_rate,
                queue_wait_ms,
                inference_ms,
            )
            raise
        finally:
            if acquired:
                self.inference_semaphore.release()

        result = dict(result or {})
        metadata = dict(result.get("metadata") or {})
        metadata.update({
            "stt_provider": provider_name,
            "stt_request_kind": request_kind,
            "asr_queue_wait_ms": queue_wait_ms,
            "asr_inference_ms": inference_ms,
            "audio_bytes": len(pcm_data),
            "sample_rate": sample_rate,
        })
        result["metadata"] = metadata

        logger.info(
            "STT 推理完成: kind=%s provider=%s audio_bytes=%s sample_rate=%s "
            "queue_wait=%.1fms inference=%.1fms text_len=%s",
            request_kind,
            provider_name,
            len(pcm_data),
            sample_rate,
            queue_wait_ms,
            inference_ms,
            len(result.get("text") or ""),
        )
        return result

    def RecognizeSpeech(self, request, context):
        """
        单次语音识别（带并发控制）

        Args:
            request: AudioRequest
            context: gRPC 上下文

        Returns:
            TextResponse: 识别结果
        """
        try:
            audio_format = request.format or "wav"
            logger.info(f"收到语音识别请求: format={audio_format}, size={len(request.audio_data)} bytes")

            # 根据格式选择处理方式
            if audio_format.lower() == "pcm":
                # PCM 格式：直接使用内存处理（无磁盘 I/O）
                sample_rate = request.sample_rate or 16000
                logger.info(f"使用 PCM 直接识别（采样率: {sample_rate}Hz）")

                result = self._recognize_pcm_with_metrics(
                    pcm_data=request.audio_data,
                    sample_rate=sample_rate,
                    language=request.language or None,
                    request_kind="unary",
                    context=context,
                )
            else:
                # 其他格式：转换为 PCM 后识别
                logger.warning(f"不支持的格式 {audio_format}，仅支持 PCM 格式")
                if context is not None:
                    context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                    context.set_details(f"不支持的音频格式: {audio_format}，仅支持 PCM")
                return stt_service_pb2.TextResponse()

            # 返回结果
            return _to_text_response(result)

        except STTRequestCancelled as e:
            logger.info("语音识别请求已取消: %s", e)
            if context is not None:
                context.set_code(grpc.StatusCode.CANCELLED)
                context.set_details(str(e))
            return stt_service_pb2.TextResponse()
        except STTInferenceQueueTimeout as e:
            logger.warning("语音识别排队超时: %s", e)
            if context is not None:
                context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                context.set_details(str(e))
            return stt_service_pb2.TextResponse()
        except Exception as e:
            logger.error(f"语音识别错误: {e}")
            if context is not None:
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(str(e))
            return stt_service_pb2.TextResponse()

    def StreamRecognize(self, request_iterator, context):
        """
        流式语音识别（带并发控制）

        Args:
            request_iterator: AudioChunk 流
            context: gRPC 上下文

        Yields:
            TextResponse: 识别结果流
        """
        try:
            logger.info("收到流式语音识别请求")

            # 收集音频数据
            audio_chunks = []
            for chunk in request_iterator:
                if not _context_is_active(context):
                    raise STTRequestCancelled("STT stream request cancelled while receiving audio")
                audio_chunks.append(chunk.data)
                if chunk.is_final:
                    break

            # 合并音频数据
            audio_data = b"".join(audio_chunks)
            logger.info(f"收集到音频数据: {len(audio_data)} bytes")

            result = self._recognize_pcm_with_metrics(
                pcm_data=audio_data,
                sample_rate=16000,
                language=None,
                request_kind="stream_collect",
                context=context,
            )

            # 返回结果
            yield _to_text_response(result)

        except STTRequestCancelled as e:
            logger.info("流式识别请求已取消: %s", e)
            if context is not None:
                context.set_code(grpc.StatusCode.CANCELLED)
                context.set_details(str(e))
        except STTInferenceQueueTimeout as e:
            logger.warning("流式识别排队超时: %s", e)
            if context is not None:
                context.set_code(grpc.StatusCode.RESOURCE_EXHAUSTED)
                context.set_details(str(e))
        except Exception as e:
            logger.error(f"流式识别错误: {e}")
            if context is not None:
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(str(e))


def serve(port: int = 50054, host: str = '0.0.0.0'):
    """启动 STT gRPC 服务器"""
    server = grpc.server(ThreadPoolExecutor(max_workers=STT_GRPC_MAX_WORKERS))
    stt_service_pb2_grpc.add_STTServiceServicer_to_server(
        STTServiceServicer(), server
    )
    server.add_insecure_port(f'{host}:{port}')
    server.start()
    logger.info(f"STT gRPC 服务器已启动，监听地址: {host}:{port}")
    server.wait_for_termination()


if __name__ == '__main__':
    serve(port=STT_GRPC_SERVER_PORT, host=STT_GRPC_BIND_HOST)
