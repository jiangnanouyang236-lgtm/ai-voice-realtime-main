"""
Gateway runtime state backed by server-config snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from server_config.models import GatewaySettingsConfig, RobotConfig, utcnow
from server_config.repository import ConfigRepository


logger = logging.getLogger(__name__)


class GatewayRuntimeState:
    def __init__(self, *, database_url: str) -> None:
        self.database_url = (database_url or "").strip()
        if not self.database_url:
            raise ValueError("Gateway runtime 缺少 CONFIG_DATABASE_URL，当前分支要求从数据库加载配置")

        self.repository = ConfigRepository(self.database_url)
        self._lock = threading.RLock()
        self._bots: dict[str, Any] = {}
        self._robots: dict[str, RobotConfig] = {}
        self._source = "db"
        self._config_version: int | None = None
        self._loaded_at = utcnow()
        self._default_bot_id: str | None = None
        self._gateway_settings = GatewaySettingsConfig(
            max_connections=20,
            max_history_length=10,
            interrupt_enabled=True,
            stt_service_url="grpc://127.0.0.1:50054",
            llm_service_url="grpc://127.0.0.1:50053",
            tts_service_url="grpc://127.0.0.1:50052",
        )
        self._last_reload_error: str | None = None

        try:
            self._load_from_database()
        except Exception as exc:
            logger.exception("从数据库加载 Gateway 配置失败: %s", exc)
            self._last_reload_error = str(exc)
            self._loaded_at = utcnow()
            raise

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "success": True,
                "source": self._source,
                "config_version": self._config_version,
                "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
                "bot_count": len(self._bots),
                "robot_count": len(self._robots),
                "default_bot_id": self._default_bot_id,
                "gateway_settings": self._gateway_settings.to_dict(),
                "last_reload_error": self._last_reload_error,
            }

    async def validate(self, version: int | None = None) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(self._build_runtime_snapshot, version)
        return {
            "success": True,
            "source": snapshot.source,
            "config_version": snapshot.config_version,
            "validated_at": utcnow().isoformat(),
            "bot_count": len(snapshot.bots),
            "robot_count": len(snapshot.robots),
            "default_bot_id": snapshot.default_bot_id,
            "gateway_settings": snapshot.gateway_settings.to_dict(),
        }

    async def reload(self, version: int | None = None) -> dict[str, Any]:
        try:
            snapshot = await asyncio.to_thread(self._build_runtime_snapshot, version)
            with self._lock:
                self._bots = dict(snapshot.bots)
                self._robots = dict(snapshot.robots)
                self._gateway_settings = snapshot.gateway_settings
                self._source = snapshot.source
                self._config_version = snapshot.config_version
                self._loaded_at = snapshot.loaded_at
                self._default_bot_id = snapshot.default_bot_id
                self._last_reload_error = None
        except Exception as exc:
            logger.exception("重新加载 Gateway runtime 配置失败: %s", exc)
            with self._lock:
                self._last_reload_error = str(exc)
            raise

        return self.get_status()

    def register_robot(
        self,
        robot_id: str,
        client_type: str | None = None,
        *,
        allow_create: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            current_default_bot_id = self._default_bot_id

        row = self.repository.register_robot(
            robot_id,
            client_type,
            default_bot_id=current_default_bot_id,
            allow_create=allow_create,
        )
        if not row.get("enabled", True):
            return row

        with self._lock:
            bot_config = None
            runtime_robot = self._robots.get(robot_id)

            if runtime_robot:
                bot_config = self._bots.get(runtime_robot.assigned_bot_id)
                if not bot_config:
                    raise ValueError(f"Robot {robot_id} 绑定的 Bot 未加载到当前运行时")

                self._robots[robot_id] = RobotConfig(
                    robot_id=runtime_robot.robot_id,
                    name=runtime_robot.name,
                    assigned_bot_id=runtime_robot.assigned_bot_id,
                    client_type=row.get("client_type") or runtime_robot.client_type,
                    enabled=True,
                    is_new=runtime_robot.is_new,
                    last_connected_at=row.get("last_connected_at"),
                    notes=runtime_robot.notes,
                )
                row["assigned_bot_id"] = runtime_robot.assigned_bot_id
                row["is_new"] = runtime_robot.is_new
            elif row.get("created"):
                if not current_default_bot_id:
                    raise ValueError("当前运行时缺少默认 Bot，无法为新 Robot 建立绑定")
                bot_config = self._bots.get(current_default_bot_id)
                if not bot_config:
                    raise ValueError(f"默认 Bot 未加载到当前运行时: {current_default_bot_id}")

                self._robots[robot_id] = RobotConfig(
                    robot_id=row["robot_id"],
                    name=row.get("name") or row["robot_id"],
                    assigned_bot_id=current_default_bot_id,
                    client_type=row.get("client_type"),
                    enabled=True,
                    is_new=bool(row.get("is_new", True)),
                    last_connected_at=row.get("last_connected_at"),
                    notes=row.get("notes"),
                )
                row["assigned_bot_id"] = current_default_bot_id
            else:
                raise ValueError(f"Robot {robot_id} 尚未应用到当前运行时配置，请先 Apply")

            row["bot_name"] = bot_config.name if bot_config else None
            return row

    def verify_robot_secret(self, robot_id: str, secret: str) -> bool:
        return self.repository.verify_robot_secret(robot_id, secret)

    def resolve_robot_bot_binding(self, robot_id: str) -> dict[str, Any] | None:
        with self._lock:
            robot = self._robots.get(robot_id)
            if not robot:
                return None
            bot = self._bots.get(robot.assigned_bot_id)
            if not bot:
                raise ValueError(f"Robot {robot_id} 绑定的 Bot 未加载到当前运行时")
            return {
                "robot_id": robot.robot_id,
                "assigned_bot_id": robot.assigned_bot_id,
                "robot_enabled": robot.enabled,
                "bot_name": bot.name,
            }

    def resolve_legacy_bot(self, bot_id: str | None) -> tuple[str | None, str | None]:
        if not bot_id:
            return None, None
        with self._lock:
            bot = self._bots.get(bot_id)
            if not bot:
                raise ValueError(f"Bot {bot_id} 尚未应用到当前运行时配置，请先 Apply")
            return bot.bot_id, bot.name

    def get_bot_tts_settings(self, bot_id: str | None) -> dict[str, Any] | None:
        if not bot_id:
            return None
        with self._lock:
            bot = self._bots.get(bot_id)
            if not bot:
                raise ValueError(f"Bot {bot_id} 尚未应用到当前运行时配置，请先 Apply")
            return {
                "tts_profile_id": bot.tts_profile_id,
            }

    def list_runtime_robots(self) -> list[dict[str, Any]]:
        with self._lock:
            items: list[dict[str, Any]] = []
            for robot in self._robots.values():
                bot = self._bots.get(robot.assigned_bot_id)
                items.append(
                    {
                        "robot_id": robot.robot_id,
                        "name": robot.name,
                        "assigned_bot_id": robot.assigned_bot_id,
                        "bot_name": bot.name if bot else None,
                        "client_type": robot.client_type,
                        "enabled": robot.enabled,
                        "is_new": robot.is_new,
                        "last_connected_at": robot.last_connected_at.isoformat() if robot.last_connected_at else None,
                        "notes": robot.notes,
                    }
                )

        return sorted(items, key=lambda item: item["robot_id"])

    def get_gateway_settings(self) -> dict[str, Any]:
        with self._lock:
            return self._gateway_settings.to_dict()

    def _build_runtime_snapshot(self, version: int | None = None):
        return self.repository.load_gateway_runtime_snapshot(version=version)

    def _load_from_database(self) -> None:
        snapshot = self._build_runtime_snapshot()
        with self._lock:
            self._bots = dict(snapshot.bots)
            self._robots = dict(snapshot.robots)
            self._gateway_settings = snapshot.gateway_settings
            self._source = snapshot.source
            self._config_version = snapshot.config_version
            self._loaded_at = snapshot.loaded_at
            self._default_bot_id = snapshot.default_bot_id
