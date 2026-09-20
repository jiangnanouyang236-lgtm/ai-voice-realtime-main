import io
import os
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from gateway.turn_gate_shadow_models import TurnGateShadowModels


ROOT = Path(__file__).resolve().parents[1]


def _wav_bytes(duration_ms: int = 100) -> bytes:
    frames = 16000 * duration_ms // 1000
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


class _SmartExtractor:
    def __call__(self, *_args, **_kwargs):
        return SimpleNamespace(input_features=np.zeros((1, 80, 800), dtype=np.float32))


class _Session:
    def __init__(self, output):
        self.output = output
        self.inputs = []

    def run(self, _outputs, inputs):
        self.inputs.append(inputs)
        return [self.output]


class _Tokenizer:
    def __init__(self):
        self.messages = None

    def apply_chat_template(self, messages, **_kwargs):
        self.messages = messages
        return "formatted<|im_end|>"

    def __call__(self, *_args, **_kwargs):
        return {"input_ids": np.asarray([[1, 2, 3]], dtype=np.int64)}


def test_smart_turn_uses_probability_without_second_sigmoid():
    models = TurnGateShadowModels()
    session = _Session(np.asarray([[0.75]], dtype=np.float32))
    models._smart_runtime = (np, session, _SmartExtractor())

    result = models.run_smart(_wav_bytes())

    assert result["status"] == "ok"
    assert result["probability"] == 0.75
    assert result["threshold"] == 0.5
    assert result["end"] is True
    assert session.inputs[0]["input_features"].shape == (1, 80, 800)


def test_livekit_eou_uses_last_token_probability_and_context():
    models = TurnGateShadowModels()
    tokenizer = _Tokenizer()
    session = _Session(np.asarray([[0.001, 0.02]], dtype=np.float32))
    models._eou_runtime = (np, session, tokenizer)

    result = models.run_eou(
        [{"role": "assistant", "content": "你想去哪里？"}],
        "我想去北京",
    )

    assert result["status"] == "ok"
    assert abs(result["probability"] - 0.02) < 1e-6
    assert result["threshold"] == 0.0066
    assert result["end"] is True
    assert tokenizer.messages == [
        {"role": "assistant", "content": "你想去哪里"},
        {"role": "user", "content": "我想去北京"},
    ]


def test_livekit_eou_skips_empty_asr_without_loading_model():
    models = TurnGateShadowModels()

    result = models.run_eou([], "")

    assert result["status"] == "skipped_empty_asr"


def test_missing_model_fails_open_as_unavailable(tmp_path):
    models = TurnGateShadowModels()
    models.smart_model_path = tmp_path / "missing-smart.onnx"
    models.eou_model_dir = tmp_path / "missing-eou"

    smart = models.run_smart(_wav_bytes())
    eou = models.run_eou([], "说完了")

    assert smart["status"] == "unavailable"
    assert eou["status"] == "unavailable"


def test_python_gateway_does_not_inherit_rust_turn_gate_flags():
    env = os.environ.copy()
    env["TURN_GATE_SHADOW_ENABLED"] = "true"
    env["TURN_GATE_ACTIVE_ENABLED"] = "true"
    env.pop("GATEWAY_TURN_GATE_SHADOW_ENABLED", None)
    env.pop("GATEWAY_TURN_GATE_ACTIVE_ENABLED", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gateway.config import TURN_GATE_SHADOW_ENABLED, TURN_GATE_ACTIVE_ENABLED; "
            "print(TURN_GATE_SHADOW_ENABLED, TURN_GATE_ACTIVE_ENABLED)",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False False"


def test_python_model_settings_use_gateway_prefix(monkeypatch, tmp_path):
    smart_path = tmp_path / "smart.onnx"
    eou_dir = tmp_path / "eou"
    monkeypatch.setenv("GATEWAY_TURN_GATE_SMART_MODEL_PATH", str(smart_path))
    monkeypatch.setenv("GATEWAY_TURN_GATE_EOU_MODEL_DIR", str(eou_dir))
    monkeypatch.setenv("GATEWAY_TURN_GATE_SMART_THRESHOLD", "0.7")
    monkeypatch.setenv("GATEWAY_TURN_GATE_EOU_THRESHOLD", "0.2")

    models = TurnGateShadowModels()

    assert models.smart_model_path == smart_path
    assert models.eou_model_dir == eou_dir
    assert models.smart_threshold == 0.7
    assert models.eou_threshold == 0.2
