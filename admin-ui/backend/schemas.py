"""
Admin backend request / response models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ApiMessage(BaseModel):
    success: bool
    message: str
    detail: Any | None = None


class AuthLoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class AuthStatusResponse(BaseModel):
    success: bool
    authenticated: bool
    username: str | None = None
    message: str | None = None


class RuntimeStatus(BaseModel):
    success: bool = True
    source: str | None = None
    config_version: int | None = None
    loaded_at: str | None = None
    bot_count: int | None = None
    robot_count: int | None = None
    mcp_count: int | None = None
    default_bot_id: str | None = None
    last_reload_error: str | None = None
    mcp_status: list[dict[str, Any]] = Field(default_factory=list)
    in_sync: bool | None = None
    llm: dict[str, Any] = Field(default_factory=dict)
    gateway: dict[str, Any] = Field(default_factory=dict)
    tts: dict[str, Any] = Field(default_factory=dict)


class OverviewDbStatus(BaseModel):
    latest_config_version: int
    bot_count: int
    robot_count: int
    mcp_count: int
    agent_count: int = 0
    tts_profile_count: int = 0
    default_bot_id: str | None = None


class ConfigSnapshotItem(BaseModel):
    config_version: int
    created_at: datetime
    created_by: str | None = None


class OverviewResponse(BaseModel):
    success: bool
    db: OverviewDbStatus
    runtime: RuntimeStatus | dict[str, Any]
    snapshots: list[ConfigSnapshotItem] = Field(default_factory=list)


class OptionItem(BaseModel):
    value: str
    label: str


class OptionsResponse(BaseModel):
    success: bool
    models: list[str]
    tts_custom_voices: list[str] = Field(default_factory=list)
    tts_profiles: list[OptionItem] = Field(default_factory=list)
    mcp_types: list[str]
    enabled_mcp_servers: list[OptionItem]
    enabled_agents: list[OptionItem] = Field(default_factory=list)
    bots: list[OptionItem]


class RuntimeGatewaySessionItem(BaseModel):
    session_id: str
    robot_id: str | None = None
    bot_id: str | None = None
    bot_name: str | None = None
    client_type: str | None = None
    is_new_robot: bool = False
    interrupted: bool = False
    history_count: int = 0
    last_active_at: str | None = None
    registered: bool = False


class RuntimeGatewayMessageItem(BaseModel):
    role: str
    content: str
    timestamp: str | None = None


class RuntimeGatewaySessionDetailItem(RuntimeGatewaySessionItem):
    history: list[RuntimeGatewayMessageItem] = Field(default_factory=list)


class RuntimeGatewaySessionDetailResponse(BaseModel):
    success: bool
    session: RuntimeGatewaySessionDetailItem | None = None
    message: str | None = None


class RuntimeGatewayRobotItem(BaseModel):
    robot_id: str
    name: str
    assigned_bot_id: str
    bot_name: str | None = None
    client_type: str | None = None
    enabled: bool = True
    is_new: bool = False
    last_connected_at: str | None = None
    notes: str | None = None


class RuntimeGatewayStats(BaseModel):
    active_connections: int = 0
    total_sessions: int = 0
    registered_sessions: int = 0


class RuntimeGatewayUpstreamItem(BaseModel):
    service: str
    target: str
    status: str
    message: str | None = None
    last_ready_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None


class RuntimeAgentItem(BaseModel):
    id: str
    name: str
    description: str | None = None
    module: str | None = None
    class_name: str | None = None
    trigger_examples: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    tool_groups: list[str] = Field(default_factory=list)
    active: bool = True


class RuntimeGatewayResponse(BaseModel):
    success: bool
    stats: RuntimeGatewayStats
    sessions: list[RuntimeGatewaySessionItem] = Field(default_factory=list)
    robots: list[RuntimeGatewayRobotItem] = Field(default_factory=list)
    settings: dict[str, Any] | None = None
    upstreams: list[RuntimeGatewayUpstreamItem] = Field(default_factory=list)
    agents: list[RuntimeAgentItem] = Field(default_factory=list)
    agents_error: str | None = None


class BotPayload(BaseModel):
    bot_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    system_prompt: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=100)
    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(gt=0)
    max_response_chars: int = Field(default=0, ge=0)
    tts_profile_id: str = Field(default="default_tts_profile", min_length=1, max_length=100)
    enabled: bool = True
    is_default: bool = False
    mcp_servers: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)

    @field_validator("mcp_servers")
    @classmethod
    def dedupe_mcp_servers(cls, value: list[str]) -> list[str]:
        deduped: list[str] = []
        for item in value:
            item = item.strip()
            if item and item not in deduped:
                deduped.append(item)
        return deduped

    @field_validator("agents")
    @classmethod
    def dedupe_agents(cls, value: list[str]) -> list[str]:
        deduped: list[str] = []
        for item in value:
            item = item.strip()
            if item and item not in deduped:
                deduped.append(item)
        return deduped


class BotItem(BotPayload):
    updated_at: datetime


class BotListResponse(BaseModel):
    success: bool
    items: list[BotItem]


class BotDetailResponse(BaseModel):
    success: bool
    item: BotItem


class TTSProfilePayload(BaseModel):
    tts_id: str = Field(min_length=1, max_length=100)
    tts_name: str = Field(min_length=1, max_length=100)
    provider_type: str = Field(pattern="^(qwen3_custom_voice|qwen3_base)$")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    enabled: bool = True
    voice: str | None = Field(default=None, max_length=100)
    instruct: str | None = None
    path: str | None = None
    content: str | None = None

    @field_validator("tts_id", "tts_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("voice", "instruct", "path", "content")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class TTSProfileItem(TTSProfilePayload):
    updated_at: datetime


class TTSProfileListResponse(BaseModel):
    success: bool
    items: list[TTSProfileItem]


class TTSProfileDetailResponse(BaseModel):
    success: bool
    item: TTSProfileItem


class AgentPayload(BaseModel):
    agent_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    module: str | None = Field(default=None, max_length=200)
    class_name: str | None = Field(default=None, max_length=100)
    enabled: bool = True
    enabled_by_default: bool = True
    trigger_examples_json: list[str] = Field(default_factory=list)
    allowed_tools_json: list[str] = Field(default_factory=list)
    tool_groups_json: list[str] = Field(default_factory=list)


class AgentItem(AgentPayload):
    updated_at: datetime


class AgentListResponse(BaseModel):
    success: bool
    items: list[AgentItem]


class AgentDetailResponse(BaseModel):
    success: bool
    item: AgentItem


class RobotPayload(BaseModel):
    robot_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    assigned_bot_id: str = Field(min_length=1, max_length=100)
    client_type: str | None = Field(default=None, max_length=50)
    enabled: bool = True
    is_new: bool = False
    notes: str | None = None


class RobotItem(RobotPayload):
    last_connected_at: datetime | None = None
    updated_at: datetime
    secret_configured: bool = False
    robot_secret_updated_at: datetime | None = None


class RobotListResponse(BaseModel):
    success: bool
    items: list[RobotItem]


class RobotDetailResponse(BaseModel):
    success: bool
    item: RobotItem


class RobotSecretResponse(BaseModel):
    success: bool
    robot_id: str
    robot_secret: str
    robot_secret_updated_at: datetime | None = None
    message: str


class MCPServerPayload(BaseModel):
    server_key: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)
    type: str
    url: str | None = None
    command: str | None = None
    args_json: list[str] = Field(default_factory=list)
    headers_json: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True

    @field_validator("type")
    @classmethod
    def validate_type(cls, value: str) -> str:
        allowed = {"sse", "streamable_http"}
        if value not in allowed:
            raise ValueError(f"type 必须是 {sorted(allowed)} 之一")
        return value


class MCPServerItem(MCPServerPayload):
    updated_at: datetime


class MCPServerListResponse(BaseModel):
    success: bool
    items: list[MCPServerItem]


class MCPServerDetailResponse(BaseModel):
    success: bool
    item: MCPServerItem
