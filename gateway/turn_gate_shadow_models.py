"""Optional Smart Turn and LiveKit EOU inference for Turn Gate shadow probes."""

from __future__ import annotations

import io
import os
import re
import threading
import time
import unicodedata
import wave
from pathlib import Path
from typing import Any


SAMPLE_RATE = 16000
MAX_SMART_SAMPLES = 8 * SAMPLE_RATE
MAX_HISTORY_TOKENS = 128
MAX_HISTORY_TURNS = 6
REPO_ROOT = Path(__file__).resolve().parents[1]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "").lower())
    text = "".join(
        char
        for char in text
        if not (unicodedata.category(char).startswith("P") and char not in ["'", "-"])
    )
    return re.sub(r"\s+", " ", text).strip()


def _format_chat(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    normalized: list[dict[str, str]] = []
    for original in messages[-MAX_HISTORY_TURNS:]:
        role = str(original.get("role") or "").strip()
        content = _normalize_text(original.get("content") or original.get("text") or "")
        if role not in {"user", "assistant"} or not content:
            continue
        if normalized and normalized[-1]["role"] == role:
            normalized[-1]["content"] += f" {content}"
        else:
            normalized.append({"role": role, "content": content})
    text = tokenizer.apply_chat_template(
        normalized,
        add_generation_prompt=False,
        add_special_tokens=False,
        tokenize=False,
    )
    marker = text.rfind("<|im_end|>")
    if marker < 0:
        raise ValueError("tokenizer chat template did not produce <|im_end|>")
    return text[:marker]


class TurnGateShadowModels:
    def __init__(self) -> None:
        self.smart_model_path = Path(
            os.getenv(
                "GATEWAY_TURN_GATE_SMART_MODEL_PATH",
                str(REPO_ROOT / "turn-gate/models/smart-turn-v3.2/smart-turn-v3.2-cpu.onnx"),
            )
        )
        self.eou_model_dir = Path(
            os.getenv(
                "GATEWAY_TURN_GATE_EOU_MODEL_DIR",
                str(REPO_ROOT / "turn-gate/models/livekit-eou-v0.4.1-intl"),
            )
        )
        self.smart_threshold = min(
            1.0, max(0.0, _env_float("GATEWAY_TURN_GATE_SMART_THRESHOLD", 0.5))
        )
        self.eou_threshold = min(
            1.0, max(0.0, _env_float("GATEWAY_TURN_GATE_EOU_THRESHOLD", 0.0066))
        )
        self._smart_load_lock = threading.Lock()
        self._eou_load_lock = threading.Lock()
        self._smart_run_lock = threading.Lock()
        self._eou_run_lock = threading.Lock()
        self._smart_runtime: tuple[Any, Any, Any] | None = None
        self._eou_runtime: tuple[Any, Any, Any] | None = None

    def _load_smart(self) -> tuple[Any, Any, Any]:
        with self._smart_load_lock:
            if self._smart_runtime is not None:
                return self._smart_runtime
            if not self.smart_model_path.is_file():
                raise FileNotFoundError(f"Smart Turn model not found: {self.smart_model_path}")
            import numpy as np
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor

            options = ort.SessionOptions()
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            session = ort.InferenceSession(
                str(self.smart_model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._smart_runtime = (np, session, WhisperFeatureExtractor(chunk_length=8))
            return self._smart_runtime

    def _load_eou(self) -> tuple[Any, Any, Any]:
        with self._eou_load_lock:
            if self._eou_runtime is not None:
                return self._eou_runtime
            model_path = self.eou_model_dir / "onnx/model_q8.onnx"
            if not model_path.is_file():
                raise FileNotFoundError(f"LiveKit EOU model not found: {model_path}")
            import numpy as np
            import onnxruntime as ort
            from transformers import AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(
                self.eou_model_dir,
                local_files_only=True,
                truncation_side="left",
            )
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            options.inter_op_num_threads = 1
            options.add_session_config_entry("session.dynamic_block_base", "4")
            session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._eou_runtime = (np, session, tokenizer)
            return self._eou_runtime

    def run_smart(self, wav_data: bytes) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            np, session, extractor = self._load_smart()
            with wave.open(io.BytesIO(wav_data), "rb") as handle:
                if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (16000, 1, 2):
                    raise ValueError("Smart Turn requires 16kHz mono PCM16 WAV")
                pcm = handle.readframes(handle.getnframes())
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            if len(audio) > MAX_SMART_SAMPLES:
                audio = audio[-MAX_SMART_SAMPLES:]
            preprocess_started = time.perf_counter()
            inputs = extractor(
                audio,
                sampling_rate=SAMPLE_RATE,
                return_tensors="np",
                padding="max_length",
                max_length=MAX_SMART_SAMPLES,
                truncation=True,
                do_normalize=True,
            )
            features = np.expand_dims(inputs.input_features.squeeze(0).astype(np.float32), axis=0)
            preprocess_ms = (time.perf_counter() - preprocess_started) * 1000
            inference_started = time.perf_counter()
            with self._smart_run_lock:
                probability = float(session.run(None, {"input_features": features})[0][0].item())
            inference_ms = (time.perf_counter() - inference_started) * 1000
            return {
                "status": "ok",
                "probability": probability,
                "end": probability > self.smart_threshold,
                "threshold": self.smart_threshold,
                "preprocess_ms": preprocess_ms,
                "inference_ms": inference_ms,
                "total_ms": (time.perf_counter() - started) * 1000,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "error": str(exc),
                "threshold": self.smart_threshold,
                "total_ms": (time.perf_counter() - started) * 1000,
            }

    def run_eou(self, history: list[dict[str, Any]], text: str) -> dict[str, Any]:
        started = time.perf_counter()
        if not str(text or "").strip():
            return {
                "status": "skipped_empty_asr",
                "threshold": self.eou_threshold,
                "total_ms": 0.0,
            }
        try:
            np, session, tokenizer = self._load_eou()
            messages = [
                {"role": item.get("role", ""), "content": item.get("content", "")}
                for item in history
            ]
            messages.append({"role": "user", "content": text})
            preprocess_started = time.perf_counter()
            formatted = _format_chat(tokenizer, messages)
            inputs = tokenizer(
                formatted,
                add_special_tokens=False,
                return_tensors="np",
                max_length=MAX_HISTORY_TOKENS,
                truncation=True,
            )
            preprocess_ms = (time.perf_counter() - preprocess_started) * 1000
            inference_started = time.perf_counter()
            with self._eou_run_lock:
                probability = float(
                    session.run(None, {"input_ids": inputs["input_ids"].astype(np.int64)})[0]
                    .flatten()[-1]
                )
            inference_ms = (time.perf_counter() - inference_started) * 1000
            return {
                "status": "ok",
                "probability": probability,
                "end": probability >= self.eou_threshold,
                "threshold": self.eou_threshold,
                "context_turns": min(len(history), MAX_HISTORY_TURNS),
                "preprocess_ms": preprocess_ms,
                "inference_ms": inference_ms,
                "total_ms": (time.perf_counter() - started) * 1000,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "error": str(exc),
                "threshold": self.eou_threshold,
                "total_ms": (time.perf_counter() - started) * 1000,
            }


_MODELS: TurnGateShadowModels | None = None
_MODELS_LOCK = threading.Lock()


def get_turn_gate_shadow_models() -> TurnGateShadowModels:
    global _MODELS
    with _MODELS_LOCK:
        if _MODELS is None:
            _MODELS = TurnGateShadowModels()
        return _MODELS


def run_smart_turn_shadow(wav_data: bytes) -> dict[str, Any]:
    return get_turn_gate_shadow_models().run_smart(wav_data)


def run_livekit_eou_shadow(history: list[dict[str, Any]], text: str) -> dict[str, Any]:
    return get_turn_gate_shadow_models().run_eou(history, text)


def warmup_turn_gate_shadow_models() -> dict[str, str]:
    models = get_turn_gate_shadow_models()
    status: dict[str, str] = {}
    for name, loader in (("smart_turn", models._load_smart), ("livekit_eou", models._load_eou)):
        try:
            loader()
            status[name] = "ready"
        except Exception as exc:
            status[name] = f"unavailable: {exc}"
    return status
