import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from llm.mcp_manager import MCPManager


class MCPManagerTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_connect_server_times_out_and_marks_error(self):
        manager = MCPManager(connect_timeout_sec=0.05)
        manager.add_server("slow", {"type": "sse", "url": "http://127.0.0.1:9/mcp"})

        async def slow_connect(name, config):
            await asyncio.sleep(1.0)

        manager._connect_sse = slow_connect

        with self.assertRaises(TimeoutError):
            await manager.connect_server("slow")

        status = manager.runtime_status["slow"]
        self.assertFalse(status["connected"])
        self.assertEqual("error", status["status"])
        self.assertIn("超时", status["last_error"])

    async def test_tool_call_timeout_closes_ephemeral_session(self):
        manager = MCPManager(tool_timeout_sec=0.05)
        manager.add_server("weather", {"type": "sse", "url": "http://127.0.0.1:9/mcp"})
        manager.tools["weather.get_weather"] = {
            "server": "weather",
            "name": "get_weather",
            "description": "weather",
            "input_schema": {"type": "object"},
        }
        manager._refresh_llm_tool_name_map_locked()
        tracker = {}

        @asynccontextmanager
        async def slow_session(_server_name, _config):
            tracker["entered"] = True
            try:
                yield _SlowToolSession()
            finally:
                tracker["exited"] = True

        manager._open_server_session = slow_session

        with self.assertRaises(TimeoutError):
            await manager.call_tool("weather.get_weather", {}, max_retries=0)

        self.assertTrue(tracker.get("entered"))
        self.assertTrue(tracker.get("exited"))
        self.assertNotIn("weather", manager.sessions)
        self.assertNotIn("weather", manager.contexts)
        self.assertEqual("loaded", manager.runtime_status["weather"]["status"])

    async def test_connect_success_caches_tools_without_retaining_contexts(self):
        manager = MCPManager(connect_timeout_sec=1.0)
        manager.add_server("weather", {"type": "sse", "url": "http://127.0.0.1:9/mcp"})
        tracker = {}

        with (
            patch("llm.mcp_manager.sse_client", return_value=_FakeSSEContext(tracker)),
            patch("llm.mcp_manager.ClientSession", lambda read, write: _FakeSessionContext(tracker, _ToolListSession())),
        ):
            await manager.connect_server("weather")

        self.assertTrue(tracker.get("sse_entered"))
        self.assertTrue(tracker.get("session_entered"))
        self.assertTrue(tracker.get("session_exited"))
        self.assertTrue(tracker.get("sse_exited"))
        self.assertIn("weather.get_weather", manager.tools)
        self.assertNotIn("weather", manager.sessions)
        self.assertNotIn("weather", manager.contexts)
        status = manager.get_runtime_status()[0]
        self.assertFalse(status["connected"])
        self.assertTrue(status["loaded"])
        self.assertEqual("loaded", status["status"])

    async def test_successful_tool_call_clears_previous_error(self):
        manager = MCPManager(tool_timeout_sec=1.0)
        manager.add_server("weather", {"type": "sse", "url": "http://127.0.0.1:9/mcp"})
        manager.tools["weather.get_weather"] = {
            "server": "weather",
            "name": "get_weather",
            "description": "weather",
            "input_schema": {"type": "object"},
        }
        manager.runtime_status["weather"].update({
            "loaded": True,
            "status": "loaded",
            "last_error": "old failure",
            "last_error_at": "2026-07-01T00:00:00",
        })
        manager._refresh_llm_tool_name_map_locked()

        @asynccontextmanager
        async def ok_session(_server_name, _config):
            yield _FastToolSession()

        manager._open_server_session = ok_session

        result = await manager.call_tool("weather.get_weather", {}, max_retries=0)

        self.assertEqual("ok", result)
        status = manager.runtime_status["weather"]
        self.assertIsNone(status["last_error"])
        self.assertIsNone(status["last_error_at"])
        self.assertFalse(status["connected"])
        self.assertTrue(status["loaded"])

    async def test_connect_timeout_closes_partially_open_sse_contexts(self):
        manager = MCPManager(connect_timeout_sec=0.05)
        manager.add_server("slow", {"type": "sse", "url": "http://127.0.0.1:9/mcp"})
        tracker = {}

        with (
            patch("llm.mcp_manager.sse_client", return_value=_FakeSSEContext(tracker)),
            patch("llm.mcp_manager.ClientSession", lambda read, write: _FakeSessionContext(tracker)),
        ):
            with self.assertRaises(TimeoutError):
                await manager.connect_server("slow")

        self.assertTrue(tracker.get("sse_entered"))
        self.assertTrue(tracker.get("session_entered"))
        self.assertTrue(tracker.get("session_exited"))
        self.assertTrue(tracker.get("sse_exited"))
        self.assertNotIn("slow", manager.sessions)
        self.assertNotIn("slow", manager.contexts)

    async def test_failed_connect_uses_retry_cooldown(self):
        manager = MCPManager(connect_timeout_sec=0.05, connect_retry_cooldown_sec=30.0)
        manager.add_server("slow", {"type": "sse", "url": "http://127.0.0.1:9/mcp"})
        attempts = 0

        async def failing_connect(name, config):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("boom")

        manager._connect_sse = failing_connect

        with self.assertRaises(RuntimeError):
            await manager.connect_server("slow")
        with self.assertRaisesRegex(RuntimeError, "后再自动重试"):
            await manager.connect_server("slow")

        self.assertEqual(1, attempts)


class _SlowToolSession:
    async def call_tool(self, name, arguments):
        await asyncio.sleep(1.0)
        return "late"


class _FastToolSession:
    async def call_tool(self, name, arguments):
        return "ok"


class _FakeSSEContext:
    def __init__(self, tracker):
        self.tracker = tracker

    async def __aenter__(self):
        self.tracker["sse_entered"] = True
        return object(), object()

    async def __aexit__(self, exc_type, exc, tb):
        self.tracker["sse_exited"] = True


class _FakeSessionContext:
    def __init__(self, tracker, session=None):
        self.tracker = tracker
        self.session = session or _HangingInitializeSession()

    async def __aenter__(self):
        self.tracker["session_entered"] = True
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        self.tracker["session_exited"] = True


class _HangingInitializeSession:
    async def initialize(self):
        await asyncio.sleep(1.0)


class _Tool:
    name = "get_weather"
    description = "weather"
    inputSchema = {"type": "object"}


class _ToolListResult:
    tools = [_Tool()]


class _ToolListSession:
    async def initialize(self):
        return None

    async def list_tools(self):
        return _ToolListResult()
