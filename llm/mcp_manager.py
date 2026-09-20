"""
MCP 工具管理器

支持 SSE 和 streamable_http 两种远程 MCP Server 连接方式。
"""

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict, List, Any, Optional
import httpx
from mcp import ClientSession
from mcp.client.sse import sse_client

try:
    from mcp.client.streamable_http import streamable_http_client
except ModuleNotFoundError:
    streamable_http_client = None

logger = logging.getLogger(__name__)


_SAFE_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是有效数字，使用默认值 %s", name, value, default)
        return default


def _make_llm_tool_name(tool_name: str, used_names: set[str]) -> str:
    """把内部 MCP 工具名转换成 OpenAI/DashScope 规范内的 function.name。"""
    safe_name = _SAFE_TOOL_NAME_RE.sub("__", tool_name).strip("_-") or "tool"
    safe_name = safe_name[:64]

    if safe_name not in used_names:
        used_names.add(safe_name)
        return safe_name

    suffix = 2
    while True:
        suffix_text = f"_{suffix}"
        candidate = f"{safe_name[:64 - len(suffix_text)]}{suffix_text}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        suffix += 1


class MCPManager:
    """
    MCP 工具管理器

    负责连接 MCP Server、拉取工具列表、调用工具
    """

    def __init__(
        self,
        *,
        connect_timeout_sec: float | None = None,
        tool_timeout_sec: float | None = None,
        http_timeout_sec: float | None = None,
        connect_retry_cooldown_sec: float | None = None,
    ):
        self.servers = {}  # 存储 MCP Server 配置
        self.sessions = {}  # 兼容旧状态字段；新实现不保留跨任务 MCP Session
        self.tools = {}    # 存储所有可用工具
        self.llm_tool_name_map = {}  # LLM 安全工具名 -> 内部真实工具名
        self.contexts = {}  # 兼容旧状态字段；新实现不保留跨任务上下文管理器
        self.runtime_status = {}  # 存储每个 MCP Server 的运行态摘要
        self._lock = asyncio.Lock()  # 异步锁，保护并发访问
        self._connecting = {}  # 记录正在连接的 Server，避免重复连接
        self.connect_timeout_sec = (
            connect_timeout_sec
            if connect_timeout_sec is not None
            else max(0.0, _env_float("MCP_CONNECT_TIMEOUT_SEC", 10.0))
        )
        self.tool_timeout_sec = (
            tool_timeout_sec
            if tool_timeout_sec is not None
            else max(0.0, _env_float("MCP_TOOL_TIMEOUT_SEC", 30.0))
        )
        self.http_timeout_sec = (
            http_timeout_sec
            if http_timeout_sec is not None
            else max(0.0, _env_float("MCP_HTTP_TIMEOUT_SEC", 60.0))
        )
        self.connect_retry_cooldown_sec = (
            connect_retry_cooldown_sec
            if connect_retry_cooldown_sec is not None
            else max(0.0, _env_float("MCP_CONNECT_RETRY_COOLDOWN_SEC", 30.0))
        )
        logger.info("MCP 管理器已初始化")

    async def _with_timeout(self, awaitable, timeout_sec: float, label: str):
        if timeout_sec <= 0:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout_sec)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"{label} 超时（超过 {timeout_sec:.1f} 秒）") from exc

    @staticmethod
    def _build_llm_tool_name_maps(
        tools_snapshot: Dict[str, Dict[str, Any]],
    ) -> tuple[Dict[str, str], Dict[str, str]]:
        used_llm_names: set[str] = set()
        llm_tool_name_map: Dict[str, str] = {}
        tool_to_llm_name: Dict[str, str] = {}

        for tool_name in tools_snapshot:
            llm_tool_name = _make_llm_tool_name(tool_name, used_llm_names)
            llm_tool_name_map[llm_tool_name] = tool_name
            tool_to_llm_name[tool_name] = llm_tool_name

        return llm_tool_name_map, tool_to_llm_name

    def _refresh_llm_tool_name_map_locked(self) -> Dict[str, str]:
        """刷新 LLM 安全工具名映射；调用方需要已经持有 self._lock。"""
        self.llm_tool_name_map, tool_to_llm_name = self._build_llm_tool_name_maps(self.tools)
        return tool_to_llm_name

    def _has_loaded_tools_locked(self, name: str) -> bool:
        status = self.runtime_status.get(name) or {}
        return bool(status.get("loaded")) and any(
            tool_info.get("server") == name for tool_info in self.tools.values()
        )

    def add_server(self, name: str, config: Dict[str, Any]):
        """
        添加 MCP Server 配置

        Args:
            name: Server 名称
            config: Server 配置
        """
        self.servers[name] = config
        self.runtime_status[name] = {
            "server_key": name,
            "type": config.get("type"),
            "loaded": False,
            "connected": False,
            "tool_count": 0,
            "last_connected_at": None,
            "last_error": None,
            "last_error_at": None,
            "status": "configured",
            "message": "已配置，待连接",
        }
        logger.info(f"添加 MCP Server: {name}, type={config.get('type')}")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    def _ensure_runtime_status(self, name: str) -> dict[str, Any]:
        if name not in self.runtime_status:
            config = self.servers.get(name, {})
            self.runtime_status[name] = {
                "server_key": name,
                "type": config.get("type"),
                "loaded": False,
                "connected": False,
                "tool_count": 0,
                "last_connected_at": None,
                "last_error": None,
                "last_error_at": None,
                "status": "configured",
                "message": "已配置，待连接",
            }
        return self.runtime_status[name]

    def _update_runtime_status(self, name: str, **fields: Any) -> None:
        status = self._ensure_runtime_status(name)
        status.update(fields)

    def mark_connection_error(self, name: str, message: str) -> None:
        self._update_runtime_status(
            name,
            connected=False,
            loaded=False,
            status="error",
            message=f"连接失败: {message}",
            last_error=message,
            last_error_at=self._now_iso(),
        )

    def _connect_retry_cooldown_remaining(self, name: str) -> float:
        if self.connect_retry_cooldown_sec <= 0:
            return 0.0
        status = self.runtime_status.get(name) or {}
        if status.get("status") != "error":
            return 0.0
        raw_error_at = status.get("last_error_at")
        if not raw_error_at:
            return 0.0
        try:
            error_at = datetime.fromisoformat(raw_error_at)
        except ValueError:
            return 0.0
        elapsed = (datetime.now() - error_at).total_seconds()
        return max(0.0, self.connect_retry_cooldown_sec - elapsed)

    async def connect_server(self, name: str):
        """
        连接到 MCP Server（并发安全）

        Args:
            name: Server 名称
        """
        # 快速检查：工具已缓存则直接返回。MCP SDK 的底层 context
        # 必须在创建它的 task 中关闭，因此这里不再保留长期 session。
        if self._has_loaded_tools_locked(name):
            logger.debug("MCP Server %s 复用已缓存工具列表", name)
            return

        # 加锁检查状态并决定行为
        wait_event = None
        async with self._lock:
            # 双重检查：工具已缓存
            if self._has_loaded_tools_locked(name):
                logger.debug("MCP Server %s 复用已缓存工具列表（锁内确认）", name)
                return
            config = self.servers.get(name)
            if not config:
                raise ValueError(f"MCP Server {name} 未配置")
            cooldown_remaining = self._connect_retry_cooldown_remaining(name)
            if cooldown_remaining > 0:
                message = (
                    f"MCP Server {name} 上次连接失败，"
                    f"{cooldown_remaining:.1f}s 后再自动重试"
                )
                self._update_runtime_status(
                    name,
                    connected=False,
                    loaded=False,
                    status="error",
                    message=message,
                )
                raise RuntimeError(message)

            # 如果正在连接，获取 Event 用于等待
            if name in self._connecting:
                wait_event = self._connecting[name]
            else:
                # 标记正在连接
                self._connecting[name] = asyncio.Event()

        # 如果需要等待其他连接完成
        if wait_event:
            logger.info("MCP Server %s 已有连接任务在进行，等待该任务完成", name)
            await self._with_timeout(
                wait_event.wait(),
                self.connect_timeout_sec,
                f"MCP Server {name} 并发连接等待",
            )
            async with self._lock:
                if self._has_loaded_tools_locked(name):
                    logger.debug("MCP Server %s 复用并发连接任务加载的工具列表", name)
                    return
                status = self.runtime_status.get(name, {})
                error = status.get("last_error") or status.get("message") or "连接未完成"
            raise RuntimeError(f"MCP Server {name} 连接失败: {error}")

        server_type = config.get("type")

        try:
            self._update_runtime_status(
                name,
                connected=False,
                status="connecting",
                message="连接中",
            )
            if server_type == "sse":
                await self._with_timeout(
                    self._connect_sse(name, config),
                    self.connect_timeout_sec,
                    f"MCP Server {name} SSE 连接",
                )
            elif server_type == "streamable_http":
                await self._with_timeout(
                    self._connect_streamable_http(name, config),
                    self.connect_timeout_sec,
                    f"MCP Server {name} HTTP 连接",
                )
            else:
                raise ValueError(f"不支持的 MCP Server 类型: {server_type}")

            self._update_runtime_status(
                name,
                connected=False,
                loaded=True,
                last_connected_at=self._now_iso(),
                last_error=None,
                last_error_at=None,
                status="loaded",
                message="工具已缓存，调用时按次连接",
            )
            logger.info("MCP Server %s 工具缓存刷新成功", name)

        except asyncio.CancelledError:
            self.mark_connection_error(name, "连接取消或超时")
            raise
        except Exception as e:
            self._update_runtime_status(
                name,
                connected=False,
                loaded=False,
                status="error",
                message=f"连接失败: {e}",
                last_error=str(e),
                last_error_at=self._now_iso(),
            )
            logger.error(f"连接 MCP Server {name} 失败: {e!r}")
            raise
        finally:
            # 通知等待的协程，并清理标记
            async with self._lock:
                if name in self._connecting:
                    self._connecting[name].set()
                    del self._connecting[name]

    async def _connect_sse(self, name: str, config: Dict[str, Any]):
        """连接 SSE 类型的 MCP Server（远程）"""
        url = config["url"]
        headers = config.get("headers", {})

        async with sse_client(url, headers) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # 拉取工具列表
                await self._load_tools(name, session)

    async def _connect_streamable_http(self, name: str, config: Dict[str, Any]):
        """连接 streamable_http 类型的 MCP Server（远程）"""
        if streamable_http_client is None:
            raise RuntimeError(
                "当前环境未安装 streamable_http 支持，请升级 mcp 依赖后再启用该类型的 MCP Server"
            )

        async with self._open_streamable_http_session(config) as session:
            # 拉取工具列表
            await self._load_tools(name, session)

    @asynccontextmanager
    async def _open_server_session(self, name: str, config: Dict[str, Any]):
        server_type = config.get("type")
        if server_type == "sse":
            async with sse_client(config["url"], config.get("headers", {})) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return

        if server_type == "streamable_http":
            if streamable_http_client is None:
                raise RuntimeError(
                    "当前环境未安装 streamable_http 支持，请升级 mcp 依赖后再启用该类型的 MCP Server"
                )
            async with self._open_streamable_http_session(config) as session:
                yield session
            return

        raise ValueError(f"不支持的 MCP Server 类型: {server_type}")

    @asynccontextmanager
    async def _open_streamable_http_session(self, config: Dict[str, Any]):
        http_timeout = (
            httpx.Timeout(self.http_timeout_sec, connect=min(self.http_timeout_sec, 10.0))
            if self.http_timeout_sec > 0
            else None
        )
        http_client = httpx.AsyncClient(
            headers=config.get("headers", {}),
            timeout=http_timeout,
        )
        try:
            async with streamable_http_client(config["url"], http_client=http_client) as (
                read,
                write,
                _get_session_id,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        finally:
            await self._safe_async_close("http_client", http_client.aclose)

    async def _load_tools(self, server_name: str, session: ClientSession):
        """从 MCP Server 拉取工具列表"""
        try:
            result = await session.list_tools()
            tools = result.tools
            server_tools = {}
            for tool in tools:
                tool_name = f"{server_name}.{tool.name}"
                server_tools[tool_name] = {
                    "server": server_name,
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.inputSchema
                }

            # 加锁保护写入 tools 字典
            async with self._lock:
                self.tools = {
                    tool_name: tool_info
                    for tool_name, tool_info in self.tools.items()
                    if tool_info.get("server") != server_name
                }
                self.tools.update(server_tools)
                self._refresh_llm_tool_name_map_locked()

            self._update_runtime_status(
                server_name,
                loaded=True,
                tool_count=len(tools),
                status="loaded",
                message=f"已加载 {len(tools)} 个工具",
            )
            logger.info(f"从 {server_name} 加载了 {len(tools)} 个工具")

        except Exception as e:
            self._update_runtime_status(
                server_name,
                loaded=False,
                status="error",
                message=f"加载工具失败: {e}",
                last_error=str(e),
                last_error_at=self._now_iso(),
            )
            logger.error(f"加载工具失败: {e!r}")
            raise

    async def _pop_server_runtime(self, name: str) -> Dict[str, Any]:
        """
        从运行时缓存中移除一个 MCP Server 的连接状态。

        先移除，再关闭底层上下文，避免关闭阶段异常导致 stale session 残留。
        """
        async with self._lock:
            self.sessions.pop(name, None)
            return self.contexts.pop(name, {})

    @staticmethod
    async def _safe_async_close(label: str, closer) -> None:
        try:
            await closer()
        except Exception as exc:
            logger.warning("关闭 MCP %s 失败: %r", label, exc)

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any], max_retries: int = 2) -> Any:
        """
        调用 MCP 工具（带自动重连）

        Args:
            tool_name: 工具名称（格式：server_name.tool_name）
            arguments: 工具参数
            max_retries: 最大重试次数（每次重试会重新连接）

        Returns:
            工具执行结果
        """
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                # 尝试调用工具
                result = await self._with_timeout(
                    self._do_call_tool(tool_name, arguments),
                    self.tool_timeout_sec,
                    f"MCP 工具 {tool_name} 调用",
                )
                return result
            except Exception as e:
                last_error = e
                logger.warning(f"调用工具 {tool_name} 失败 (尝试 {attempt + 1}/{max_retries + 1}): {e!r}")

                # 获取 server_name 以便记录状态。工具调用本身按次建立 MCP session，
                # 不再断开/复用跨 task 的长期 session。
                resolved_tool_name = self.resolve_tool_name(tool_name)
                tool_info = self.tools.get(resolved_tool_name)
                if not tool_info:
                    # 工具名不存在通常是模型生成了未注册的 tool_call，重连 MCP 无法修复。
                    break

                server_name = tool_info["server"]
                self._update_runtime_status(
                    server_name,
                    connected=False,
                    status="loaded",
                    message=f"上次工具调用失败: {e}",
                    last_error=str(e),
                    last_error_at=self._now_iso(),
                )

                if attempt < max_retries:
                    logger.info("MCP Server %s 工具调用失败后准备重新按次连接", server_name)
                    await asyncio.sleep(0)

        # 所有尝试都失败
        raise last_error

    async def _do_call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        执行实际的工具调用

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果
        """
        # 加锁读取工具和 Server 配置快照。MCP SDK 底层 context 使用 AnyIO
        # cancel scope，必须在同一个 task 中 enter/exit，所以这里按次打开。
        async with self._lock:
            resolved_tool_name = self.resolve_tool_name(tool_name)
            tool_info = self.tools.get(resolved_tool_name)
            if not tool_info:
                available_tools = self._format_available_tool_names(tool_name)
                raise ValueError(f"工具 {tool_name} 不存在。当前可用工具: {available_tools}")

            server_name = tool_info["server"]
            config = dict(self.servers.get(server_name) or {})
            if not config:
                raise ValueError(f"MCP Server {server_name} 未配置")

        # 工具调用在锁外执行，避免阻塞其他操作。session 生命周期被限制在
        # 当前 coroutine 内，避免 reload/interrupt 时跨 task 关闭 MCP context。
        async with self._open_server_session(server_name, config) as session:
            result = await session.call_tool(tool_info["name"], arguments)
        self._update_runtime_status(
            server_name,
            connected=False,
            loaded=True,
            status="loaded",
            message="工具调用成功，session 已关闭",
            last_error=None,
            last_error_at=None,
        )
        logger.info(f"调用工具 {resolved_tool_name} 成功")
        return result

    def _format_available_tool_names(self, requested_tool_name: str) -> str:
        """格式化可用工具名，优先展示同 server 的工具，便于定位模型幻造名称。"""
        requested = requested_tool_name or ""
        server_prefix = requested.split("__", 1)[0].split(".", 1)[0] if requested else ""
        candidates = []
        for real_name in sorted(self.tools):
            safe_name = next(
                (safe for safe, mapped in self.llm_tool_name_map.items() if mapped == real_name),
                real_name,
            )
            if server_prefix and not (
                real_name.startswith(f"{server_prefix}.")
                or safe_name.startswith(f"{server_prefix}__")
            ):
                continue
            candidates.append(f"{safe_name}->{real_name}" if safe_name != real_name else real_name)

        if not candidates:
            candidates = sorted(self.tools)
        return ", ".join(candidates[:30]) if candidates else "<none>"

    async def _disconnect_server(self, name: str):
        """标记单个 MCP Server 当前没有活跃按次连接。"""
        await self._pop_server_runtime(name)
        self._update_runtime_status(
            name,
            connected=False,
            status="loaded" if self._has_loaded_tools_locked(name) else "idle",
            message="没有活跃按次连接",
        )
        logger.info(f"已断开 MCP Server {name}")

    async def disconnect_server(self, name: str) -> None:
        """断开单个 MCP Server，并清理它暴露的工具，便于手动重连。"""
        if name not in self.servers:
            raise ValueError(f"MCP Server {name} 未配置")

        await self._pop_server_runtime(name)
        async with self._lock:
            self.tools = {
                tool_name: tool_info
                for tool_name, tool_info in self.tools.items()
                if tool_info.get("server") != name
            }
            self._refresh_llm_tool_name_map_locked()
        self._update_runtime_status(
            name,
            connected=False,
            loaded=False,
            tool_count=0,
            status="configured",
            message="已断开，等待重连",
        )

    def get_tools_for_llm(self, server_filter: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        获取工具的 OpenAI function calling 格式

        Args:
            server_filter: 可选，只返回指定 Server 的工具。为空或 None 时返回所有工具。

        Returns:
            工具列表（OpenAI 格式）
        """
        # 复制字典避免遍历时被修改。映射表始终按“全部工具”重建，
        # 避免并发请求使用不同 server_filter 时互相覆盖别名。
        tools_snapshot = dict(self.tools)
        llm_tool_name_map, tool_to_llm_name = self._build_llm_tool_name_maps(tools_snapshot)
        self.llm_tool_name_map = llm_tool_name_map
        server_filter_set = set(server_filter) if server_filter is not None else None

        tools_list = []
        for tool_name, tool_info in tools_snapshot.items():
            # 如果指定了过滤器，只返回匹配的 Server 的工具
            if server_filter_set is not None and tool_info["server"] not in server_filter_set:
                continue

            tools_list.append({
                "type": "function",
                "function": {
                    "name": tool_to_llm_name[tool_name],
                    "description": tool_info["description"],
                    "parameters": tool_info["input_schema"]
                }
            })
        return tools_list

    def resolve_tool_name(self, tool_name: str) -> str:
        """把 LLM 安全工具名还原为 MCP 内部真实工具名。"""
        return self.llm_tool_name_map.get(tool_name, tool_name)

    async def disconnect_all(self):
        """清理所有 MCP 工具缓存和兼容状态（并发安全）。"""
        async with self._lock:
            names_to_disconnect = list(self.servers.keys())
            self.sessions.clear()
            self.tools.clear()
            self.llm_tool_name_map.clear()
            self.contexts.clear()

        for name in names_to_disconnect:
            self._update_runtime_status(
                name,
                connected=False,
                loaded=False,
                tool_count=0,
                status="idle",
                message="运行时已重置，等待重新连接",
            )
            logger.info(f"断开 MCP Server {name}")

    def get_runtime_status(self) -> list[dict[str, Any]]:
        """返回当前 MCP 运行态摘要。"""
        tools_count_by_server: dict[str, int] = {}
        for tool_info in self.tools.values():
            server_name = tool_info["server"]
            tools_count_by_server[server_name] = tools_count_by_server.get(server_name, 0) + 1

        items: list[dict[str, Any]] = []
        for server_name, config in self.servers.items():
            status = dict(self._ensure_runtime_status(server_name))
            status["type"] = config.get("type")
            status["connected"] = bool(status.get("connected"))
            status["tool_count"] = tools_count_by_server.get(server_name, status.get("tool_count", 0))
            items.append(status)
        return items
