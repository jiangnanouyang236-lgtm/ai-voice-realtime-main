"""
主配置文件

包含所有服务的配置项
建议：敏感信息（如 API_KEY）应通过环境变量管理
"""

import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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


_load_dotenv(Path(__file__).resolve().parent / ".env")

# ============ API 配置 ============
_PLACEHOLDER_VALUES = {
    "",
    "your-api-key-here",
    "your-llm-api-key-here",
    "your-dashscope-api-key-here",
    "your-websearch-api-key-here",
    "your-tts-api-key-here",
    "replace-with-api-key",
    "replace-me",
}


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip() and value.strip() not in _PLACEHOLDER_VALUES:
            return value.strip()
    return default


def is_missing_secret(value: str | None) -> bool:
    return (value or "").strip() in _PLACEHOLDER_VALUES


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是有效数字，使用默认值 %s", name, value, default)
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是有效整数，使用默认值 %s", name, value, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    value = os.getenv(name)
    selected = value.strip().lower() if value is not None and value.strip() else default
    if selected not in choices:
        logger.warning(
            "环境变量 %s=%r 不在可选值 %s 中，使用默认值 %s",
            name,
            value,
            sorted(choices),
            default,
        )
        return default
    return selected


# LLM 使用 OpenAI-compatible 接口，当前基线为本地 vLLM Qwen3.5-9B。
LLM_API_KEY = _env_first("LLM_API_KEY")
LLM_MODEL_NAME = _env_first("LLM_MODEL_NAME", default="qwen3-5-9b")
LLM_BASE_URL = _env_first(
    "LLM_BASE_URL",
    default="http://10.10.6.121:15101/v1",
)
LLM_ROUTER_API_KEY = _env_first("LLM_ROUTER_API_KEY")
LLM_ROUTER_MODEL_NAME = _env_first("LLM_ROUTER_MODEL_NAME")
LLM_ROUTER_BASE_URL = _env_first("LLM_ROUTER_BASE_URL")

# DashScope/WebSearch/TTS 分离，避免本地 LLM Key 被误用于云端工具。
DASHSCOPE_API_KEY = _env_first("DASHSCOPE_API_KEY")
WEBSEARCH_API_KEY = _env_first("WEBSEARCH_API_KEY", "DASHSCOPE_API_KEY")
TTS_API_KEY = _env_first("TTS_API_KEY", "DASHSCOPE_API_KEY")
ROBOTS_TASK_SERVICE_URL = _env_first("ROBOTS_TASK_SERVICE_URL", default="https://tour.windaka.com/mcp")
ROBOTS_TASK_SERVICE_AUTHORIZATION = os.getenv("ROBOTS_TASK_SERVICE_AUTHORIZATION", "").strip()

LLM_MAX_TOOL_ROUNDS = max(1, int(os.getenv("LLM_MAX_TOOL_ROUNDS", "5")))

# ============ gRPC 服务端口配置 ============
# LLM 服务配置
LLM_GRPC_SERVER_PORT = int(os.getenv("LLM_GRPC_SERVER_PORT", "50053"))
LLM_GRPC_BIND_HOST = os.getenv("LLM_GRPC_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
LLM_GRPC_MAX_WORKERS = int(os.getenv("LLM_GRPC_MAX_WORKERS", "10"))
LLM_GRPC_SERVER = f"127.0.0.1:{LLM_GRPC_SERVER_PORT}"
LLM_ADMIN_PORT = int(os.getenv("LLM_ADMIN_PORT", "18053"))
LLM_ADMIN_BIND_HOST = os.getenv("LLM_ADMIN_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
LLM_SYNC_STREAM_EXECUTOR_MAX_WORKERS = max(1, _env_int("LLM_SYNC_STREAM_EXECUTOR_MAX_WORKERS", 4))
LLM_SYNC_STREAM_QUEUE_MAXSIZE = max(1, _env_int("LLM_SYNC_STREAM_QUEUE_MAXSIZE", 16))
LLM_HTTP_TIMEOUT_SEC = max(1.0, _env_float("LLM_HTTP_TIMEOUT_SEC", 60.0))
LLM_ROUTER_HTTP_TIMEOUT_SEC = max(1.0, _env_float("LLM_ROUTER_HTTP_TIMEOUT_SEC", 8.0))
LLM_VISION_ENABLED = _env_bool("LLM_VISION_ENABLED", True)
LLM_VISION_GATEWAY_BASE_URL = _env_first(
    "LLM_VISION_GATEWAY_BASE_URL",
    default="http://127.0.0.1:8282",
)
LLM_VISION_GATEWAY_TOKEN = os.getenv("LLM_VISION_GATEWAY_TOKEN", "").strip()
LLM_VISION_FETCH_TIMEOUT_SEC = max(
    0.1,
    _env_float("LLM_VISION_FETCH_TIMEOUT_SEC", 1.0),
)

# 配置数据库
CONFIG_DATABASE_URL = os.getenv("CONFIG_DATABASE_URL", "").strip()

# Gateway - M1 Go/Python internal voice protocol
GATEWAY_INTERNAL_VOICE_WS_ENABLED = _env_bool("GATEWAY_INTERNAL_VOICE_WS_ENABLED", True)
GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE = _env_choice(
    "GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE",
    "active",
    {"ack_only", "active"},
)

# STT 服务配置
STT_GRPC_SERVER_PORT = int(os.getenv("STT_GRPC_SERVER_PORT", "50054"))
STT_GRPC_BIND_HOST = os.getenv("STT_GRPC_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
STT_GRPC_MAX_WORKERS = int(os.getenv("STT_GRPC_MAX_WORKERS", "10"))
STT_PROVIDER = os.getenv("STT_PROVIDER", "qwen").strip().lower()
STT_MAX_CONCURRENT_INFERENCES = _env_int("STT_MAX_CONCURRENT_INFERENCES", 8)
QWEN_ASR_BASE_URL = _env_first("QWEN_ASR_BASE_URL", "ASR_BASE_URL")
QWEN_ASR_API_KEY = _env_first("QWEN_ASR_API_KEY", "ASR_API_KEY")
QWEN_ASR_MODEL = _env_first("QWEN_ASR_MODEL", "ASR_MODEL", default="qwen-asr")
QWEN_ASR_TIMEOUT = _env_float("QWEN_ASR_TIMEOUT", 30.0)

# TTS 服务配置
TTS_GRPC_SERVER_PORT = int(os.getenv("TTS_GRPC_SERVER_PORT", "50052"))
TTS_GRPC_BIND_HOST = os.getenv("TTS_GRPC_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
TTS_GRPC_MAX_WORKERS = int(os.getenv("TTS_GRPC_MAX_WORKERS", "5"))
TTS_ADMIN_PORT = int(os.getenv("TTS_ADMIN_PORT", "18052"))
TTS_ADMIN_BIND_HOST = os.getenv("TTS_ADMIN_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
LOCAL_QWEN3_TTS_WS_URL = _env_first(
    "LOCAL_QWEN3_TTS_WS_URL",
    default="ws://10.10.6.121:15120/v1/audio/speech/stream",
)
LOCAL_QWEN3_TTS_API_KEY = _env_first("LOCAL_QWEN3_TTS_API_KEY")
LOCAL_QWEN3_TTS_MODEL = _env_first("LOCAL_QWEN3_TTS_MODEL", default="qwen3-tts")
LOCAL_QWEN3_TTS_VOICE = _env_first("LOCAL_QWEN3_TTS_VOICE", default="serena")
LOCAL_QWEN3_TTS_SUPPORTED_VOICES = tuple(
    voice.strip()
    for voice in _env_first(
        "LOCAL_QWEN3_TTS_SUPPORTED_VOICES",
        default="serena,aiden,dylan,eric,ono_anna,ryan,sohee,uncle_fu,vivian",
    ).split(",")
    if voice.strip()
) or ("serena",)
LOCAL_QWEN3_TTS_LANGUAGE = _env_first("LOCAL_QWEN3_TTS_LANGUAGE", default="Chinese")
LOCAL_QWEN3_TTS_TASK_TYPE = _env_first("LOCAL_QWEN3_TTS_TASK_TYPE", default="CustomVoice")
LOCAL_QWEN3_TTS_INSTRUCTIONS = _env_first(
    "LOCAL_QWEN3_TTS_INSTRUCTIONS",
    default="自然、温柔、稳定、口语化，语速适中，情绪轻微，不夸张。",
)
LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE = max(1, _env_int("LOCAL_QWEN3_TTS_SOURCE_SAMPLE_RATE", 24000))
LOCAL_QWEN3_TTS_CONNECT_TIMEOUT_SEC = max(0.1, _env_float("LOCAL_QWEN3_TTS_CONNECT_TIMEOUT_SEC", 8.0))
LOCAL_QWEN3_TTS_RECV_TIMEOUT_SEC = max(1.0, _env_float("LOCAL_QWEN3_TTS_RECV_TIMEOUT_SEC", 60.0))
QWEN3_TTS_CUSTOM_VOICE_WS_URL = _env_first(
    "QWEN3_TTS_CUSTOM_VOICE_WS_URL",
    default="ws://10.10.6.121:15120/v1/audio/speech/stream",
)
QWEN3_TTS_CUSTOM_VOICE_API_KEY = _env_first("QWEN3_TTS_CUSTOM_VOICE_API_KEY")
QWEN3_TTS_CUSTOM_VOICE_MODEL = _env_first(
    "QWEN3_TTS_CUSTOM_VOICE_MODEL",
    default="qwen3-tts",
)
QWEN3_TTS_BASE_WS_URL = _env_first(
    "QWEN3_TTS_BASE_WS_URL",
    default="ws://10.10.6.121:15121/v1/audio/speech/stream",
)
QWEN3_TTS_BASE_API_KEY = _env_first("QWEN3_TTS_BASE_API_KEY", "LOCAL_QWEN3_TTS_BASE_API_KEY")
QWEN3_TTS_BASE_MODEL = _env_first("QWEN3_TTS_BASE_MODEL", default="qwen3-tts-base")
TTS_AUDIO_QUEUE_MAXSIZE = max(1, _env_int("TTS_AUDIO_QUEUE_MAXSIZE", 100))
TTS_AUDIO_QUEUE_PUT_TIMEOUT_SEC = max(0.0, _env_float("TTS_AUDIO_QUEUE_PUT_TIMEOUT_SEC", 2.0))

# ============ MCP 配置 ============
MCP_ENABLED = os.getenv("MCP_ENABLED", "true").lower() == "true"

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# MCP Server 配置
MCP_SERVERS = {
    # SSE 类型 - 远程工具集（需要先启动 utils_sse_server.py，端口 5004）
    "utils_remote": {
        "type": "sse",
        "url": "http://127.0.0.1:5004/mcp"
    },
    # SSE 类型 - 远程机器人控制（需要先启动 robot_sse_server.py，端口 5003）
    "robot_remote": {
        "type": "sse",
        "url": "http://127.0.0.1:5003/mcp"
    },
    # streamable_http 类型 - 阿里云百炼联网搜索
    "websearch": {
        "type": "streamable_http",
        "url": "https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp",
        "headers": {
            "Authorization": f"Bearer {WEBSEARCH_API_KEY}"
        }
    },
    # streamable_http 定时任务相关功能
    "robots_task_service":{
        "type": "streamable_http",
        "url": ROBOTS_TASK_SERVICE_URL,
        "headers": (
            {"Authorization": ROBOTS_TASK_SERVICE_AUTHORIZATION}
            if ROBOTS_TASK_SERVICE_AUTHORIZATION
            else {}
        )
    }
}
