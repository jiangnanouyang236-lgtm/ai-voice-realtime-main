"""
Initialize and optionally seed the server-config Phase 1 database.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from server_config.mysql_db import connect_mysql, decode_rows, placeholders
from llm.agents.registry import discover_local_agents

SCHEMA_PATH = ROOT_DIR / "scripts" / "config_schema.sql"
BOOTSTRAP_DIR = ROOT_DIR / "server_config" / "bootstrap"
LEGACY_BOT_CONFIG_PATH = BOOTSTRAP_DIR / "legacy_bot_config.yaml"
LEGACY_MCP_CONFIG_PATH = BOOTSTRAP_DIR / "legacy_mcp_servers.yaml"

DEFAULT_BOT_ID = "default"
DEFAULT_BOT_NAME = "默认助手"
DEFAULT_SYSTEM_PROMPT = (
    "你叫默认助手，你是一个友好、专业的 AI 助手。"
    "回答应简洁、自然，并优先遵守当前 Bot 绑定的工具规则。"
)
DEFAULT_TTS_VOICE = "serena"
DEFAULT_TTS_PROFILE_ID = "default_tts_profile"
DEFAULT_TTS_PROFILE_NAME = "Default CustomVoice"
DEFAULT_TTS_SPEED = 1.0
DEFAULT_PYTHON_COMMAND = "python"
DEFAULT_GATEWAY_SETTINGS = {
    "max_connections": 20,
    "max_history_length": 10,
    "interrupt_enabled": True,
    "stt_service_url": "grpc://127.0.0.1:50054",
    "llm_service_url": "grpc://127.0.0.1:50053",
    "tts_service_url": "grpc://127.0.0.1:50052",
}
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize and seed config database")
    parser.add_argument(
        "--database-url",
        default=os.getenv("CONFIG_DATABASE_URL", "").strip(),
        help="MySQL/MariaDB connection string. Defaults to CONFIG_DATABASE_URL.",
    )
    parser.add_argument(
        "--created-by",
        default="init_config_db",
        help="Value stored in snapshot metadata.",
    )
    parser.add_argument(
        "--skip-legacy-sync",
        action="store_true",
        help="Only initialize schema/default bot, do not import bootstrap legacy files.",
    )
    parser.add_argument(
        "--legacy-bot-config",
        default=str(LEGACY_BOT_CONFIG_PATH),
        help="Legacy bot YAML path. Defaults to server_config/bootstrap/legacy_bot_config.yaml.",
    )
    parser.add_argument(
        "--legacy-mcp-config",
        default=str(LEGACY_MCP_CONFIG_PATH),
        help="Legacy MCP YAML path. Defaults to server_config/bootstrap/legacy_mcp_servers.yaml.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the legacy import plan without writing to the database.",
    )
    return parser.parse_args()


def load_legacy_bots(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"未找到 legacy bot_config.yaml: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    bots = raw.get("bots")
    if not isinstance(bots, dict):
        raise ValueError("legacy bot_config.yaml 缺少 bots 字段")

    normalized: list[dict[str, Any]] = []
    for bot_id, config in bots.items():
        if not isinstance(config, dict):
            raise ValueError(f"Bot {bot_id} 的配置格式非法")

        normalized.append(
            {
                "bot_id": str(bot_id).strip(),
                "name": str(config.get("name") or bot_id).strip(),
                "system_prompt": str(config.get("system_prompt") or "").strip(),
                "model": str(config.get("model") or "qwen3-5-9b").strip(),
                "temperature": float(config.get("temperature", 0.7)),
                "max_tokens": int(config.get("max_tokens", 2000)),
                "max_response_chars": int(config.get("max_response_chars", 0) or 0),
                "tts_profile_id": str(config.get("tts_profile_id") or DEFAULT_TTS_PROFILE_ID).strip(),
                "enabled": bool(config.get("enabled", True)),
                "is_default": bot_id == DEFAULT_BOT_ID,
                "mcp_servers": normalize_text_list(config.get("mcp_servers")),
                "agents": normalize_text_list(config.get("agents")),
            }
        )

    return normalized


def load_legacy_mcp_servers(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"未找到 legacy MCP 配置文件: {path}")

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    raw_servers = raw.get("mcp_servers")
    if not isinstance(raw_servers, dict):
        raise ValueError("legacy_mcp_servers.yaml 缺少 mcp_servers 字段")

    servers: dict[str, dict[str, Any]] = {}
    for server_key, config in raw_servers.items():
        if not isinstance(config, dict):
            raise ValueError(f"MCP Server {server_key} 的配置格式非法")
        servers[server_key] = normalize_mcp_server(server_key, config)

    return servers


def normalize_mcp_server(server_key: str, config: dict[str, Any]) -> dict[str, Any]:
    server_type = str(config.get("type") or "").strip()
    if not server_type:
        raise ValueError(f"MCP Server {server_key} 缺少 type")

    normalized_headers = dict(config.get("headers") or {})
    if server_key == "websearch":
        normalized_headers["Authorization"] = "Bearer ${WEBSEARCH_API_KEY}"

    return {
        "server_key": server_key,
        "display_name": str(config.get("display_name") or humanize_server_key(server_key)).strip(),
        "type": server_type,
        "url": config.get("url"),
        "command": config.get("command"),
        "args_json": normalize_text_list(config.get("args") or config.get("args_json")),
        "headers_json": normalized_headers,
        "enabled": bool(config.get("enabled", True)),
    }


def humanize_server_key(value: str) -> str:
    return value.replace("_", " ").strip().title()


def normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Iterable):
        items: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in items:
                items.append(text)
        return items
    raise ValueError(f"无法将 {value!r} 解析为字符串数组")


def print_legacy_summary(
    *,
    bots: list[dict[str, Any]],
    mcp_servers: dict[str, dict[str, Any]],
    agents: list[dict[str, Any]],
) -> None:
    print("Legacy import plan:")
    print(f"- bots: {len(bots)}")
    for bot in bots:
        print(
            f"  - {bot['bot_id']} ({bot['name']}), "
            f"default={bot['is_default']}, model={bot['model']}, mcp={bot['mcp_servers']}"
            f", agents={bot['agents']}"
        )

    print(f"- mcp_servers: {len(mcp_servers)}")
    for server in mcp_servers.values():
        location = server["url"]
        print(f"  - {server['server_key']} ({server['type']}): {location}")

    print(f"- agents: {len(agents)}")
    for agent in agents:
        print(f"  - {agent['agent_id']} ({agent['name']}), enabled={agent['enabled']}")

    missing_refs = sorted(
        {
            server_key
            for bot in bots
            for server_key in bot["mcp_servers"]
            if server_key not in mcp_servers
        }
    )
    if missing_refs:
        print(f"- missing bot MCP refs: {missing_refs}")


def execute_schema(cur: Any, schema_sql: str) -> None:
    for statement in schema_sql.split(";"):
        sql = statement.strip()
        if sql:
            cur.execute(sql)


def seed_legacy_config(
    conn: Any,
    *,
    bots: list[dict[str, Any]],
    mcp_servers: dict[str, dict[str, Any]],
    agents: list[dict[str, Any]],
    created_by: str,
) -> None:
    with conn.cursor() as cur:
        ensure_service_defaults(cur)
        for server in mcp_servers.values():
            upsert_mcp_server(cur, server)
        for agent in agents:
            upsert_agent(cur, agent)

        bot_id_to_db_id: dict[str, int] = {}
        for bot in bots:
            bot_id_to_db_id[bot["bot_id"]] = upsert_bot(cur, bot)

        for bot in bots:
            sync_bot_bindings(cur, bot_id_to_db_id[bot["bot_id"]], bot)
            sync_bot_agent_bindings(cur, bot_id_to_db_id[bot["bot_id"]], bot)

        assert_single_default_bot(cur)
        create_snapshot(cur, created_by=created_by)


def ensure_service_defaults(cur: Any) -> None:
    cur.execute(
        """
        insert into service_configs (service_name, config_json)
        values ('gateway', %s)
        on duplicate key update service_name = service_name
        """,
        (json.dumps(DEFAULT_GATEWAY_SETTINGS, ensure_ascii=False),),
    )
    ensure_tts_profile_defaults(cur)


def ensure_tts_profile_defaults(cur: Any) -> None:
    cur.execute(
        """
        insert into tts_profiles (
            tts_id, tts_name, provider_type, speed, provider_config_json, enabled
        )
        values (%s, %s, 'qwen3_custom_voice', %s, %s, true)
        on duplicate key update tts_id = tts_id
        """,
        (
            DEFAULT_TTS_PROFILE_ID,
            DEFAULT_TTS_PROFILE_NAME,
            DEFAULT_TTS_SPEED,
            json.dumps(
                {
                    "voice": DEFAULT_TTS_VOICE,
                    "instruct": "自然、温柔、稳定、口语化，语速适中，情绪轻微，不夸张。",
                },
                ensure_ascii=False,
            ),
        ),
    )


def upsert_bot(cur: Any, bot: dict[str, Any]) -> int:
    if bot["is_default"]:
        cur.execute(
            "update bots set is_default = false, updated_at = now() where bot_id <> %s",
            (bot["bot_id"],),
        )

    cur.execute(
        """
        insert into bots (
            bot_id, name, system_prompt, model, temperature, max_tokens,
            max_response_chars, tts_profile_id, enabled, is_default
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on duplicate key update
            id = last_insert_id(id),
            name = values(name),
            system_prompt = values(system_prompt),
            model = values(model),
            temperature = values(temperature),
            max_tokens = values(max_tokens),
            max_response_chars = values(max_response_chars),
            tts_profile_id = values(tts_profile_id),
            enabled = values(enabled),
            is_default = values(is_default),
            updated_at = now()
        """,
        (
            bot["bot_id"],
            bot["name"],
            bot["system_prompt"],
            bot["model"],
            bot["temperature"],
            bot["max_tokens"],
            bot["max_response_chars"],
            bot["tts_profile_id"],
            bot["enabled"],
            bot["is_default"],
        ),
    )
    return int(cur.lastrowid)


def upsert_mcp_server(cur: Any, server: dict[str, Any]) -> None:
    cur.execute(
        """
        insert into mcp_servers (
            server_key, display_name, type, url, command, args_json, headers_json, enabled
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on duplicate key update
            display_name = values(display_name),
            type = values(type),
            url = values(url),
            command = values(command),
            args_json = values(args_json),
            headers_json = values(headers_json),
            enabled = values(enabled),
            updated_at = now()
        """,
        (
            server["server_key"],
            server["display_name"],
            server["type"],
            server["url"],
            server["command"],
            json.dumps(server["args_json"], ensure_ascii=False),
            json.dumps(server["headers_json"], ensure_ascii=False),
            server["enabled"],
        ),
    )


def load_local_agents() -> list[dict[str, Any]]:
    agents = []
    for agent in discover_local_agents():
        agents.append(
            {
                "agent_id": agent.id,
                "name": agent.name,
                "description": getattr(agent, "description", "") or "",
                "module": agent.__class__.__module__,
                "class_name": agent.__class__.__name__,
                "enabled": bool(getattr(agent, "enabled_by_default", True)),
                "enabled_by_default": bool(getattr(agent, "enabled_by_default", True)),
                "trigger_examples_json": list(getattr(agent, "trigger_examples", ()) or ()),
                "allowed_tools_json": list(getattr(agent, "allowed_tools", ()) or ()),
                "tool_groups_json": list(getattr(agent, "tool_groups", ()) or ()),
            }
        )
    return agents


def upsert_agent(cur: Any, agent: dict[str, Any]) -> None:
    cur.execute(
        """
        insert into agents (
            agent_id, name, description, module, class_name, enabled,
            enabled_by_default, trigger_examples_json, allowed_tools_json, tool_groups_json
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on duplicate key update
            name = values(name),
            description = values(description),
            module = values(module),
            class_name = values(class_name),
            enabled_by_default = values(enabled_by_default),
            trigger_examples_json = values(trigger_examples_json),
            allowed_tools_json = values(allowed_tools_json),
            tool_groups_json = values(tool_groups_json),
            updated_at = now()
        """,
        (
            agent["agent_id"],
            agent["name"],
            agent.get("description") or "",
            agent.get("module"),
            agent.get("class_name"),
            agent.get("enabled", True),
            agent.get("enabled_by_default", True),
            json.dumps(agent.get("trigger_examples_json") or [], ensure_ascii=False),
            json.dumps(agent.get("allowed_tools_json") or [], ensure_ascii=False),
            json.dumps(agent.get("tool_groups_json") or [], ensure_ascii=False),
        ),
    )


def sync_bot_bindings(cur: Any, bot_db_id: int, bot: dict[str, Any]) -> None:
    cur.execute("delete from bot_mcp_bindings where bot_id = %s", (bot_db_id,))
    if not bot["mcp_servers"]:
        return

    cur.execute(
        f"""
        select id, server_key
        from mcp_servers
        where enabled = true and server_key in ({placeholders(bot["mcp_servers"])})
        """,
        tuple(bot["mcp_servers"]),
    )
    rows = list(cur.fetchall())
    server_key_to_id = {row["server_key"]: int(row["id"]) for row in rows}

    missing = [server_key for server_key in bot["mcp_servers"] if server_key not in server_key_to_id]
    if missing:
        print(f"Warning: Bot {bot['bot_id']} 引用了未启用或不存在的 MCP Server，已跳过: {missing}")

    for sort_order, server_key in enumerate(bot["mcp_servers"]):
        mcp_server_id = server_key_to_id.get(server_key)
        if not mcp_server_id:
            continue
        cur.execute(
            """
            insert into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
            values (%s, %s, %s)
            """,
            (bot_db_id, mcp_server_id, sort_order),
        )


def sync_bot_agent_bindings(cur: Any, bot_db_id: int, bot: dict[str, Any]) -> None:
    cur.execute("delete from bot_agent_bindings where bot_id = %s", (bot_db_id,))
    if not bot["agents"]:
        return

    cur.execute(
        f"""
        select id, agent_id
        from agents
        where enabled = true and agent_id in ({placeholders(bot["agents"])})
        """,
        tuple(bot["agents"]),
    )
    rows = list(cur.fetchall())
    agent_id_to_db_id = {row["agent_id"]: int(row["id"]) for row in rows}

    missing = [agent_id for agent_id in bot["agents"] if agent_id not in agent_id_to_db_id]
    if missing:
        print(f"Warning: Bot {bot['bot_id']} 引用了未启用或不存在的 Agent，已跳过: {missing}")

    for sort_order, agent_id in enumerate(bot["agents"]):
        agent_db_id = agent_id_to_db_id.get(agent_id)
        if not agent_db_id:
            continue
        cur.execute(
            """
            insert into bot_agent_bindings (bot_id, agent_id, sort_order)
            values (%s, %s, %s)
            """,
            (bot_db_id, agent_db_id, sort_order),
        )


def ensure_default_bot(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            select bot_id, is_default, enabled
            from bots
            where is_default = true
            order by id
            """
        )
        default_bots = list(cur.fetchall())

        if len(default_bots) > 1:
            raise ValueError("数据库中存在多个默认 Bot，请先清理后再初始化")

        if not default_bots:
            cur.execute(
                """
                insert into bots (
                    bot_id, name, system_prompt, model, temperature, max_tokens,
                    max_response_chars, tts_profile_id, enabled, is_default
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, true, true)
                on duplicate key update
                    name = values(name),
                    system_prompt = values(system_prompt),
                    model = values(model),
                    temperature = values(temperature),
                    max_tokens = values(max_tokens),
                    max_response_chars = values(max_response_chars),
                    tts_profile_id = values(tts_profile_id),
                    enabled = true,
                    is_default = true,
                    updated_at = now()
                """,
                (
                    DEFAULT_BOT_ID,
                    DEFAULT_BOT_NAME,
                    DEFAULT_SYSTEM_PROMPT,
                    "qwen3-5-9b",
                    0.7,
                    2000,
                    0,
                    DEFAULT_TTS_PROFILE_ID,
                ),
            )
            print("Seeded default bot")
        else:
            bot = default_bots[0]
            if not bot["enabled"]:
                cur.execute(
                    "update bots set enabled = true, updated_at = now() where bot_id = %s",
                    (bot["bot_id"],),
                )
                print(f"Enabled default bot: {bot['bot_id']}")


def ensure_initial_snapshot(conn: Any, created_by: str) -> None:
    with conn.cursor() as cur:
        cur.execute("select count(*) as count from config_snapshots")
        row = cur.fetchone()
        if int(row["count"]) > 0:
            return

        snapshot = build_snapshot_payload(conn, config_version=1)
        cur.execute(
            """
            insert into config_snapshots (config_version, snapshot_json, created_by)
            values (%s, %s, %s)
            on duplicate key update config_version = config_version
            """,
            (1, json.dumps(snapshot, ensure_ascii=False), created_by),
        )
        print("Created initial config snapshot v1")


def create_snapshot(cur: Any, *, created_by: str) -> int:
    conn = cursor_connection(cur)
    next_version = get_latest_config_version(conn) + 1
    snapshot = build_snapshot_payload(conn, config_version=next_version)
    cur.execute(
        """
        insert into config_snapshots (config_version, snapshot_json, created_by)
        values (%s, %s, %s)
        """,
        (next_version, json.dumps(snapshot, ensure_ascii=False), created_by),
    )
    cur.execute("select id from config_snapshots order by config_version desc limit 999999 offset 10")
    old_ids = [row["id"] for row in cur.fetchall()]
    if old_ids:
        cur.execute(
            f"delete from config_snapshots where id in ({placeholders(old_ids)})",
            tuple(old_ids),
        )
    print(f"Created config snapshot v{next_version}")
    return next_version


def cursor_connection(cur: Any) -> Any:
    return getattr(cur, "connection", None) or cur._get_db()


def get_latest_config_version(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute("select coalesce(max(config_version), 0) as version from config_snapshots")
        row = cur.fetchone()
    return int(row["version"])


def assert_single_default_bot(cur: Any) -> None:
    cur.execute("select count(*) as count from bots where enabled = true and is_default = true")
    row = cur.fetchone()
    if int(row["count"]) != 1:
        raise ValueError("必须且只能有一个启用中的默认 Bot")


def build_snapshot_payload(conn: Any, *, config_version: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("select * from bots order by id")
        bots = decode_rows(list(cur.fetchall()))
        cur.execute("select * from robots order by id")
        robots = decode_rows(list(cur.fetchall()))
        cur.execute("select * from mcp_servers order by id")
        mcp_servers = decode_rows(list(cur.fetchall()))
        cur.execute("select * from bot_mcp_bindings order by sort_order, id")
        bindings = decode_rows(list(cur.fetchall()))
        cur.execute("select * from agents order by id")
        agents = decode_rows(list(cur.fetchall()))
        cur.execute("select * from bot_agent_bindings order by sort_order, id")
        agent_bindings = decode_rows(list(cur.fetchall()))
        cur.execute("select * from service_configs order by service_name")
        service_configs = decode_rows(list(cur.fetchall()))
        cur.execute("select * from tts_profiles order by id")
        tts_profiles = decode_rows(list(cur.fetchall()))

    return {
        "config_version": config_version,
        "bots": to_jsonable(bots),
        "tts_profiles": to_jsonable(tts_profiles),
        "robots": to_jsonable(robots),
        "mcp_servers": to_jsonable(mcp_servers),
        "bot_mcp_bindings": to_jsonable(bindings),
        "agents": to_jsonable(agents),
        "bot_agent_bindings": to_jsonable(agent_bindings),
        "service_configs": to_jsonable(service_configs),
    }


def to_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def main() -> None:
    args = parse_args()

    legacy_bots: list[dict[str, Any]] = []
    legacy_mcp_servers: dict[str, dict[str, Any]] = {}
    local_agents: list[dict[str, Any]] = []

    if not args.skip_legacy_sync:
        legacy_bots = load_legacy_bots(Path(args.legacy_bot_config))
        legacy_mcp_servers = load_legacy_mcp_servers(Path(args.legacy_mcp_config))
        local_agents = load_local_agents()

    if args.dry_run:
        print_legacy_summary(bots=legacy_bots, mcp_servers=legacy_mcp_servers, agents=local_agents)
        return

    if not args.database_url:
        raise ValueError("缺少 --database-url 或环境变量 CONFIG_DATABASE_URL")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with connect_mysql(args.database_url) as conn:
        with conn.cursor() as cur:
            execute_schema(cur, schema_sql)
            ensure_service_defaults(cur)

        if args.skip_legacy_sync:
            ensure_default_bot(conn)
            ensure_initial_snapshot(conn, args.created_by)
        else:
            seed_legacy_config(
                conn,
                bots=legacy_bots,
                mcp_servers=legacy_mcp_servers,
                agents=local_agents,
                created_by=args.created_by,
            )
        conn.commit()

    print("Config DB initialized successfully")


if __name__ == "__main__":
    main()
