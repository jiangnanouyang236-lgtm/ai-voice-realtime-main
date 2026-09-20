import asyncio
import threading
import unittest

from llm.runtime_state import LLMRuntimeState


class LLMRuntimeStateShutdownTest(unittest.TestCase):
    def test_shutdown_cancels_warmup_and_disconnects_mcp_manager(self):
        async def run_test():
            state = LLMRuntimeState.__new__(LLMRuntimeState)
            state._lock = threading.RLock()
            warmup_task = asyncio.create_task(asyncio.sleep(60))
            state._warmup_task = warmup_task
            state._mcp_manager = _FakeMCPManager()

            await state.shutdown()

            self.assertIsNone(state._warmup_task)
            self.assertTrue(warmup_task.cancelled())
            self.assertTrue(state._mcp_manager.disconnected)

        asyncio.run(run_test())

    def test_wait_for_mcp_warmup_waits_for_scheduled_task(self):
        async def run_test():
            state = LLMRuntimeState.__new__(LLMRuntimeState)
            state._lock = threading.RLock()

            async def finish_warmup():
                await asyncio.sleep(0)
                return {
                    "success": True,
                    "total": 2,
                    "connected": 2,
                    "failed": 0,
                    "errors": {},
                }

            state._warmup_task = asyncio.create_task(finish_warmup())
            result = await state.wait_for_mcp_warmup()

            self.assertTrue(result["success"])
            self.assertEqual(2, result["connected"])
            self.assertTrue(state._warmup_task.done())

        asyncio.run(run_test())


class _FakeMCPManager:
    def __init__(self):
        self.disconnected = False

    async def disconnect_all(self):
        self.disconnected = True


if __name__ == "__main__":
    unittest.main()
