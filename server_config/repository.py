"""
Database repository for server-config Phase 1.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Any, Iterator

from server_config.snapshot import (
    build_gateway_runtime_snapshot,
    build_llm_runtime_snapshot,
    build_runtime_snapshot,
    build_tts_runtime_snapshot,
)
from server_config.models import normalize_grpc_service_url
from server_config.mysql_db import connect_mysql, decode_row, decode_rows, placeholders
from server_config.secrets import generate_robot_secret, hash_robot_secret, verify_robot_secret


QWEN3_CUSTOM_VOICE_OPTIONS = ["serena", "aiden", "dylan", "eric", "ono_anna", "ryan", "sohee", "uncle_fu", "vivian"]
MCP_TYPE_OPTIONS = ["sse", "streamable_http"]
MODEL_OPTIONS = ["qwen3-5-9b"]
DEFAULT_TTS_PROFILE_ID = "default_tts_profile"
TTS_PROVIDER_TYPES = {"qwen3_custom_voice", "qwen3_base"}
DEFAULT_GATEWAY_SETTINGS = {
    "max_connections": 20,
    "max_history_length": 10,
    "interrupt_enabled": True,
    "stt_service_url": "grpc://127.0.0.1:50054",
    "llm_service_url": "grpc://127.0.0.1:50053",
    "tts_service_url": "grpc://127.0.0.1:50052",
}


class ConfigRepository:
    def __init__(self, database_url: str):
        self.database_url = database_url

    @contextmanager
    def connection(self) -> Iterator[Any]:
        conn = connect_mysql(self.database_url)
        try:
            yield conn
        finally:
            conn.close()

    def load_runtime_snapshot(self):
        config_version, rows, source = self._resolve_runtime_rows()
        return build_runtime_snapshot(
            config_version=config_version,
            bot_rows=rows["bot_rows"],
            robot_rows=rows["robot_rows"],
            mcp_rows=rows["mcp_rows"],
            binding_rows=rows["binding_rows"],
            agent_rows=rows["agent_rows"],
            agent_binding_rows=rows["agent_binding_rows"],
            service_rows=rows["service_rows"],
            tts_profile_rows=rows["tts_profile_rows"],
            source=source,
        )

    def load_llm_runtime_snapshot(self, *, version: int | None = None):
        config_version, rows, source = self._resolve_runtime_rows(version=version)
        return build_llm_runtime_snapshot(
            config_version=config_version,
            bot_rows=rows["bot_rows"],
            mcp_rows=rows["mcp_rows"],
            binding_rows=rows["binding_rows"],
            agent_rows=rows["agent_rows"],
            agent_binding_rows=rows["agent_binding_rows"],
            source=source,
        )

    def load_gateway_runtime_snapshot(self, *, version: int | None = None):
        config_version, rows, source = self._resolve_runtime_rows(version=version)
        return build_gateway_runtime_snapshot(
            config_version=config_version,
            bot_rows=rows["bot_rows"],
            robot_rows=rows["robot_rows"],
            service_rows=rows["service_rows"],
            source=source,
        )

    def load_tts_runtime_snapshot(self, *, version: int | None = None):
        config_version, rows, source = self._resolve_runtime_rows(version=version)
        return build_tts_runtime_snapshot(
            config_version=config_version,
            service_rows=rows["service_rows"],
            tts_profile_rows=rows["tts_profile_rows"],
            source=source,
        )

    def register_robot(
        self,
        robot_id: str,
        client_type: str | None = None,
        *,
        default_bot_id: str | None = None,
        allow_create: bool = True,
    ) -> dict[str, Any]:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select robot_id, name, assigned_bot_id, client_type, enabled, is_new,
                           robot_secret_hash, robot_secret_updated_at, last_connected_at
                    from robots
                    where robot_id = %s
                    """,
                    (robot_id,),
                )
                existing = cur.fetchone()

                if existing:
                    cur.execute(
                        """
                        update robots
                        set client_type = coalesce(%s, client_type),
                            last_connected_at = now(),
                            updated_at = now()
                        where robot_id = %s
                        """,
                        (client_type, robot_id),
                    )
                    cur.execute(
                        """
                        select robot_id, name, assigned_bot_id, client_type, enabled, is_new,
                               robot_secret_hash, robot_secret_updated_at, last_connected_at
                        from robots
                        where robot_id = %s
                        """,
                        (robot_id,),
                    )
                    row = decode_row(cur.fetchone())
                    conn.commit()
                    row["created"] = False
                    return row

                if not allow_create:
                    raise ValueError(f"Robot 不存在: {robot_id}")

                if default_bot_id:
                    cur.execute(
                        """
                        select bot_id
                        from bots
                        where enabled = true and bot_id = %s
                        limit 1
                        """,
                        (default_bot_id,),
                    )
                    default_bot = cur.fetchone()
                    if not default_bot:
                        raise ValueError(f"指定的默认 Bot 不存在或未启用: {default_bot_id}")
                else:
                    cur.execute(
                        """
                        select bot_id
                        from bots
                        where enabled = true and is_default = true
                        order by id
                        limit 1
                        """
                    )
                    default_bot = cur.fetchone()
                if not default_bot:
                    raise ValueError("缺少 enabled 的默认 Bot，无法自动注册 Robot")

                cur.execute(
                    """
                    insert into robots (
                      robot_id, name, assigned_bot_id, client_type, enabled, is_new, last_connected_at
                    )
                    values (%s, %s, %s, %s, true, true, now())
                    """,
                    (robot_id, robot_id, default_bot["bot_id"], client_type),
                )
                cur.execute(
                    """
                    select robot_id, name, assigned_bot_id, client_type, enabled, is_new,
                           robot_secret_hash, robot_secret_updated_at, last_connected_at
                    from robots
                    where robot_id = %s
                    """,
                    (robot_id,),
                )
                row = decode_row(cur.fetchone())
                self._create_snapshot(cur, created_by="gateway-auto-register")
                conn.commit()
                row["created"] = True
                return row

    def verify_robot_secret(self, robot_id: str, secret: str) -> bool:
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                "select robot_secret_hash from robots where robot_id = %s",
                (robot_id,),
            )
        return verify_robot_secret(secret, row.get("robot_secret_hash"))

    def reset_robot_secret(self, robot_id: str) -> dict[str, Any]:
        secret = generate_robot_secret()
        secret_hash = hash_robot_secret(secret)
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update robots
                    set robot_secret_hash = %s,
                        robot_secret_updated_at = now(),
                        updated_at = now()
                    where robot_id = %s
                    """,
                    (secret_hash, robot_id),
                )
                cur.execute(
                    """
                    select robot_id, robot_secret_updated_at
                    from robots
                    where robot_id = %s
                    """,
                    (robot_id,),
                )
                row = decode_row(cur.fetchone())
                if not row:
                    raise ValueError(f"Robot 不存在: {robot_id}")
                conn.commit()
        return {
            "robot_id": row["robot_id"],
            "robot_secret": secret,
            "robot_secret_updated_at": row["robot_secret_updated_at"],
        }

    def get_bot_name(self, bot_id: str) -> str | None:
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                "select name from bots where bot_id = %s",
                (bot_id,),
            )
        return row.get("name")

    def resolve_robot_bot_binding(self, robot_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                """
                select r.robot_id, r.assigned_bot_id, r.enabled as robot_enabled, b.name as bot_name
                from robots r
                left join bots b on b.bot_id = r.assigned_bot_id
                where r.robot_id = %s
                """,
                (robot_id,),
            )
        return row if row.get("robot_id") else None

    def get_latest_config_version(self, conn: Any | None = None) -> int:
        if conn is not None:
            row = self._fetch_one(conn, "select max(config_version) as version from config_snapshots")
            return int(row["version"] or 1)

        with self.connection() as owned_conn:
            row = self._fetch_one(owned_conn, "select max(config_version) as version from config_snapshots")
            return int(row["version"] or 1)

    def get_overview_db_status(self) -> dict[str, Any]:
        with self.connection() as conn:
            bot_count = self._fetch_one(conn, "select count(*) as count from bots")
            robot_count = self._fetch_one(conn, "select count(*) as count from robots")
            mcp_count = self._fetch_one(conn, "select count(*) as count from mcp_servers")
            agent_count = self._fetch_one(conn, "select count(*) as count from agents")
            tts_profile_count = self._fetch_one(conn, "select count(*) as count from tts_profiles")
            default_bot = self._fetch_one(
                conn,
                "select bot_id from bots where enabled = true and is_default = true order by id limit 1",
            )
            latest_version = self.get_latest_config_version(conn)

        return {
            "latest_config_version": latest_version,
            "bot_count": int(bot_count["count"]),
            "robot_count": int(robot_count["count"]),
            "mcp_count": int(mcp_count["count"]),
            "agent_count": int(agent_count["count"]),
            "tts_profile_count": int(tts_profile_count["count"]),
            "default_bot_id": default_bot.get("bot_id"),
        }

    def list_config_snapshots(self, limit: int = 10) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        with self.connection() as conn:
            return self._fetch_all(
                conn,
                """
                select config_version, created_at, created_by
                from config_snapshots
                order by config_version desc
                limit %s
                """,
                (safe_limit,),
            )

    def get_gateway_settings(self) -> dict[str, Any]:
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                """
                select service_name, config_json, updated_at
                from service_configs
                where service_name = 'gateway'
                """
            )
        config_json = row.get("config_json") if row else None
        merged = {**DEFAULT_GATEWAY_SETTINGS, **(config_json or {})}
        merged["updated_at"] = row.get("updated_at") if row else None
        return merged

    def upsert_gateway_settings(self, payload: dict[str, Any], *, created_by: str = "admin-api") -> dict[str, Any]:
        max_connections = int(payload["max_connections"])
        max_history_length = int(payload["max_history_length"])
        interrupt_enabled = bool(payload["interrupt_enabled"])
        stt_service_url = normalize_grpc_service_url(
            payload.get("stt_service_url"),
            default=DEFAULT_GATEWAY_SETTINGS["stt_service_url"],
        )
        llm_service_url = normalize_grpc_service_url(
            payload.get("llm_service_url"),
            default=DEFAULT_GATEWAY_SETTINGS["llm_service_url"],
        )
        tts_service_url = normalize_grpc_service_url(
            payload.get("tts_service_url"),
            default=DEFAULT_GATEWAY_SETTINGS["tts_service_url"],
        )

        if max_connections <= 0:
            raise ValueError("max_connections 必须大于 0")
        if max_history_length <= 0:
            raise ValueError("max_history_length 必须大于 0")

        normalized = {
            "max_connections": max_connections,
            "max_history_length": max_history_length,
            "interrupt_enabled": interrupt_enabled,
            "stt_service_url": stt_service_url,
            "llm_service_url": llm_service_url,
            "tts_service_url": tts_service_url,
        }

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into service_configs (service_name, config_json, updated_at)
                    values ('gateway', %s, now())
                    on duplicate key update
                        config_json = values(config_json),
                        updated_at = now()
                    """,
                    (json.dumps(normalized, ensure_ascii=False),),
                )
                self._create_snapshot(cur, created_by=created_by)
                cur.execute(
                    """
                    select config_json, updated_at
                    from service_configs
                    where service_name = 'gateway'
                    """
                )
                row = decode_row(cur.fetchone()) or {}
            conn.commit()

        config_json = row.get("config_json") or {}
        result = {**DEFAULT_GATEWAY_SETTINGS, **config_json}
        result["updated_at"] = row.get("updated_at")
        return result

    def list_bots(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            bots = self._fetch_all(
                conn,
                """
                select bot_id, name, system_prompt, model, temperature, max_tokens,
                       max_response_chars, tts_profile_id, enabled, is_default, updated_at
                from bots
                order by bot_id
                """,
            )
            bindings = self._fetch_all(
                conn,
                """
                select b.bot_id, ms.server_key, bm.sort_order
                from bot_mcp_bindings bm
                join bots b on b.id = bm.bot_id
                join mcp_servers ms on ms.id = bm.mcp_server_id
                order by b.bot_id, bm.sort_order, ms.server_key
                """,
            )
            agent_bindings = self._fetch_all(
                conn,
                """
                select b.bot_id, a.agent_id, ba.sort_order
                from bot_agent_bindings ba
                join bots b on b.id = ba.bot_id
                join agents a on a.id = ba.agent_id
                order by b.bot_id, ba.sort_order, a.agent_id
                """,
            )
        return self._attach_bot_bindings(bots, bindings, agent_bindings)

    def get_bot(self, bot_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select bot_id, name, system_prompt, model, temperature, max_tokens,
                           max_response_chars, tts_profile_id, enabled, is_default, updated_at
                    from bots
                    where bot_id = %s
                    """,
                    (bot_id,),
                )
                bot = decode_row(cur.fetchone())
                if not bot:
                    return None

                cur.execute(
                    """
                    select b.bot_id, ms.server_key, bm.sort_order
                    from bot_mcp_bindings bm
                    join bots b on b.id = bm.bot_id
                    join mcp_servers ms on ms.id = bm.mcp_server_id
                    where b.bot_id = %s
                    order by bm.sort_order, ms.server_key
                    """,
                    (bot_id,),
                )
                bindings = decode_rows(list(cur.fetchall()))
                cur.execute(
                    """
                    select b.bot_id, a.agent_id, ba.sort_order
                    from bot_agent_bindings ba
                    join bots b on b.id = ba.bot_id
                    join agents a on a.id = ba.agent_id
                    where b.bot_id = %s
                    order by ba.sort_order, a.agent_id
                    """,
                    (bot_id,),
                )
                agent_bindings = decode_rows(list(cur.fetchall()))
        return self._attach_bot_bindings([bot], bindings, agent_bindings)[0]

    def upsert_bot(self, payload: dict[str, Any], *, created_by: str = "admin-api") -> dict[str, Any]:
        payload_bot_id = payload["bot_id"].strip()
        if not payload_bot_id:
            raise ValueError("bot_id 不能为空")
        if payload.get("is_default") and not payload.get("enabled", True):
            raise ValueError("默认 Bot 必须启用")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id, is_default from bots where bot_id = %s", (payload_bot_id,))
                existing = cur.fetchone()

                mcp_keys = payload.get("mcp_servers", [])
                if mcp_keys:
                    cur.execute(
                        f"""
                        select id, server_key
                        from mcp_servers
                        where enabled = true and server_key in ({placeholders(mcp_keys)})
                        """,
                        tuple(mcp_keys),
                    )
                    mcp_rows = list(cur.fetchall())
                    mcp_map = {row["server_key"]: row["id"] for row in mcp_rows}
                    missing = [key for key in mcp_keys if key not in mcp_map]
                    if missing:
                        raise ValueError(f"存在不存在或未启用的 MCP Server: {missing}")
                else:
                    mcp_map = {}

                agent_keys = payload.get("agents", [])
                if agent_keys:
                    cur.execute(
                        f"""
                        select id, agent_id
                        from agents
                        where enabled = true and agent_id in ({placeholders(agent_keys)})
                        """,
                        tuple(agent_keys),
                    )
                    agent_rows = list(cur.fetchall())
                    agent_map = {row["agent_id"]: row["id"] for row in agent_rows}
                    missing = [key for key in agent_keys if key not in agent_map]
                    if missing:
                        raise ValueError(f"存在不存在或未启用的 Agent: {missing}")
                else:
                    agent_map = {}

                if payload.get("is_default"):
                    cur.execute(
                        "update bots set is_default = false, updated_at = now() where bot_id <> %s",
                        (payload_bot_id,),
                    )

                if existing:
                    cur.execute(
                        """
                        update bots
                        set name = %s,
                            system_prompt = %s,
                            model = %s,
                            temperature = %s,
                            max_tokens = %s,
                            max_response_chars = %s,
                            tts_profile_id = %s,
                            enabled = %s,
                            is_default = %s,
                            updated_at = now()
                        where bot_id = %s
                        """,
                        (
                            payload["name"],
                            payload["system_prompt"],
                            payload["model"],
                            payload["temperature"],
                            payload["max_tokens"],
                            payload.get("max_response_chars", 0),
                            payload.get("tts_profile_id") or DEFAULT_TTS_PROFILE_ID,
                            payload.get("enabled", True),
                            payload.get("is_default", False),
                            payload_bot_id,
                        ),
                    )
                    bot_db_id = existing["id"]
                else:
                    cur.execute(
                        """
                        insert into bots (
                            bot_id, name, system_prompt, model, temperature, max_tokens,
                            max_response_chars, tts_profile_id, enabled, is_default
                        )
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            payload_bot_id,
                            payload["name"],
                            payload["system_prompt"],
                            payload["model"],
                            payload["temperature"],
                            payload["max_tokens"],
                            payload.get("max_response_chars", 0),
                            payload.get("tts_profile_id") or DEFAULT_TTS_PROFILE_ID,
                            payload.get("enabled", True),
                            payload.get("is_default", False),
                        ),
                    )
                    bot_db_id = cur.lastrowid

                cur.execute("delete from bot_mcp_bindings where bot_id = %s", (bot_db_id,))
                for sort_order, server_key in enumerate(mcp_keys):
                    cur.execute(
                        """
                        insert into bot_mcp_bindings (bot_id, mcp_server_id, sort_order)
                        values (%s, %s, %s)
                        """,
                        (bot_db_id, mcp_map[server_key], sort_order),
                    )

                cur.execute("delete from bot_agent_bindings where bot_id = %s", (bot_db_id,))
                for sort_order, agent_id in enumerate(agent_keys):
                    cur.execute(
                        """
                        insert into bot_agent_bindings (bot_id, agent_id, sort_order)
                        values (%s, %s, %s)
                        """,
                        (bot_db_id, agent_map[agent_id], sort_order),
                    )

                self._assert_single_default_bot(cur)
                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

        item = self.get_bot(payload_bot_id)
        if item is None:
            raise ValueError(f"保存后未找到 Bot: {payload_bot_id}")
        return item

    def list_tts_profiles(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = self._fetch_all(
                conn,
                """
                select tts_id, tts_name, provider_type, speed, provider_config_json, enabled, updated_at
                from tts_profiles
                order by tts_id
                """,
            )
        return [self._flatten_tts_profile_row(row) for row in rows]

    def get_tts_profile(self, tts_id: str) -> dict[str, Any] | None:
        tts_id = tts_id.strip()
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                """
                select tts_id, tts_name, provider_type, speed, provider_config_json, enabled, updated_at
                from tts_profiles
                where tts_id = %s
                """,
                (tts_id,),
            )
        if "tts_id" not in row:
            return None
        return self._flatten_tts_profile_row(row)

    def upsert_tts_profile(self, payload: dict[str, Any], *, created_by: str = "admin-api") -> dict[str, Any]:
        tts_id = str(payload["tts_id"]).strip()
        tts_name = str(payload["tts_name"]).strip()
        provider_type = str(payload["provider_type"]).strip()
        speed = float(payload.get("speed") or 1.0)
        enabled = bool(payload.get("enabled", True))
        provider_config = self._build_tts_provider_config(payload)

        if not tts_id:
            raise ValueError("tts_id 不能为空")
        if not tts_name:
            raise ValueError("tts_name 不能为空")
        if provider_type not in TTS_PROVIDER_TYPES:
            raise ValueError(f"provider_type 非法: {provider_type}")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("speed 必须在 0.5 到 2.0 之间")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id from tts_profiles where tts_id = %s", (tts_id,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        update tts_profiles
                        set tts_name = %s,
                            provider_type = %s,
                            speed = %s,
                            provider_config_json = %s,
                            enabled = %s,
                            updated_at = now()
                        where tts_id = %s
                        """,
                        (
                            tts_name,
                            provider_type,
                            speed,
                            json.dumps(provider_config, ensure_ascii=False),
                            enabled,
                            tts_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        insert into tts_profiles (
                            tts_id, tts_name, provider_type, speed, provider_config_json, enabled
                        )
                        values (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tts_id,
                            tts_name,
                            provider_type,
                            speed,
                            json.dumps(provider_config, ensure_ascii=False),
                            enabled,
                        ),
                    )
                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

        item = self.get_tts_profile(tts_id)
        if item is None:
            raise ValueError(f"保存后未找到 TTS Profile: {tts_id}")
        return item

    def delete_tts_profile(self, tts_id: str, *, created_by: str = "admin-api") -> None:
        tts_id = tts_id.strip()
        if not tts_id:
            raise ValueError("tts_id 不能为空")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id from tts_profiles where tts_id = %s", (tts_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"TTS Profile 不存在: {tts_id}")

                cur.execute("select bot_id from bots where tts_profile_id = %s order by bot_id", (tts_id,))
                bot_rows = list(cur.fetchall())
                if bot_rows:
                    bot_ids = ", ".join(row["bot_id"] for row in bot_rows)
                    raise ValueError(f"该 TTS Profile 正被 Bot 使用，不能删除: {bot_ids}")

                cur.execute("delete from tts_profiles where tts_id = %s", (tts_id,))
                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

    def delete_bot(self, bot_id: str, *, created_by: str = "admin-api") -> None:
        bot_id = bot_id.strip()
        if not bot_id:
            raise ValueError("bot_id 不能为空")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id, is_default from bots where bot_id = %s", (bot_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Bot 不存在: {bot_id}")
                if row["is_default"]:
                    raise ValueError("默认 Bot 不能删除，请先把其他 Bot 设为默认")

                cur.execute(
                    "select robot_id from robots where assigned_bot_id = %s order by robot_id",
                    (bot_id,),
                )
                robot_rows = list(cur.fetchall())
                if robot_rows:
                    robot_ids = ", ".join(row["robot_id"] for row in robot_rows)
                    raise ValueError(f"该 Bot 仍被 Robot 使用，不能删除: {robot_ids}")

                cur.execute("delete from bots where bot_id = %s", (bot_id,))
                self._assert_single_default_bot(cur)
                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

    def list_robots(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return self._fetch_all(
                conn,
                """
                select robot_id, name, assigned_bot_id, client_type, enabled, is_new,
                       last_connected_at, notes, updated_at,
                       (robot_secret_hash is not null) as secret_configured,
                       robot_secret_updated_at
                from robots
                order by robot_id
                """,
            )

    def get_robot(self, robot_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                """
                select robot_id, name, assigned_bot_id, client_type, enabled, is_new,
                       last_connected_at, notes, updated_at,
                       (robot_secret_hash is not null) as secret_configured,
                       robot_secret_updated_at
                from robots
                where robot_id = %s
                """,
                (robot_id,),
            )
        return row if "robot_id" in row else None

    def upsert_robot(self, payload: dict[str, Any], *, created_by: str = "admin-api") -> dict[str, Any]:
        robot_id = payload["robot_id"].strip()
        if not robot_id:
            raise ValueError("robot_id 不能为空")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select bot_id from bots where bot_id = %s and enabled = true",
                    (payload["assigned_bot_id"],),
                )
                bot = cur.fetchone()
                if not bot:
                    raise ValueError(f"绑定的 Bot 不存在或未启用: {payload['assigned_bot_id']}")

                cur.execute("select id from robots where robot_id = %s", (robot_id,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        update robots
                        set name = %s,
                            assigned_bot_id = %s,
                            client_type = %s,
                            enabled = %s,
                            is_new = %s,
                            notes = %s,
                            updated_at = now()
                        where robot_id = %s
                        """,
                        (
                            payload["name"],
                            payload["assigned_bot_id"],
                            payload.get("client_type"),
                            payload.get("enabled", True),
                            payload.get("is_new", False),
                            payload.get("notes"),
                            robot_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        insert into robots (
                            robot_id, name, assigned_bot_id, client_type, enabled, is_new, notes
                        )
                        values (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            robot_id,
                            payload["name"],
                            payload["assigned_bot_id"],
                            payload.get("client_type"),
                            payload.get("enabled", True),
                            payload.get("is_new", False),
                            payload.get("notes"),
                        ),
                    )

                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

        item = self.get_robot(robot_id)
        if item is None:
            raise ValueError(f"保存后未找到 Robot: {robot_id}")
        return item

    def delete_robot(self, robot_id: str, *, created_by: str = "admin-api") -> None:
        robot_id = robot_id.strip()
        if not robot_id:
            raise ValueError("robot_id 不能为空")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id from robots where robot_id = %s", (robot_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Robot 不存在: {robot_id}")

                cur.execute("delete from robots where robot_id = %s", (robot_id,))
                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

    def list_mcp_servers(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            items = self._fetch_all(
                conn,
                """
                select server_key, display_name, type, url, headers_json, enabled, updated_at
                , command, args_json
                from mcp_servers
                order by server_key
                """,
            )
        return [self._normalize_mcp_item(item) for item in items]

    def get_mcp_server(self, server_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                """
                select server_key, display_name, type, url, headers_json, enabled, updated_at
                , command, args_json
                from mcp_servers
                where server_key = %s
                """,
                (server_key,),
            )
        return self._normalize_mcp_item(row) if "server_key" in row else None

    def upsert_mcp_server(self, payload: dict[str, Any], *, created_by: str = "admin-api") -> dict[str, Any]:
        server_key = payload["server_key"].strip()
        server_type = payload["type"]
        if not server_key:
            raise ValueError("server_key 不能为空")
        if server_type not in MCP_TYPE_OPTIONS:
            raise ValueError(f"type 必须是 {MCP_TYPE_OPTIONS} 之一")
        if server_type in {"sse", "streamable_http"} and not (payload.get("url") or "").strip():
            raise ValueError("远程 MCP Server 必须配置 url")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id, enabled from mcp_servers where server_key = %s", (server_key,))
                existing = cur.fetchone()

                if existing:
                    enabled = payload["enabled"] if "enabled" in payload else bool(existing["enabled"])
                    cur.execute(
                        """
                        update mcp_servers
                        set display_name = %s,
                            type = %s,
                            url = %s,
                            command = %s,
                            args_json = %s,
                            headers_json = %s,
                            enabled = %s,
                            updated_at = now()
                        where server_key = %s
                        """,
                        (
                            payload["display_name"],
                            server_type,
                            payload.get("url"),
                            payload.get("command"),
                            json.dumps(payload.get("args_json") or [], ensure_ascii=False),
                            json.dumps(payload.get("headers_json") or {}, ensure_ascii=False),
                            enabled,
                            server_key,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        insert into mcp_servers (
                            server_key, display_name, type, url, command, args_json, headers_json, enabled
                        )
                        values (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            server_key,
                            payload["display_name"],
                            server_type,
                            payload.get("url"),
                            payload.get("command"),
                            json.dumps(payload.get("args_json") or [], ensure_ascii=False),
                            json.dumps(payload.get("headers_json") or {}, ensure_ascii=False),
                            payload.get("enabled", True),
                        ),
                    )

                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

        item = self.get_mcp_server(server_key)
        if item is None:
            raise ValueError(f"保存后未找到 MCP Server: {server_key}")
        return item

    def delete_mcp_server(self, server_key: str, *, created_by: str = "admin-api") -> None:
        server_key = server_key.strip()
        if not server_key:
            raise ValueError("server_key 不能为空")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id from mcp_servers where server_key = %s", (server_key,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"MCP Server 不存在: {server_key}")

                cur.execute("delete from mcp_servers where server_key = %s", (server_key,))
                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

    def list_agents(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return self._fetch_all(
                conn,
                """
                select agent_id, name, description, module, class_name, enabled,
                       enabled_by_default, trigger_examples_json, allowed_tools_json,
                       tool_groups_json, updated_at
                from agents
                order by agent_id
                """,
            )

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = self._fetch_one(
                conn,
                """
                select agent_id, name, description, module, class_name, enabled,
                       enabled_by_default, trigger_examples_json, allowed_tools_json,
                       tool_groups_json, updated_at
                from agents
                where agent_id = %s
                """,
                (agent_id,),
            )
        return row if "agent_id" in row else None

    def upsert_agent(self, payload: dict[str, Any], *, created_by: str = "admin-api") -> dict[str, Any]:
        agent_id = payload["agent_id"].strip()
        if not agent_id:
            raise ValueError("agent_id 不能为空")

        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id, enabled from agents where agent_id = %s", (agent_id,))
                existing = cur.fetchone()
                enabled = payload["enabled"] if "enabled" in payload else bool(existing["enabled"]) if existing else True
                if existing:
                    if not enabled:
                        cur.execute("delete from bot_agent_bindings where agent_id = %s", (existing["id"],))
                    cur.execute(
                        """
                        update agents
                        set name = %s,
                            description = %s,
                            module = %s,
                            class_name = %s,
                            enabled = %s,
                            enabled_by_default = %s,
                            trigger_examples_json = %s,
                            allowed_tools_json = %s,
                            tool_groups_json = %s,
                            updated_at = now()
                        where agent_id = %s
                        """,
                        (
                            payload["name"],
                            payload.get("description") or "",
                            payload.get("module"),
                            payload.get("class_name"),
                            enabled,
                            payload.get("enabled_by_default", True),
                            json.dumps(payload.get("trigger_examples_json") or [], ensure_ascii=False),
                            json.dumps(payload.get("allowed_tools_json") or [], ensure_ascii=False),
                            json.dumps(payload.get("tool_groups_json") or [], ensure_ascii=False),
                            agent_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        insert into agents (
                            agent_id, name, description, module, class_name, enabled,
                            enabled_by_default, trigger_examples_json, allowed_tools_json, tool_groups_json
                        )
                        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            agent_id,
                            payload["name"],
                            payload.get("description") or "",
                            payload.get("module"),
                            payload.get("class_name"),
                            enabled,
                            payload.get("enabled_by_default", True),
                            json.dumps(payload.get("trigger_examples_json") or [], ensure_ascii=False),
                            json.dumps(payload.get("allowed_tools_json") or [], ensure_ascii=False),
                            json.dumps(payload.get("tool_groups_json") or [], ensure_ascii=False),
                        ),
                    )

                self._create_snapshot(cur, created_by=created_by)
            conn.commit()

        item = self.get_agent(agent_id)
        if item is None:
            raise ValueError(f"保存后未找到 Agent: {agent_id}")
        return item

    def sync_discovered_agents(
        self,
        agents: list[dict[str, Any]],
        *,
        created_by: str = "agent-discovery",
    ) -> bool:
        """Upsert locally discovered Agent metadata without changing admin enabled flags."""
        if not agents:
            return False

        changed = False
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select agent_id, name, description, module, class_name,
                           enabled_by_default, trigger_examples_json,
                           allowed_tools_json, tool_groups_json
                    from agents
                    """
                )
                existing_map = {row["agent_id"]: decode_row(row) for row in cur.fetchall()}

                for agent in agents:
                    agent_id = str(agent["agent_id"]).strip()
                    if not agent_id:
                        continue
                    normalized = {
                        "agent_id": agent_id,
                        "name": str(agent.get("name") or agent_id).strip(),
                        "description": str(agent.get("description") or "").strip(),
                        "module": agent.get("module"),
                        "class_name": agent.get("class_name"),
                        "enabled_by_default": bool(agent.get("enabled_by_default", True)),
                        "trigger_examples_json": list(agent.get("trigger_examples_json") or []),
                        "allowed_tools_json": list(agent.get("allowed_tools_json") or []),
                        "tool_groups_json": list(agent.get("tool_groups_json") or []),
                    }
                    existing = existing_map.get(agent_id)
                    if existing:
                        metadata_changed = any(
                            existing.get(key) != value
                            for key, value in normalized.items()
                            if key != "agent_id"
                        )
                        if metadata_changed:
                            cur.execute(
                                """
                                update agents
                                set name = %s,
                                    description = %s,
                                    module = %s,
                                    class_name = %s,
                                    enabled_by_default = %s,
                                    trigger_examples_json = %s,
                                    allowed_tools_json = %s,
                                    tool_groups_json = %s,
                                    updated_at = now()
                                where agent_id = %s
                                """,
                                (
                                    normalized["name"],
                                    normalized["description"],
                                    normalized["module"],
                                    normalized["class_name"],
                                    normalized["enabled_by_default"],
                                    json.dumps(normalized["trigger_examples_json"], ensure_ascii=False),
                                    json.dumps(normalized["allowed_tools_json"], ensure_ascii=False),
                                    json.dumps(normalized["tool_groups_json"], ensure_ascii=False),
                                    agent_id,
                                ),
                            )
                            changed = True
                    else:
                        cur.execute(
                            """
                            insert into agents (
                                agent_id, name, description, module, class_name, enabled,
                                enabled_by_default, trigger_examples_json, allowed_tools_json, tool_groups_json
                            )
                            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                agent_id,
                                normalized["name"],
                                normalized["description"],
                                normalized["module"],
                                normalized["class_name"],
                                normalized["enabled_by_default"],
                                normalized["enabled_by_default"],
                                json.dumps(normalized["trigger_examples_json"], ensure_ascii=False),
                                json.dumps(normalized["allowed_tools_json"], ensure_ascii=False),
                                json.dumps(normalized["tool_groups_json"], ensure_ascii=False),
                            ),
                        )
                        changed = True

                if changed:
                    self._create_snapshot(cur, created_by=created_by)
            conn.commit()

        return changed

    def get_options(self) -> dict[str, Any]:
        with self.connection() as conn:
            models = self._fetch_all(conn, "select distinct model from bots order by model")
            bots = self._fetch_all(conn, "select bot_id as value, name as label from bots order by bot_id")
            enabled_mcp = self._fetch_all(
                conn,
                """
                select server_key as value, display_name as label
                from mcp_servers
                where enabled = true
                order by server_key
                """,
            )
            enabled_agents = self._fetch_all(
                conn,
                """
                select agent_id as value, name as label
                from agents
                where enabled = true
                order by agent_id
                """,
            )
            tts_profiles = self._fetch_all(
                conn,
                """
                select tts_id as value, tts_name as label, enabled
                from tts_profiles
                order by tts_id
                """,
            )

        model_values = sorted({row["model"] for row in models} | set(MODEL_OPTIONS))
        return {
            "models": model_values,
            "tts_custom_voices": list(QWEN3_CUSTOM_VOICE_OPTIONS),
            "tts_profiles": [
                {
                    "value": row["value"],
                    "label": row["label"] if row.get("enabled") else f"{row['label']} (disabled)",
                }
                for row in tts_profiles
            ],
            "mcp_types": list(MCP_TYPE_OPTIONS),
            "enabled_mcp_servers": enabled_mcp,
            "enabled_agents": enabled_agents,
            "bots": bots,
        }

    def _resolve_runtime_rows(
        self,
        *,
        version: int | None = None,
    ) -> tuple[int, dict[str, list[dict[str, Any]]], str]:
        with self.connection() as conn:
            if version is None:
                config_version = self.get_latest_config_version(conn)
                return config_version, self._fetch_current_runtime_rows(conn), "db"

            snapshot_row = self._fetch_one(
                conn,
                """
                select config_version, snapshot_json
                from config_snapshots
                where config_version = %s
                """,
                (version,),
            )
            if "config_version" not in snapshot_row:
                raise ValueError(f"配置快照不存在: v{version}")
            rows = self._rows_from_snapshot_payload(snapshot_row["snapshot_json"])
            return int(snapshot_row["config_version"]), rows, "snapshot"

    def _fetch_current_runtime_rows(self, conn: Any) -> dict[str, list[dict[str, Any]]]:
        return {
            "bot_rows": self._fetch_all(
                conn,
                """
                select
                  id,
                  bot_id,
                  name,
                  system_prompt,
                  model,
                  temperature,
                  max_tokens,
                  max_response_chars,
                  tts_profile_id,
                  enabled,
                  is_default
                from bots
                order by id
                """,
            ),
            "robot_rows": self._fetch_all(
                conn,
                """
                select
                  id,
                  robot_id,
                  name,
                  assigned_bot_id,
                  client_type,
                  enabled,
                  is_new,
                  last_connected_at,
                  notes
                from robots
                order by id
                """,
            ),
            "mcp_rows": self._fetch_all(
                conn,
                """
                select
                  id,
                  server_key,
                  display_name,
                  type,
                  url,
                  command,
                  args_json,
                  headers_json,
                  enabled
                from mcp_servers
                order by id
                """,
            ),
            "binding_rows": self._fetch_all(
                conn,
                """
                select
                  id,
                  bm.bot_id,
                  bm.mcp_server_id,
                  bm.sort_order
                from bot_mcp_bindings bm
                order by bm.sort_order, bm.id
                """,
            ),
            "agent_rows": self._fetch_all(
                conn,
                """
                select
                  id,
                  agent_id,
                  name,
                  description,
                  module,
                  class_name,
                  enabled,
                  enabled_by_default,
                  trigger_examples_json,
                  allowed_tools_json,
                  tool_groups_json
                from agents
                order by id
                """,
            ),
            "agent_binding_rows": self._fetch_all(
                conn,
                """
                select
                  id,
                  ba.bot_id,
                  ba.agent_id,
                  ba.sort_order
                from bot_agent_bindings ba
                order by ba.sort_order, ba.id
                """,
            ),
            "service_rows": self._fetch_all(
                conn,
                """
                select service_name, config_json, updated_at
                from service_configs
                order by service_name
                """,
            ),
            "tts_profile_rows": self._fetch_all(
                conn,
                """
                select
                  id,
                  tts_id,
                  tts_name,
                  provider_type,
                  speed,
                  provider_config_json,
                  enabled
                from tts_profiles
                order by id
                """,
            ),
        }

    def _rows_from_snapshot_payload(self, payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        bot_rows = [dict(item) for item in payload.get("bots", [])]
        robot_rows = [dict(item) for item in payload.get("robots", [])]
        mcp_rows = [dict(item) for item in payload.get("mcp_servers", [])]
        binding_rows = [dict(item) for item in payload.get("bot_mcp_bindings", [])]
        agent_rows = [dict(item) for item in payload.get("agents", [])]
        agent_binding_rows = [dict(item) for item in payload.get("bot_agent_bindings", [])]
        service_rows = [dict(item) for item in payload.get("service_configs", [])]
        tts_profile_rows = [dict(item) for item in payload.get("tts_profiles", [])]

        for row in robot_rows:
            row["last_connected_at"] = self._parse_optional_datetime(row.get("last_connected_at"))

        return {
            "bot_rows": bot_rows,
            "robot_rows": robot_rows,
            "mcp_rows": mcp_rows,
            "binding_rows": binding_rows,
            "agent_rows": agent_rows,
            "agent_binding_rows": agent_binding_rows,
            "service_rows": service_rows,
            "tts_profile_rows": tts_profile_rows,
        }

    @staticmethod
    def _build_tts_provider_config(payload: dict[str, Any]) -> dict[str, str]:
        provider_type = str(payload.get("provider_type") or "").strip()
        if provider_type == "qwen3_custom_voice":
            voice = str(payload.get("voice") or "").strip()
            instruct = str(payload.get("instruct") or "").strip()
            if not voice:
                raise ValueError("CustomVoice TTS Profile 缺少 voice")
            if not instruct:
                raise ValueError("CustomVoice TTS Profile 缺少 instruct")
            return {"voice": voice, "instruct": instruct}

        if provider_type == "qwen3_base":
            path = str(payload.get("path") or "").strip()
            content = str(payload.get("content") or "").strip()
            if not path:
                raise ValueError("Base TTS Profile 缺少 path")
            if not path.startswith("/"):
                raise ValueError("Base TTS Profile path 必须是 vLLM 容器内绝对路径")
            if not content:
                raise ValueError("Base TTS Profile 缺少 content")
            return {"path": path, "content": content}

        raise ValueError(f"provider_type 非法: {provider_type}")

    @staticmethod
    def _flatten_tts_profile_row(row: dict[str, Any]) -> dict[str, Any]:
        provider_config = dict(row.get("provider_config_json") or {})
        return {
            "tts_id": row["tts_id"],
            "tts_name": row["tts_name"],
            "provider_type": row["provider_type"],
            "speed": float(row.get("speed") or 1.0),
            "enabled": bool(row.get("enabled", True)),
            "voice": provider_config.get("voice") or "",
            "instruct": provider_config.get("instruct") or "",
            "path": provider_config.get("path") or "",
            "content": provider_config.get("content") or "",
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _fetch_all(
        conn: Any,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return decode_rows(list(cur.fetchall()))

    @staticmethod
    def _fetch_one(
        conn: Any,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> dict[str, Any]:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return decode_row(row) if row is not None else {"version": 1}

    @staticmethod
    def _attach_bot_bindings(
        bots: list[dict[str, Any]],
        bindings: list[dict[str, Any]],
        agent_bindings: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        binding_map: dict[str, list[str]] = {}
        for row in bindings:
            binding_map.setdefault(row["bot_id"], []).append(row["server_key"])
        agent_binding_map: dict[str, list[str]] = {}
        for row in agent_bindings or []:
            agent_binding_map.setdefault(row["bot_id"], []).append(row["agent_id"])
        for row in bots:
            row["mcp_servers"] = binding_map.get(row["bot_id"], [])
            row["agents"] = agent_binding_map.get(row["bot_id"], [])
        return bots

    @staticmethod
    def _assert_single_default_bot(cur: Any) -> None:
        cur.execute(
            "select count(*) as count from bots where enabled = true and is_default = true"
        )
        row = cur.fetchone()
        if int(row["count"]) != 1:
            raise ValueError("必须且只能有一个启用中的默认 Bot")

    def _create_snapshot(self, cur: Any, *, created_by: str) -> int:
        conn = self._cursor_connection(cur)
        next_version = self.get_latest_config_version(conn) + 1
        snapshot = self._build_snapshot_payload(conn, config_version=next_version)
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
        return next_version

    @staticmethod
    def _cursor_connection(cur: Any) -> Any:
        return getattr(cur, "connection", None) or cur._get_db()

    def _build_snapshot_payload(self, conn: Any, *, config_version: int) -> dict[str, Any]:
        bots = self._fetch_all(conn, "select * from bots order by id")
        robots = self._fetch_all(conn, "select * from robots order by id")
        mcp_servers = self._fetch_all(conn, "select * from mcp_servers order by id")
        bindings = self._fetch_all(conn, "select * from bot_mcp_bindings order by sort_order, id")
        agents = self._fetch_all(conn, "select * from agents order by id")
        agent_bindings = self._fetch_all(conn, "select * from bot_agent_bindings order by sort_order, id")
        service_configs = self._fetch_all(conn, "select * from service_configs order by service_name")
        tts_profiles = self._fetch_all(conn, "select * from tts_profiles order by id")
        return {
            "config_version": config_version,
            "bots": self._to_jsonable(bots),
            "tts_profiles": self._to_jsonable(tts_profiles),
            "robots": self._to_jsonable(robots),
            "mcp_servers": self._to_jsonable(mcp_servers),
            "bot_mcp_bindings": self._to_jsonable(bindings),
            "agents": self._to_jsonable(agents),
            "bot_agent_bindings": self._to_jsonable(agent_bindings),
            "service_configs": self._to_jsonable(service_configs),
        }

    @staticmethod
    def _to_jsonable(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: ConfigRepository._to_jsonable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [ConfigRepository._to_jsonable(item) for item in value]
        if isinstance(value, tuple):
            return [ConfigRepository._to_jsonable(item) for item in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

    @staticmethod
    def _parse_optional_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    @staticmethod
    def _normalize_mcp_item(item: dict[str, Any]) -> dict[str, Any]:
        item = dict(item)
        item["headers_json"] = item.get("headers_json") or {}
        item["args_json"] = item.get("args_json") or []
        return item
