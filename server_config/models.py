"""
Shared runtime config models for Gateway / LLM / Admin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re
from typing import Any


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigValidationError(ValueError):
    """Raised when persisted config cannot be converted into a valid runtime snapshot."""


def normalize_grpc_service_url(value: Any, *, default: str) -> str:
    """Normalize Gateway upstream addresses to grpc:// or grpcs:// URLs."""
    raw = str(value or default).strip()
    if not raw:
        raise ConfigValidationError("gateway upstream 地址不能为空")

    if "://" in raw:
        scheme, target = raw.split("://", 1)
        scheme = scheme.lower().strip()
    else:
        scheme = "grpc"
        target = raw

    target = target.strip().rstrip("/")
    if scheme not in {"grpc", "grpcs"}:
        raise ConfigValidationError(f"gateway upstream 只支持 grpc:// 或 grpcs://: {raw}")
    if not target:
        raise ConfigValidationError(f"gateway upstream 缺少 host:port: {raw}")

    return f"{scheme}://{target}"


@dataclass(frozen=True)
class BotTemplateConfig:
    bot_id: str
    name: str
    system_prompt: str
    model: str
    temperature: float
    max_tokens: int
    tts_profile_id: str
    max_response_chars: int = 0
    mcp_servers: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    enabled: bool = True
    is_default: bool = False


@dataclass(frozen=True)
class TTSProfileConfig:
    tts_id: str
    tts_name: str
    provider_type: str
    speed: float
    provider_config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @property
    def voice(self) -> str:
        return str(self.provider_config.get("voice") or "").strip()

    @property
    def instruct(self) -> str:
        return str(self.provider_config.get("instruct") or "").strip()

    @property
    def path(self) -> str:
        return str(self.provider_config.get("path") or "").strip()

    @property
    def content(self) -> str:
        return str(self.provider_config.get("content") or "").strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "tts_id": self.tts_id,
            "tts_name": self.tts_name,
            "provider_type": self.provider_type,
            "speed": self.speed,
            "provider_config": dict(self.provider_config),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    name: str
    description: str = ""
    module: str | None = None
    class_name: str | None = None
    enabled: bool = True
    enabled_by_default: bool = True
    trigger_examples: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    tool_groups: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RobotConfig:
    robot_id: str
    name: str
    assigned_bot_id: str
    client_type: str | None = None
    enabled: bool = True
    is_new: bool = True
    last_connected_at: datetime | None = None
    notes: str | None = None


@dataclass(frozen=True)
class MCPServerConfig:
    server_key: str
    display_name: str
    type: str
    url: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_mcp_manager_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "type": self.type,
        }
        if self.url:
            config["url"] = self.url
        if self.command:
            config["command"] = self.command
        if self.args:
            config["args"] = list(self.args)
        if self.headers:
            config["headers"] = dict(self.headers)
        return config


@dataclass(frozen=True)
class GatewaySettingsConfig:
    max_connections: int
    max_history_length: int
    interrupt_enabled: bool
    stt_service_url: str
    llm_service_url: str
    tts_service_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_connections": self.max_connections,
            "max_history_length": self.max_history_length,
            "interrupt_enabled": self.interrupt_enabled,
            "stt_service_url": self.stt_service_url,
            "llm_service_url": self.llm_service_url,
            "tts_service_url": self.tts_service_url,
        }


@dataclass(frozen=True)
class TTSSettingsConfig:
    realtime_model: str
    default_voice: str
    default_speed: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "realtime_model": self.realtime_model,
            "default_voice": self.default_voice,
            "default_speed": self.default_speed,
        }


@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    config_version: int
    bots: dict[str, BotTemplateConfig]
    tts_profiles: dict[str, TTSProfileConfig]
    robots: dict[str, RobotConfig]
    mcp_servers: dict[str, MCPServerConfig]
    agents: dict[str, AgentConfig]
    gateway_settings: GatewaySettingsConfig
    tts_settings: TTSSettingsConfig
    default_bot_id: str
    loaded_at: datetime
    source: str = "db"

    @property
    def bot_count(self) -> int:
        return len(self.bots)

    @property
    def robot_count(self) -> int:
        return len(self.robots)

    @property
    def mcp_count(self) -> int:
        return len(self.mcp_servers)

    @property
    def agent_count(self) -> int:
        return len(self.agents)


@dataclass(frozen=True)
class LLMRuntimeSnapshot:
    config_version: int
    bots: dict[str, BotTemplateConfig]
    mcp_servers: dict[str, MCPServerConfig]
    agents: dict[str, AgentConfig]
    default_bot_id: str
    loaded_at: datetime
    source: str = "db"


@dataclass(frozen=True)
class GatewayRuntimeSnapshot:
    config_version: int
    bots: dict[str, BotTemplateConfig]
    robots: dict[str, RobotConfig]
    gateway_settings: GatewaySettingsConfig
    default_bot_id: str
    loaded_at: datetime
    source: str = "db"


@dataclass(frozen=True)
class TTSRuntimeSnapshot:
    config_version: int
    tts_settings: TTSSettingsConfig
    tts_profiles: dict[str, TTSProfileConfig]
    loaded_at: datetime
    source: str = "db"


def resolve_env_placeholders(value: str) -> str:
    """Replace ${ENV_NAME} placeholders using environment variables."""

    def replacer(match: re.Match[str]) -> str:
        env_name = match.group(1)
        env_value = os.getenv(env_name)
        if not env_value:
            raise ConfigValidationError(f"缺少配置所需的环境变量: {env_name}")
        return env_value

    return _ENV_PATTERN.sub(replacer, value)


def resolve_headers(raw_headers: Any) -> dict[str, str]:
    if raw_headers is None:
        return {}
    if not isinstance(raw_headers, dict):
        raise ConfigValidationError("headers_json 必须是 JSON 对象")
    resolved: dict[str, str] = {}
    for key, value in raw_headers.items():
        resolved[str(key)] = resolve_env_placeholders(str(value))
    return resolved


def resolve_text_list(raw_items: Any) -> list[str]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise ConfigValidationError("args_json 必须是 JSON 数组")
    return [resolve_env_placeholders(str(item)) for item in raw_items]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
