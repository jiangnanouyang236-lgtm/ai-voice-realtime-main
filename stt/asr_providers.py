"""
ASR provider adapters.

The STT gRPC surface stays stable while Qwen3-ASR is configured through
environment variables.
"""

from __future__ import annotations

import io
import logging
import os
import time
import wave
from typing import Any

from openai import DefaultHttpxClient, OpenAI

import config  # noqa: F401 - load project .env before reading os.environ

logger = logging.getLogger(__name__)


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_float(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是有效数字，使用默认值 %s", name, value, default)
        return default


def _pcm16_to_wav_bytes(pcm_data: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate or 16000)
        wav_file.writeframes(pcm_data)
    return buffer.getvalue()


def _normalize_openai_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1/chat/completions"):
        return normalized[: -len("/chat/completions")]
    if normalized.endswith("/v1/audio/transcriptions"):
        return normalized[: -len("/audio/transcriptions")]
    if normalized and not normalized.endswith("/v1"):
        return f"{normalized}/v1"
    return normalized


def _extract_transcription_text(response: Any) -> str:
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        return str(response.get("text") or "").strip()
    return str(getattr(response, "text", "") or "").strip()


class QwenASRProvider:
    """OpenAI-compatible Qwen ASR adapter."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: Any | None = None,
        timeout: float | None = None,
    ):
        if not base_url:
            raise ValueError("QWEN_ASR_BASE_URL is required when STT_PROVIDER=qwen")
        if not api_key:
            raise ValueError("QWEN_ASR_API_KEY is required when STT_PROVIDER=qwen")
        if not model:
            raise ValueError("QWEN_ASR_MODEL is required when STT_PROVIDER=qwen")

        self.base_url = _normalize_openai_base_url(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.client = client or OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            http_client=DefaultHttpxClient(
                timeout=timeout or 30.0,
                trust_env=False,
            ),
        )

    def recognize_from_pcm(
        self,
        pcm_data: bytes,
        sample_rate: int = 16000,
        language: str | None = None,
    ) -> dict[str, Any]:
        wav_bytes = _pcm16_to_wav_bytes(pcm_data, sample_rate or 16000)
        return self._recognize_wav_bytes(
            wav_bytes=wav_bytes,
            audio_format="wav",
            sample_rate=sample_rate or 16000,
            language=language,
            audio_size=len(pcm_data),
        )

    def _recognize_wav_bytes(
        self,
        *,
        wav_bytes: bytes,
        audio_format: str,
        sample_rate: int,
        language: str | None,
        audio_size: int,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "file": ("audio.wav", wav_bytes, "audio/wav"),
            "response_format": "json",
            "temperature": 0,
        }
        if language:
            kwargs["language"] = language

        response = self.client.audio.transcriptions.create(**kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        text = _extract_transcription_text(response)

        logger.info(
            "Qwen ASR 完成: model=%s format=%s sample_rate=%s size=%s elapsed=%.1fms text_len=%s",
            self.model,
            audio_format,
            sample_rate,
            audio_size,
            elapsed_ms,
            len(text),
        )

        return {
            "text": text,
            "confidence": 0.0,
            "confidence_source": "unavailable",
            "language": language or "",
            "emotion": "",
            "event_type": "speech" if text else "",
            "raw_text": text,
            "tags": [],
            "metadata": {
                "provider": "qwen",
                "model": self.model,
                "audio_format": audio_format,
                "sample_rate": sample_rate,
                "audio_size": audio_size,
                "elapsed_ms": elapsed_ms,
            },
        }


def create_stt_provider() -> Any:
    provider = os.getenv("STT_PROVIDER", "qwen").strip().lower()
    if provider != "qwen":
        raise ValueError(f"unsupported STT_PROVIDER={provider!r}; only 'qwen' is supported")
    return QwenASRProvider(
        base_url=_env_first("QWEN_ASR_BASE_URL", "ASR_BASE_URL"),
        api_key=_env_first("QWEN_ASR_API_KEY", "ASR_API_KEY"),
        model=_env_first("QWEN_ASR_MODEL", "ASR_MODEL", default="qwen-asr"),
        timeout=_env_float("QWEN_ASR_TIMEOUT"),
    )
