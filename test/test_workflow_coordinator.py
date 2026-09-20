import asyncio
import json
from types import SimpleNamespace
import unittest

from gateway.workflow_coordinator import (
    WorkflowCoordinator,
    WorkflowRunRequest,
)
from gateway.workflow_playback_barrier import (
    PLAYBACK_COMPLETE,
    PLAYBACK_INTERRUPTED,
    PlaybackBarrierRegistry,
)


def _request():
    return WorkflowRunRequest(
        text="查天气然后拨打视频",
        session_id="session-1",
        bot_id="xiaowen",
        robot_id="companion_01",
        trace_id="trace-1",
        round_id="round-1",
        playback_id="round-1:playback",
    )


def _plan_json(*, terminal=True):
    steps = [
        {"id": "step_1", "type": "tool", "terminal": False},
        {"id": "step_2", "type": "respond", "terminal": False},
    ]
    if terminal:
        steps.append({"id": "step_3", "type": "tool", "terminal": True})
    return json.dumps({"version": 1, "goal": "test", "steps": steps})


class _Client:
    def __init__(self, *, terminal=True):
        self.plan_response = SimpleNamespace(
            success=True,
            plan_id="wf-1",
            plan_json=_plan_json(terminal=terminal),
            error_code="",
            message="",
        )
        self.executed = []
        self.cancelled = []
        self.stream_gate = None

    async def classify(self, request):
        return SimpleNamespace(success=True, complex=True)

    async def plan(self, request):
        return self.plan_response

    async def execute_step(self, request, *, plan_id, step_id, playback_completed):
        self.executed.append((step_id, playback_completed, request.playback_id))
        return SimpleNamespace(success=True, exit=step_id == "step_3")

    async def stream_synthesis(self, request, *, plan_id):
        yield SimpleNamespace(text="青岛有雨，", error_code="", is_final=False)
        if self.stream_gate is not None:
            await self.stream_gate.wait()
        yield SimpleNamespace(text="建议带伞。", error_code="", is_final=False)
        yield SimpleNamespace(text="", error_code="", is_final=True)

    async def cancel(self, *, plan_id, session_id, reason):
        self.cancelled.append((plan_id, session_id, reason))
        return SimpleNamespace(success=True, cancelled=True)


class WorkflowCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_classify_complex_delegates_to_workflow_client(self):
        coordinator = WorkflowCoordinator(
            client=_Client(),
            playback_barriers=PlaybackBarrierRegistry(),
        )

        self.assertTrue(await coordinator.classify_complex(_request()))

    async def test_terminal_waits_for_matching_playback_complete(self):
        client = _Client()
        barriers = PlaybackBarrierRegistry()
        coordinator = WorkflowCoordinator(
            client=client,
            playback_barriers=barriers,
            playback_timeout_sec=1.0,
        )
        emitted = []
        synthesis_done = asyncio.Event()

        async def emit(text):
            emitted.append(text)
            if text == "建议带伞。":
                synthesis_done.set()

        task = asyncio.create_task(coordinator.run(_request(), emit_text=emit))
        await synthesis_done.wait()
        await asyncio.sleep(0)
        self.assertEqual([("step_1", False, "round-1:playback")], client.executed)

        matched = barriers.record_report(
            session_id="session-1",
            round_id="round-1",
            playback_id="round-1:playback",
            report_type=PLAYBACK_COMPLETE,
        )
        result = await task

        self.assertTrue(matched)
        self.assertTrue(result.success)
        self.assertTrue(result.exit)
        self.assertEqual(["青岛有雨，", "建议带伞。"], emitted)
        self.assertEqual(
            [
                ("step_1", False, "round-1:playback"),
                ("step_3", True, "round-1:playback"),
            ],
            client.executed,
        )

    async def test_interrupted_playback_cancels_without_terminal_action(self):
        client = _Client()
        barriers = PlaybackBarrierRegistry()
        coordinator = WorkflowCoordinator(
            client=client,
            playback_barriers=barriers,
            playback_timeout_sec=1.0,
        )
        emitted = asyncio.Event()

        async def emit(text):
            if text == "建议带伞。":
                emitted.set()

        task = asyncio.create_task(coordinator.run(_request(), emit_text=emit))
        await emitted.wait()
        barriers.record_report(
            session_id="session-1",
            round_id="round-1",
            playback_id="round-1:playback",
            report_type=PLAYBACK_INTERRUPTED,
            reason="wake_interrupt",
        )
        result = await task

        self.assertFalse(result.success)
        self.assertEqual("playback_interrupted", result.error_code)
        self.assertEqual([("step_1", False, "round-1:playback")], client.executed)
        self.assertEqual(1, len(client.cancelled))

    async def test_cancel_session_stops_blocked_stream_and_cancels_remote_once(self):
        client = _Client()
        client.stream_gate = asyncio.Event()
        coordinator = WorkflowCoordinator(
            client=client,
            playback_barriers=PlaybackBarrierRegistry(),
            playback_timeout_sec=1.0,
        )
        first_chunk = asyncio.Event()

        async def emit(text):
            first_chunk.set()

        task = asyncio.create_task(coordinator.run(_request(), emit_text=emit))
        await first_chunk.wait()
        self.assertTrue(await coordinator.cancel_session("session-1", reason="client_interrupt"))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([("wf-1", "session-1", "client_interrupt")], client.cancelled)
        self.assertEqual({"active_workflows": 0}, await coordinator.snapshot())

    async def test_invalid_plan_is_rejected_before_any_step(self):
        client = _Client()
        client.plan_response.plan_json = json.dumps(
            {
                "steps": [
                    {"id": "step_1", "type": "tool", "terminal": True},
                    {"id": "step_2", "type": "respond", "terminal": False},
                ]
            }
        )
        coordinator = WorkflowCoordinator(
            client=client,
            playback_barriers=PlaybackBarrierRegistry(),
        )

        emitted = []
        result = await coordinator.run(
            _request(), emit_text=lambda text: _append(emitted, text)
        )

        self.assertFalse(result.success)
        self.assertEqual("invalid_plan_response", result.error_code)
        self.assertEqual([], client.executed)
        self.assertEqual(1, len(client.cancelled))
        self.assertEqual(1, len(emitted))
        self.assertIn("分开告诉我", emitted[0])

    async def test_non_terminal_workflow_does_not_wait_for_playback_report(self):
        client = _Client(terminal=False)
        coordinator = WorkflowCoordinator(
            client=client,
            playback_barriers=PlaybackBarrierRegistry(),
        )
        emitted = []

        result = await coordinator.run(
            _request(),
            emit_text=lambda text: _append(emitted, text),
        )

        self.assertTrue(result.success)
        self.assertFalse(result.exit)
        self.assertEqual([("step_1", False, "round-1:playback")], client.executed)


async def _append(items, value):
    items.append(value)


if __name__ == "__main__":
    unittest.main()
