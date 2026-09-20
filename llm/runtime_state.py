"""
LLM runtime state backed by server-config snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import Any

from llm.bot_manager import BotConfig, BotManager
from llm.mcp_manager import MCPManager
from server_config.models import utcnow
from server_config.repository import ConfigRepository


logger = logging.getLogger(__name__)


class LLMRuntimeState:
    def __init__(
        self,
        *,
        database_url: str,
        mcp_enabled: bool,
    ) -> None:
        self.database_url = (database_url or "").strip()
        if not self.database_url:
            raise ValueError("LLM runtime 缺少 CONFIG_DATABASE_URL，当前分支要求从数据库加载配置")

        self.repository = ConfigRepository(self.database_url)
        self.mcp_enabled = mcp_enabled
        self._lock = threading.RLock()
        self._bot_manager = BotManager()
        self._mcp_manager = None
        self._agents: dict[str, Any] = {}
        self._source = "db"
        self._config_version: int | None = None
        self._loaded_at = utcnow()
        self._default_bot_id = BotManager.DEFAULT_BOT_ID
        self._last_reload_error: str | None = None
        self._warmup_task: asyncio.Task | None = None

        try:
            self._load_from_database()
        except Exception as exc:
            logger.exception("从数据库加载 LLM 配置失败: %s", exc)
            self._last_reload_error = str(exc)
            self._loaded_at = utcnow()
            raise
        self.schedule_mcp_warmup()

    @property
    def bot_manager(self) -> BotManager:
        with self._lock:
            return self._bot_manager

    @property
    def mcp_manager(self) -> MCPManager | None:
        with self._lock:
            return self._mcp_manager

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            bot_manager = self._bot_manager
            mcp_manager = self._mcp_manager
            return {
                "success": True,
                "source": self._source,
                "config_version": self._config_version,
                "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
                "bot_count": len(bot_manager.list_bots()),
                "mcp_count": len(mcp_manager.servers) if mcp_manager else 0,
                "agent_count": len(getattr(self, "_agents", {}) or {}),
                "default_bot_id": self._default_bot_id,
                "last_reload_error": self._last_reload_error,
                "mcp_status": mcp_manager.get_runtime_status() if mcp_manager else [],
            }

    def enabled_agent_ids(self) -> set[str]:
        with self._lock:
            return set(self._agents.keys())

    async def validate(self, version: int | None = None) -> dict[str, Any]:
        _, _, snapshot = await asyncio.to_thread(self._build_runtime_components, version)
        return {
            "success": True,
            "source": snapshot.source,
            "config_version": snapshot.config_version,
            "validated_at": utcnow().isoformat(),
            "default_bot_id": snapshot.default_bot_id,
            "bot_count": len(snapshot.bots),
            "mcp_count": len(snapshot.mcp_servers),
            "agent_count": len(snapshot.agents),
        }

    async def reload(self, version: int | None = None) -> dict[str, Any]:
        old_mcp_manager = None
        with self._lock:
            old_mcp_manager = self._mcp_manager
            old_warmup_task = self._warmup_task
            self._warmup_task = None
            if old_warmup_task and not old_warmup_task.done():
                old_warmup_task.cancel()

        try:
            bot_manager, mcp_manager, snapshot = await asyncio.to_thread(self._build_runtime_components, version)
            with self._lock:
                self._bot_manager = bot_manager
                self._mcp_manager = mcp_manager
                self._agents = dict(snapshot.agents)
                self._source = snapshot.source
                self._config_version = snapshot.config_version
                self._loaded_at = snapshot.loaded_at
                self._default_bot_id = snapshot.default_bot_id
                self._last_reload_error = None
        except Exception as exc:
            logger.exception("重新加载 LLM runtime 配置失败: %s", exc)
            with self._lock:
                self._last_reload_error = str(exc)
            raise
        finally:
            with self._lock:
                current_mcp_manager = self._mcp_manager
            if old_mcp_manager is not None and old_mcp_manager is not current_mcp_manager:
                try:
                    await old_mcp_manager.disconnect_all()
                except Exception as exc:
                    logger.warning("关闭旧 MCP manager 失败: %s", exc)

        self.schedule_mcp_warmup()
        warmup = await self.wait_for_mcp_warmup()
        status = self.get_status()
        status["mcp_warmup"] = warmup
        return status

    async def shutdown(self) -> None:
        """Stop background warmup and close MCP connections before the loop exits."""
        with self._lock:
            warmup_task = self._warmup_task
            self._warmup_task = None
            mcp_manager = self._mcp_manager

        if warmup_task and not warmup_task.done():
            warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await warmup_task

        if mcp_manager is not None:
            try:
                await mcp_manager.disconnect_all()
            except Exception as exc:
                logger.warning("关闭 MCP manager 失败: %s", exc)

    def schedule_mcp_warmup(self) -> None:
        if not self.mcp_enabled:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.info("LLM runtime 当前不在事件循环中，跳过自动 MCP warm up")
            return

        with self._lock:
            if self._warmup_task and not self._warmup_task.done():
                self._warmup_task.cancel()
            self._warmup_task = loop.create_task(self.warmup_mcp_servers())

    async def wait_for_mcp_warmup(self) -> dict[str, Any]:
        """等待当前预热完成，使服务就绪/配置 Apply 不把连接延迟留给首个用户。"""
        with self._lock:
            warmup_task = self._warmup_task
        if warmup_task is None:
            return await self.warmup_mcp_servers()
        return await asyncio.shield(warmup_task)

    async def warmup_mcp_servers(self, timeout_seconds: float = 10.0) -> dict[str, Any]:
        with self._lock:
            bot_manager = self._bot_manager
            mcp_manager = self._mcp_manager

        if not self.mcp_enabled or not mcp_manager:
            return {"success": True, "total": 0, "connected": 0, "failed": 0, "errors": {}}

        server_names = self._collect_warmup_server_names(bot_manager, mcp_manager)
        if not server_names:
            return {"success": True, "total": 0, "connected": 0, "failed": 0, "errors": {}}

        logger.info("开始 MCP warm up（预连接 Bot 绑定的 MCP Server）: %s", ", ".join(server_names))
        results = await asyncio.gather(
            *[
                self._warmup_one_mcp_server(mcp_manager, server_name, timeout_seconds)
                for server_name in server_names
            ],
            return_exceptions=False,
        )
        errors = {name: message for name, ok, message in results if not ok}
        summary = {
            "success": not errors,
            "total": len(server_names),
            "connected": len(server_names) - len(errors),
            "failed": len(errors),
            "errors": errors,
        }
        if errors:
            logger.warning("MCP warm up 完成，部分预连接失败: %s", errors)
        else:
            logger.info("MCP warm up 完成，全部目标已可用")
        return summary

    async def reconnect_mcp_server(self, server_name: str, timeout_seconds: float = 10.0) -> dict[str, Any]:
        with self._lock:
            mcp_manager = self._mcp_manager

        if not self.mcp_enabled or not mcp_manager:
            raise ValueError("MCP 当前未启用")
        if server_name not in mcp_manager.servers:
            raise ValueError(f"MCP Server 不存在于当前运行时: {server_name}")

        await mcp_manager.disconnect_server(server_name)
        name, ok, message = await self._warmup_one_mcp_server(mcp_manager, server_name, timeout_seconds)
        status = next(
            (item for item in mcp_manager.get_runtime_status() if item.get("server_key") == server_name),
            None,
        )
        return {
            "success": ok,
            "server_key": name,
            "message": message or "重连成功",
            "status": status,
        }

    @staticmethod
    def _collect_warmup_server_names(bot_manager: BotManager, mcp_manager: MCPManager) -> list[str]:
        names: set[str] = set()
        for bot in bot_manager.list_bots():
            names.update(bot.mcp_servers)
        return sorted(name for name in names if name in mcp_manager.servers)

    @staticmethod
    async def _warmup_one_mcp_server(
        mcp_manager: MCPManager,
        server_name: str,
        timeout_seconds: float,
    ) -> tuple[str, bool, str | None]:
        try:
            await asyncio.wait_for(mcp_manager.connect_server(server_name), timeout=timeout_seconds)
            return server_name, True, None
        except asyncio.TimeoutError:
            message = f"连接超时（超过 {timeout_seconds:.0f} 秒）"
            mcp_manager.mark_connection_error(server_name, message)
            logger.warning("MCP warm up 失败: %s - %s", server_name, message)
            return server_name, False, message
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            mcp_manager.mark_connection_error(server_name, message)
            logger.warning("MCP warm up 失败: %s - %s", server_name, message)
            return server_name, False, message

    def _build_runtime_components(self, version: int | None = None) -> tuple[BotManager, MCPManager | None, Any]:
        snapshot = self.repository.load_llm_runtime_snapshot(version=version)
        bot_manager = BotManager(
            initial_bots={
                bot_id: BotConfig(
                    bot_id=bot.bot_id,
                    name=bot.name,
                    system_prompt=bot.system_prompt,
                    model=bot.model,
                    temperature=bot.temperature,
                    max_tokens=bot.max_tokens,
                    tts_profile_id=bot.tts_profile_id,
                    max_response_chars=bot.max_response_chars,
                    mcp_servers=list(bot.mcp_servers),
                    agents=list(bot.agents),
                    enabled=bot.enabled,
                    is_default=bot.is_default,
                )
                for bot_id, bot in snapshot.bots.items()
            }
        )
        bot_manager.DEFAULT_BOT_ID = snapshot.default_bot_id

        mcp_manager = self._build_mcp_manager_from_snapshot(snapshot.mcp_servers) if self.mcp_enabled else None
        return bot_manager, mcp_manager, snapshot

    def _load_from_database(self) -> None:
        bot_manager, mcp_manager, snapshot = self._build_runtime_components()
        with self._lock:
            self._bot_manager = bot_manager
            self._mcp_manager = mcp_manager
            self._agents = dict(snapshot.agents)
            self._source = snapshot.source
            self._config_version = snapshot.config_version
            self._loaded_at = snapshot.loaded_at
            self._default_bot_id = snapshot.default_bot_id

    @staticmethod
    def _build_mcp_manager_from_snapshot(servers: dict[str, Any]) -> MCPManager:
        manager = MCPManager()
        for name, config in servers.items():
            manager.add_server(name, config.to_mcp_manager_config())
        return manager
