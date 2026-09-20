"""
Gateway 配置文件
"""

import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import (
    GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE,
    GATEWAY_INTERNAL_VOICE_WS_ENABLED,
)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


CONFIG_DATABASE_URL = os.getenv("CONFIG_DATABASE_URL", "").strip()

# 服务端口
GATEWAY_HOST = os.getenv("GATEWAY_BIND_HOST", os.getenv("GATEWAY_HOST", "0.0.0.0")).strip() or "0.0.0.0"
GATEWAY_PORT = int(os.getenv("GATEWAY_BIND_PORT", os.getenv("GATEWAY_PORT", "7860")))

# 后端服务地址
STT_SERVICE_URL = os.getenv("STT_SERVICE_URL", "grpc://127.0.0.1:50054")
LLM_SERVICE_URL = os.getenv("LLM_SERVICE_URL", "grpc://127.0.0.1:50053")
TTS_SERVICE_URL = os.getenv("TTS_SERVICE_URL", "grpc://127.0.0.1:50052")

# 会话配置（超时检测已移至客户端）
MAX_HISTORY_LENGTH = 10  # 最大历史记录条数
ROBOT_SECRET_REQUIRED = os.getenv("GATEWAY_REQUIRE_ROBOT_SECRET", "false").lower() == "true"
GATEWAY_REQUEST_QUEUE_MAXSIZE = max(1, _env_int("GATEWAY_REQUEST_QUEUE_MAXSIZE", 1))
TURN_GATE_SHADOW_ENABLED = os.getenv(
    "GATEWAY_TURN_GATE_SHADOW_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
TURN_GATE_ACTIVE_ENABLED = os.getenv(
    "GATEWAY_TURN_GATE_ACTIVE_ENABLED", "false"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TURN_GATE_MODELS_ENABLED = os.getenv(
    "GATEWAY_TURN_GATE_MODELS_ENABLED", "false"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
NATURAL_BARGE_IN_ENABLED = os.getenv(
    "GATEWAY_BARGE_IN_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}

# 流式响应保护：防止某次 LLM/TTS 卡住后堵塞后续整条会话
LLM_TTS_IDLE_TIMEOUT_SEC = float(os.getenv("LLM_TTS_IDLE_TIMEOUT_SEC", "20"))
LLM_TTS_THREAD_JOIN_TIMEOUT_SEC = float(os.getenv("LLM_TTS_THREAD_JOIN_TIMEOUT_SEC", "5"))
STT_AUDIO_CONTEXT_TO_LLM = os.getenv("STT_AUDIO_CONTEXT_TO_LLM", "true").lower() != "false"

# gRPC 调用 deadline。设置为 0 可关闭对应 RPC 的显式 deadline。
GATEWAY_STT_RPC_TIMEOUT_SEC = max(0.0, _env_float("GATEWAY_STT_RPC_TIMEOUT_SEC", 15.0))
GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC = max(0.0, _env_float("GATEWAY_LLM_STREAM_RPC_TIMEOUT_SEC", 120.0))
GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC = max(0.0, _env_float("GATEWAY_TTS_STREAM_RPC_TIMEOUT_SEC", 120.0))
GATEWAY_LLM_SESSION_CLEANUP_TIMEOUT_SEC = max(0.0, _env_float("GATEWAY_LLM_SESSION_CLEANUP_TIMEOUT_SEC", 3.0))
GATEWAY_COMPLEX_WORKFLOW_ENABLED = os.getenv(
    "GATEWAY_COMPLEX_WORKFLOW_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
GATEWAY_WORKFLOW_RPC_TIMEOUT_SEC = max(
    1.0, _env_float("GATEWAY_WORKFLOW_RPC_TIMEOUT_SEC", 30.0)
)
GATEWAY_WORKFLOW_STREAM_TIMEOUT_SEC = max(
    1.0, _env_float("GATEWAY_WORKFLOW_STREAM_TIMEOUT_SEC", 120.0)
)
GATEWAY_WORKFLOW_PLAYBACK_TIMEOUT_SEC = max(
    1.0, _env_float("GATEWAY_WORKFLOW_PLAYBACK_TIMEOUT_SEC", 30.0)
)
GATEWAY_WS_SEND_TIMEOUT_SEC = max(0.0, _env_float("GATEWAY_WS_SEND_TIMEOUT_SEC", 5.0))
GATEWAY_WS_AUDIO_SLOW_SEND_MS = max(0.0, _env_float("GATEWAY_WS_AUDIO_SLOW_SEND_MS", 1500.0))
GATEWAY_WS_SLOW_SEND_MAX_STRIKES = max(1, _env_int("GATEWAY_WS_SLOW_SEND_MAX_STRIKES", 3))

# 内存链路观测：只记录轻量摘要，不落库、不写文件
TRACE_ENABLED = os.getenv("TRACE_ENABLED", "true").lower() != "false"
TRACE_MAX_EVENTS = int(os.getenv("TRACE_MAX_EVENTS", "10000"))
TRACE_MAX_ROUNDS = int(os.getenv("TRACE_MAX_ROUNDS", "1000"))
TRACE_TEXT_MAX_CHARS = int(os.getenv("TRACE_TEXT_MAX_CHARS", "300"))
TRACE_ERROR_MAX_CHARS = int(os.getenv("TRACE_ERROR_MAX_CHARS", "1000"))

# 打断配置
INTERRUPT_ENABLED = True

# 并发配置
MAX_CONNECTIONS = 50  # 最大并发连接数

# 音频配置
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1
AUDIO_FORMAT = "pcm"  # 使用 PCM 格式，避免磁盘 I/O

# 音频输入保护：当前客户端上行使用 protocol-v2 binary Opus。
GATEWAY_MAX_AUDIO_RAW_BYTES = max(1, _env_int("GATEWAY_MAX_AUDIO_RAW_BYTES", 400000))
GATEWAY_MAX_AUDIO_DURATION_MS = max(1, _env_int("GATEWAY_MAX_AUDIO_DURATION_MS", 10000))
GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE = max(1, _env_int("GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE", AUDIO_SAMPLE_RATE))
GATEWAY_ALLOWED_AUDIO_CHANNELS = max(1, _env_int("GATEWAY_ALLOWED_AUDIO_CHANNELS", AUDIO_CHANNELS))
GATEWAY_ALLOWED_SAMPLE_WIDTH_BYTES = max(1, _env_int("GATEWAY_ALLOWED_SAMPLE_WIDTH_BYTES", 2))
OPUS_BITRATE_BPS = min(64000, max(6000, _env_int("OPUS_BITRATE_BPS", 16000)))
