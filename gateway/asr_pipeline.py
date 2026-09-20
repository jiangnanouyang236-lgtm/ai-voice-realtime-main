from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from gateway.grpc_control import grpc_timeout_arg
from stt import stt_service_pb2


async def process_asr_audio(
    audio_data: bytes,
    session_id: str,
    *,
    get_stt_stub: Callable[[], Any],
    wav_to_pcm_fn: Callable[[bytes], tuple[bytes, int]],
    stt_metadata_fn: Callable[[Any], dict[str, Any]],
    stt_rpc_timeout_sec: float | int | None,
    audio_format: str,
    logger,
) -> tuple[str | None, float, dict[str, Any]]:
    """
    处理 ASR 识别（使用连接池，避免重复创建连接）。

    Returns:
        (识别的文本, ASR耗时ms, STT 元数据)，失败返回 (None, 0, {})
    """
    try:
        start_time = time.time()

        loop = asyncio.get_event_loop()

        t1 = time.time()
        stub = get_stt_stub()
        stub_time = (time.time() - t1) * 1000

        t_convert = time.time()
        pcm_data, sample_rate = await loop.run_in_executor(None, wav_to_pcm_fn, audio_data)
        convert_time = (time.time() - t_convert) * 1000
        logger.info(
            "会话 %s: WAV 转 PCM 完成 (%s -> %s bytes, %.1fms)",
            session_id,
            len(audio_data),
            len(pcm_data),
            convert_time,
        )

        t2 = time.time()
        request = stt_service_pb2.AudioRequest(
            audio_data=pcm_data,
            format=audio_format,
            sample_rate=sample_rate,
        )
        request_time = (time.time() - t2) * 1000

        t3 = time.time()
        stt_timeout = grpc_timeout_arg(stt_rpc_timeout_sec)
        response = await loop.run_in_executor(
            None,
            lambda: stub.RecognizeSpeech(request, timeout=stt_timeout),
        )
        grpc_time = (time.time() - t3) * 1000
        metadata = stt_metadata_fn(response)
        confidence_label = (
            f"{metadata['confidence']:.3f}"
            if metadata["confidence_source"] != "unavailable"
            else "N/A"
        )

        asr_time = time.time() - start_time
        asr_time_ms = asr_time * 1000
        logger.info(
            "会话 %s: ASR 识别完成 - text=%r, raw_text=%r, language=%s, emotion=%s, "
            "event_type=%s, confidence=%s, confidence_source=%s, tags=%s, 总耗时: %.2fs "
            "(获取stub: %.0fms, WAV转PCM: %.0fms, 构建请求: %.0fms, gRPC调用: %.0fms)",
            session_id,
            response.text,
            metadata["raw_text"],
            metadata["language"],
            metadata["emotion"],
            metadata["event_type"],
            confidence_label,
            metadata["confidence_source"],
            metadata["tags"],
            asr_time,
            stub_time,
            convert_time,
            request_time,
            grpc_time,
        )

        return response.text, asr_time_ms, metadata

    except Exception as exc:
        logger.error("会话 %s: ASR 识别失败 - %s", session_id, exc)
        return None, 0, {}
