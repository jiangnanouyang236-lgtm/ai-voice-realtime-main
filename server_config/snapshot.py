"""
Build and validate service-specific runtime snapshots from persisted rows.
"""

from __future__ import annotations

import os
from typing import Any

from server_config.models import (
    AgentConfig,
    BotTemplateConfig,
    ConfigValidationError,
    GatewayRuntimeSnapshot,
    GatewaySettingsConfig,
    LLMRuntimeSnapshot,
    MCPServerConfig,
    RobotConfig,
    RuntimeConfigSnapshot,
    TTSRuntimeSnapshot,
    TTSProfileConfig,
    TTSSettingsConfig,
    normalize_grpc_service_url,
    resolve_env_placeholders,
    resolve_headers,
    resolve_text_list,
    utcnow,
)


def build_runtime_snapshot(
    *,
    config_version: int,
    bot_rows: list[dict[str, Any]],
    robot_rows: list[dict[str, Any]],
    mcp_rows: list[dict[str, Any]],
    binding_rows: list[dict[str, Any]],
    agent_rows: list[dict[str, Any]] | None = None,
    agent_binding_rows: list[dict[str, Any]] | None = None,
    service_rows: list[dict[str, Any]] | None = None,
    tts_profile_rows: list[dict[str, Any]] | None = None,
    source: str = "db",
) -> RuntimeConfigSnapshot:
    llm_snapshot = build_llm_runtime_snapshot(
        config_version=config_version,
        bot_rows=bot_rows,
        mcp_rows=mcp_rows,
        binding_rows=binding_rows,
        agent_rows=agent_rows or [],
        agent_binding_rows=agent_binding_rows or [],
        source=source,
    )
    gateway_snapshot = build_gateway_runtime_snapshot(
        config_version=config_version,
        bot_rows=bot_rows,
        robot_rows=robot_rows,
        service_rows=service_rows or [],
        source=source,
    )
    tts_snapshot = build_tts_runtime_snapshot(
        config_version=config_version,
        service_rows=service_rows or [],
        tts_profile_rows=tts_profile_rows or [],
        source=source,
    )
    return RuntimeConfigSnapshot(
        config_version=config_version,
        bots=llm_snapshot.bots,
        tts_profiles=tts_snapshot.tts_profiles,
        robots=gateway_snapshot.robots,
        mcp_servers=llm_snapshot.mcp_servers,
        agents=llm_snapshot.agents,
        gateway_settings=gateway_snapshot.gateway_settings,
        tts_settings=tts_snapshot.tts_settings,
        default_bot_id=llm_snapshot.default_bot_id,
        loaded_at=utcnow(),
        source=source,
    )


def build_llm_runtime_snapshot(
    *,
    config_version: int,
    bot_rows: list[dict[str, Any]],
    mcp_rows: list[dict[str, Any]],
    binding_rows: list[dict[str, Any]],
    agent_rows: list[dict[str, Any]] | None = None,
    agent_binding_rows: list[dict[str, Any]] | None = None,
    source: str,
) -> LLMRuntimeSnapshot:
    enabled_mcp_servers_by_id, mcp_servers_by_key = _build_enabled_mcp_servers(mcp_rows)
    enabled_agents_by_id, agents_by_key = _build_enabled_agents(agent_rows or [])
    bots, default_bot_id = _build_enabled_bots(
        bot_rows=bot_rows,
        binding_rows=binding_rows,
        enabled_mcp_servers_by_id=enabled_mcp_servers_by_id,
        agent_binding_rows=agent_binding_rows or [],
        enabled_agents_by_id=enabled_agents_by_id,
    )
    return LLMRuntimeSnapshot(
        config_version=config_version,
        bots=bots,
        mcp_servers=mcp_servers_by_key,
        agents=agents_by_key,
        default_bot_id=default_bot_id,
        loaded_at=utcnow(),
        source=source,
    )


def build_gateway_runtime_snapshot(
    *,
    config_version: int,
    bot_rows: list[dict[str, Any]],
    robot_rows: list[dict[str, Any]],
    service_rows: list[dict[str, Any]],
    source: str,
) -> GatewayRuntimeSnapshot:
    bots, default_bot_id = _build_enabled_bots(bot_rows=bot_rows)
    robots = _build_enabled_robots(robot_rows=robot_rows, enabled_bots=bots)
    gateway_settings = build_gateway_settings(service_rows)
    return GatewayRuntimeSnapshot(
        config_version=config_version,
        bots=bots,
        robots=robots,
        gateway_settings=gateway_settings,
        default_bot_id=default_bot_id,
        loaded_at=utcnow(),
        source=source,
    )


def build_tts_runtime_snapshot(
    *,
    config_version: int,
    service_rows: list[dict[str, Any]],
    tts_profile_rows: list[dict[str, Any]],
    source: str,
) -> TTSRuntimeSnapshot:
    return TTSRuntimeSnapshot(
        config_version=config_version,
        tts_settings=build_tts_settings(service_rows),
        tts_profiles=build_tts_profiles(tts_profile_rows),
        loaded_at=utcnow(),
        source=source,
    )


def _build_enabled_mcp_servers(
    mcp_rows: list[dict[str, Any]],
) -> tuple[dict[int, MCPServerConfig], dict[str, MCPServerConfig]]:
    enabled_mcp_servers_by_id: dict[int, MCPServerConfig] = {}
    mcp_servers_by_key: dict[str, MCPServerConfig] = {}

    for row in mcp_rows:
        if not row["enabled"]:
            continue

        server_type = row["type"]
        if server_type not in {"sse", "streamable_http"}:
            raise ConfigValidationError(f"MCP Server {row['server_key']} 的 type 非法: {server_type}")

        raw_url = (row.get("url") or "").strip()
        raw_command = (row.get("command") or "").strip()
        args = resolve_text_list(row.get("args_json"))
        if server_type in {"sse", "streamable_http"} and not raw_url:
            raise ConfigValidationError(f"MCP Server {row['server_key']} 缺少 url")

        server = MCPServerConfig(
            server_key=row["server_key"],
            display_name=row["display_name"],
            type=server_type,
            url=resolve_env_placeholders(raw_url) if raw_url else None,
            command=resolve_env_placeholders(raw_command) if raw_command else None,
            args=args,
            headers=resolve_headers(row.get("headers_json")),
            enabled=True,
        )
        enabled_mcp_servers_by_id[int(row["id"])] = server
        mcp_servers_by_key[server.server_key] = server

    return enabled_mcp_servers_by_id, mcp_servers_by_key


def _build_enabled_agents(
    agent_rows: list[dict[str, Any]],
) -> tuple[dict[int, AgentConfig], dict[str, AgentConfig]]:
    enabled_agents_by_id: dict[int, AgentConfig] = {}
    agents_by_key: dict[str, AgentConfig] = {}

    for row in agent_rows:
        if not row["enabled"]:
            continue

        agent = AgentConfig(
            agent_id=row["agent_id"],
            name=row["name"],
            description=row.get("description") or "",
            module=row.get("module"),
            class_name=row.get("class_name"),
            enabled=True,
            enabled_by_default=bool(row.get("enabled_by_default", True)),
            trigger_examples=resolve_text_list(row.get("trigger_examples_json")),
            allowed_tools=resolve_text_list(row.get("allowed_tools_json")),
            tool_groups=resolve_text_list(row.get("tool_groups_json")),
        )
        enabled_agents_by_id[int(row["id"])] = agent
        agents_by_key[agent.agent_id] = agent

    return enabled_agents_by_id, agents_by_key


def _build_enabled_bots(
    *,
    bot_rows: list[dict[str, Any]],
    binding_rows: list[dict[str, Any]] | None = None,
    enabled_mcp_servers_by_id: dict[int, MCPServerConfig] | None = None,
    agent_binding_rows: list[dict[str, Any]] | None = None,
    enabled_agents_by_id: dict[int, AgentConfig] | None = None,
) -> tuple[dict[str, BotTemplateConfig], str]:
    bound_servers_by_bot_id: dict[int, list[tuple[int, str]]] = {}
    if binding_rows and enabled_mcp_servers_by_id is not None:
        for row in binding_rows:
            mcp_server = enabled_mcp_servers_by_id.get(int(row["mcp_server_id"]))
            if mcp_server is None:
                raise ConfigValidationError(
                    f"bot_mcp_bindings 引用了不存在或未启用的 MCP Server: {row['mcp_server_id']}"
                )
            bound_servers_by_bot_id.setdefault(int(row["bot_id"]), []).append(
                (int(row["sort_order"]), mcp_server.server_key),
            )

    bound_agents_by_bot_id: dict[int, list[tuple[int, str]]] = {}
    if agent_binding_rows and enabled_agents_by_id is not None:
        for row in agent_binding_rows:
            agent = enabled_agents_by_id.get(int(row["agent_id"]))
            if agent is None:
                raise ConfigValidationError(
                    f"bot_agent_bindings 引用了不存在或未启用的 Agent: {row['agent_id']}"
                )
            bound_agents_by_bot_id.setdefault(int(row["bot_id"]), []).append(
                (int(row["sort_order"]), agent.agent_id),
            )

    enabled_bots: dict[str, BotTemplateConfig] = {}
    default_bots: list[str] = []

    for row in bot_rows:
        if not row["enabled"]:
            continue

        bot_id = row["bot_id"]
        system_prompt = (row.get("system_prompt") or "").strip()
        if not system_prompt:
            raise ConfigValidationError(f"Bot {bot_id} 缺少 system_prompt")

        temperature = float(row["temperature"])
        max_tokens = int(row["max_tokens"])
        max_response_chars = int(row.get("max_response_chars") or 0)
        tts_profile_id = str(row.get("tts_profile_id") or "default_tts_profile").strip()
        if not 0 <= temperature <= 2:
            raise ConfigValidationError(f"Bot {bot_id} 的 temperature 超出范围")
        if max_tokens <= 0:
            raise ConfigValidationError(f"Bot {bot_id} 的 max_tokens 必须大于 0")
        if max_response_chars < 0:
            raise ConfigValidationError(f"Bot {bot_id} 的 max_response_chars 不能小于 0")
        if not tts_profile_id:
            raise ConfigValidationError(f"Bot {bot_id} 缺少 tts_profile_id")

        mcp_servers = [
            server_key
            for _, server_key in sorted(
                bound_servers_by_bot_id.get(int(row["id"]), []),
                key=lambda item: (item[0], item[1]),
            )
        ]
        agents = [
            agent_id
            for _, agent_id in sorted(
                bound_agents_by_bot_id.get(int(row["id"]), []),
                key=lambda item: (item[0], item[1]),
            )
        ]

        bot = BotTemplateConfig(
            bot_id=bot_id,
            name=row["name"],
            system_prompt=system_prompt,
            model=row["model"],
            temperature=temperature,
            max_tokens=max_tokens,
            tts_profile_id=tts_profile_id,
            max_response_chars=max_response_chars,
            mcp_servers=mcp_servers,
            agents=agents,
            enabled=True,
            is_default=bool(row["is_default"]),
        )
        enabled_bots[bot_id] = bot
        if bot.is_default:
            default_bots.append(bot_id)

    if not enabled_bots:
        raise ConfigValidationError("数据库中没有可用的 enabled Bot")
    if len(default_bots) != 1:
        raise ConfigValidationError("必须且只能有一个 enabled 且 is_default=true 的 Bot")

    return enabled_bots, default_bots[0]


def _build_enabled_robots(
    *,
    robot_rows: list[dict[str, Any]],
    enabled_bots: dict[str, BotTemplateConfig],
) -> dict[str, RobotConfig]:
    enabled_robots: dict[str, RobotConfig] = {}

    for row in robot_rows:
        if not row["enabled"]:
            continue
        assigned_bot_id = row["assigned_bot_id"]
        if assigned_bot_id not in enabled_bots:
            raise ConfigValidationError(
                f"Robot {row['robot_id']} 绑定了不存在或未启用的 Bot: {assigned_bot_id}"
            )

        enabled_robots[row["robot_id"]] = RobotConfig(
            robot_id=row["robot_id"],
            name=row["name"],
            assigned_bot_id=assigned_bot_id,
            client_type=row.get("client_type"),
            enabled=True,
            is_new=bool(row["is_new"]),
            last_connected_at=row.get("last_connected_at"),
            notes=row.get("notes"),
        )

    return enabled_robots


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ConfigValidationError(f"环境变量 {name} 不是有效整数: {value!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_text(name: str, default: str) -> str:
    value = os.getenv(name)
    return value.strip() if value is not None and value.strip() else default


def build_gateway_settings(_service_rows: list[dict[str, Any]]) -> GatewaySettingsConfig:
    max_connections = _env_int("GATEWAY_MAX_CONNECTIONS", 20)
    max_history_length = _env_int("GATEWAY_MAX_HISTORY_LENGTH", 10)
    interrupt_enabled = _env_bool("GATEWAY_INTERRUPT_ENABLED", True)
    stt_service_url = normalize_grpc_service_url(
        _env_text("STT_SERVICE_URL", "grpc://127.0.0.1:50054"),
        default="grpc://127.0.0.1:50054",
    )
    llm_service_url = normalize_grpc_service_url(
        _env_text("LLM_SERVICE_URL", "grpc://127.0.0.1:50053"),
        default="grpc://127.0.0.1:50053",
    )
    tts_service_url = normalize_grpc_service_url(
        _env_text("TTS_SERVICE_URL", "grpc://127.0.0.1:50052"),
        default="grpc://127.0.0.1:50052",
    )

    if max_connections <= 0:
        raise ConfigValidationError("gateway.max_connections 必须大于 0")
    if max_history_length <= 0:
        raise ConfigValidationError("gateway.max_history_length 必须大于 0")

    return GatewaySettingsConfig(
        max_connections=max_connections,
        max_history_length=max_history_length,
        interrupt_enabled=interrupt_enabled,
        stt_service_url=stt_service_url,
        llm_service_url=llm_service_url,
        tts_service_url=tts_service_url,
    )


def build_tts_profiles(profile_rows: list[dict[str, Any]]) -> dict[str, TTSProfileConfig]:
    profiles: dict[str, TTSProfileConfig] = {}
    for row in profile_rows:
        tts_id = str(row.get("tts_id") or "").strip()
        tts_name = str(row.get("tts_name") or "").strip()
        provider_type = str(row.get("provider_type") or "").strip()
        speed = float(row.get("speed") or 1.0)
        enabled = bool(row.get("enabled", True))

        if not tts_id:
            raise ConfigValidationError("TTS Profile 缺少 tts_id")
        if not tts_name:
            raise ConfigValidationError(f"TTS Profile {tts_id} 缺少 tts_name")
        if provider_type not in {"qwen3_custom_voice", "qwen3_base"}:
            raise ConfigValidationError(f"TTS Profile {tts_id} provider_type 非法: {provider_type}")
        if not 0.5 <= speed <= 2.0:
            raise ConfigValidationError(f"TTS Profile {tts_id} speed 超出范围")

        provider_config = dict(row.get("provider_config_json") or {})
        if provider_type == "qwen3_custom_voice":
            if not str(provider_config.get("voice") or "").strip():
                raise ConfigValidationError(f"TTS Profile {tts_id} 缺少 voice")
            if not str(provider_config.get("instruct") or "").strip():
                raise ConfigValidationError(f"TTS Profile {tts_id} 缺少 instruct")
        elif provider_type == "qwen3_base":
            if not str(provider_config.get("path") or "").strip():
                raise ConfigValidationError(f"TTS Profile {tts_id} 缺少 path")
            if not str(provider_config.get("content") or "").strip():
                raise ConfigValidationError(f"TTS Profile {tts_id} 缺少 content")

        profiles[tts_id] = TTSProfileConfig(
            tts_id=tts_id,
            tts_name=tts_name,
            provider_type=provider_type,
            speed=speed,
            provider_config=provider_config,
            enabled=enabled,
        )
    return profiles


def build_tts_settings(_service_rows: list[dict[str, Any]]) -> TTSSettingsConfig:
    # TTS defaults are intentionally code-owned; TTS Profile is the user-facing override.
    return TTSSettingsConfig(
        realtime_model="qwen3-tts",
        default_voice="serena",
        default_speed=1.0,
    )
