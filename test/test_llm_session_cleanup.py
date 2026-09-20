import asyncio
import os
import unittest

import grpc

os.environ["CONFIG_DATABASE_URL"] = ""

from llm import llm_service_pb2
from llm.llm_grpc_server import ImprovedLLMServiceServicer
from llm.session_manager import SessionManager


class LLMSessionCleanupTest(unittest.TestCase):
    def test_clear_session_removes_history_and_agent_state(self):
        manager = SessionManager()
        manager.add_message("session-1", "user", "你好")
        manager.set_agent_state("session-1", {"agent_id": "test-agent", "step": 1})
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.session_manager = manager

        response = asyncio.run(
            servicer.ClearSession(
                llm_service_pb2.ClearSessionRequest(session_id="session-1"),
                _FakeContext(),
            )
        )

        self.assertTrue(response.success)
        self.assertTrue(response.cleared)
        self.assertEqual("cleared", response.message)
        self.assertEqual([], manager.get_history("session-1"))
        self.assertIsNone(manager.get_agent_state("session-1"))
        self.assertEqual(0, manager.get_session_count())

    def test_clear_session_reports_not_found_for_unknown_session(self):
        manager = SessionManager()
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.session_manager = manager

        response = asyncio.run(
            servicer.ClearSession(
                llm_service_pb2.ClearSessionRequest(session_id="missing-session"),
                _FakeContext(),
            )
        )

        self.assertTrue(response.success)
        self.assertFalse(response.cleared)
        self.assertEqual("not_found", response.message)

    def test_clear_session_rejects_empty_session_id(self):
        manager = SessionManager()
        servicer = object.__new__(ImprovedLLMServiceServicer)
        servicer.session_manager = manager
        context = _FakeContext()

        response = asyncio.run(
            servicer.ClearSession(
                llm_service_pb2.ClearSessionRequest(session_id=""),
                context,
            )
        )

        self.assertFalse(response.success)
        self.assertFalse(response.cleared)
        self.assertEqual(grpc.StatusCode.INVALID_ARGUMENT, context.code)
        self.assertEqual("session_id 不能为空", context.details)


class _FakeContext:
    def __init__(self):
        self.code = None
        self.details = None

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


if __name__ == "__main__":
    unittest.main()
