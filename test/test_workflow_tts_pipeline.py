import asyncio
import json
from types import SimpleNamespace
import unittest

from gateway.workflow_coordinator import (
    WorkflowCoordinator,
    WorkflowRunRequest,
    WorkflowRunResult,
)
from gateway.workflow_playback_barrier import PLAYBACK_COMPLETE, PlaybackBarrierRegistry
from gateway.workflow_tts_pipeline import WorkflowTTSPipeline


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


class _Coordinator:
    def __init__(self, *, terminal=True, fail=False):
        self.terminal = terminal
        self.fail = fail
        self.done_seen = asyncio.Event()
        self.cancelled = []

    async def run(self, request, *, emit_text, observe_plan, synthesis_finished, **kwargs):
        if self.fail:
            return WorkflowRunResult(success=False, error_code="plan_failed")
        await observe_plan("wf-1", self.terminal)
        await emit_text("青岛有雨，")
        await emit_text("建议带伞。")
        await synthesis_finished()
        if self.terminal:
            await self.done_seen.wait()
        return WorkflowRunResult(success=True, plan_id="wf-1", exit=self.terminal)

    async def cancel_session(self, session_id, *, reason):
        self.cancelled.append((session_id, reason))
        return True


class _TTSCall:
    def __init__(self, chunks, captured):
        self._chunks = chunks
        self._captured = captured
        self.cancelled = False

    def __iter__(self):
        for chunk in self._chunks:
            self._captured.append(chunk)
            if chunk.is_final:
                break
            yield SimpleNamespace(audio_data=chunk.text.encode("utf-8"), sample_rate=16000)

    def cancel(self):
        self.cancelled = True


class _TTSStub:
    def __init__(self):
        self.captured = []
        self.call = None

    def StreamTextToSpeech(self, chunks, timeout):
        self.call = _TTSCall(chunks, self.captured)
        return self.call


class _WorkflowClient:
    def __init__(self, events):
        self.events = events

    async def plan(self, request):
        self.events.append("plan")
        return SimpleNamespace(
            success=True,
            plan_id="wf-1",
            plan_json=json.dumps(
                {
                    "steps": [
                        {"id": "step_1", "type": "tool", "terminal": False},
                        {"id": "step_2", "type": "respond", "terminal": False},
                        {"id": "step_3", "type": "tool", "terminal": True},
                    ]
                }
            ),
        )

    async def execute_step(self, request, *, plan_id, step_id, playback_completed):
        self.events.append(f"execute:{step_id}:{playback_completed}")
        return SimpleNamespace(success=True, exit=step_id == "step_3")

    async def stream_synthesis(self, request, *, plan_id):
        self.events.append("synthesis")
        yield SimpleNamespace(text="播报内容", error_code="")

    async def cancel(self, **kwargs):
        self.events.append("cancel")

    async def classify(self, request):
        return SimpleNamespace(success=True, complex=True)


class WorkflowTTSPipelineTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_coordinator_executes_terminal_only_after_done_and_playback_report(self):
        events = []
        barriers = PlaybackBarrierRegistry()
        coordinator = WorkflowCoordinator(
            client=_WorkflowClient(events),
            playback_barriers=barriers,
            playback_timeout_sec=1.0,
        )
        stub = _TTSStub()
        pipeline = WorkflowTTSPipeline(
            coordinator=coordinator,
            tts_stub_provider=lambda: stub,
            tts_timeout_sec=10.0,
        )

        async def send_control(kind, payload):
            events.append(kind)
            if kind == "done":
                barriers.record_report(
                    session_id="session-1",
                    round_id="round-1",
                    playback_id="round-1:playback",
                    report_type=PLAYBACK_COMPLETE,
                )

        async def send_audio(chunk, seq):
            events.append("audio")

        result = await pipeline.run(
            _request(),
            bot_tts_settings=None,
            send_control=send_control,
            send_audio=send_audio,
            is_cancelled=lambda: False,
        )

        self.assertTrue(result.workflow.success)
        first_done = events.index("done")
        terminal = events.index("execute:step_3:True")
        second_done = events.index("done", first_done + 1)
        self.assertLess(events.index("audio"), first_done)
        self.assertLess(first_done, terminal)
        self.assertLess(terminal, second_done)

    async def test_terminal_done_is_sent_after_audio_and_before_terminal_completion(self):
        coordinator = _Coordinator(terminal=True)
        stub = _TTSStub()
        pipeline = WorkflowTTSPipeline(
            coordinator=coordinator,
            tts_stub_provider=lambda: stub,
            tts_timeout_sec=10.0,
        )
        controls = []
        audio = []

        async def send_control(kind, payload):
            controls.append((kind, payload))
            if kind == "done":
                coordinator.done_seen.set()

        async def send_audio(chunk, seq):
            audio.append((seq, chunk.audio_data))

        result = await pipeline.run(
            _request(),
            bot_tts_settings={"voice": "test"},
            send_control=send_control,
            send_audio=send_audio,
            is_cancelled=lambda: False,
        )

        self.assertTrue(result.workflow.success)
        self.assertTrue(result.workflow.exit)
        self.assertTrue(result.done_sent)
        self.assertTrue(result.exit_sent)
        self.assertEqual(
            ["playback_start", "done", "done"],
            [kind for kind, _ in controls],
        )
        self.assertFalse(controls[-2][1]["exit"])
        self.assertTrue(controls[-1][1]["exit"])
        self.assertEqual(
            ["青岛有雨，".encode(), "建议带伞。".encode()],
            [item[1] for item in audio],
        )
        self.assertEqual(["青岛有雨，", "建议带伞。", ""], [chunk.text for chunk in stub.captured])
        self.assertTrue(stub.captured[-1].is_final)
        self.assertEqual("trace-1", stub.captured[0].trace_id)
        self.assertEqual("round-1:playback", stub.captured[0].playback_id)

    async def test_non_terminal_sends_done_exit_false(self):
        coordinator = _Coordinator(terminal=False)
        stub = _TTSStub()
        pipeline = WorkflowTTSPipeline(
            coordinator=coordinator,
            tts_stub_provider=lambda: stub,
            tts_timeout_sec=10.0,
        )
        controls = []

        result = await pipeline.run(
            _request(),
            bot_tts_settings=None,
            send_control=lambda kind, payload: _append(controls, (kind, payload)),
            send_audio=lambda chunk, seq: asyncio.sleep(0),
            is_cancelled=lambda: False,
            initial_text="复杂任务，稍等哦。",
        )

        self.assertTrue(result.workflow.success)
        self.assertFalse(controls[-1][1]["exit"])
        self.assertFalse(result.exit_sent)
        self.assertEqual("复杂任务，稍等哦。", stub.captured[0].text)

    async def test_plan_failure_does_not_send_done(self):
        coordinator = _Coordinator(fail=True)
        stub = _TTSStub()
        pipeline = WorkflowTTSPipeline(
            coordinator=coordinator,
            tts_stub_provider=lambda: stub,
            tts_timeout_sec=10.0,
        )
        controls = []

        result = await pipeline.run(
            _request(),
            bot_tts_settings=None,
            send_control=lambda kind, payload: _append(controls, (kind, payload)),
            send_audio=lambda chunk, seq: asyncio.sleep(0),
            is_cancelled=lambda: False,
        )

        self.assertFalse(result.workflow.success)
        self.assertFalse(result.done_sent)
        self.assertFalse(result.exit_sent)
        self.assertEqual(["playback_start"], [kind for kind, _ in controls])

    async def test_cancelled_round_cancels_workflow_and_does_not_send_done(self):
        coordinator = _Coordinator(terminal=True)
        stub = _TTSStub()
        pipeline = WorkflowTTSPipeline(
            coordinator=coordinator,
            tts_stub_provider=lambda: stub,
            tts_timeout_sec=10.0,
        )
        controls = []

        with self.assertRaises(asyncio.CancelledError):
            await pipeline.run(
                _request(),
                bot_tts_settings=None,
                send_control=lambda kind, payload: _append(controls, (kind, payload)),
                send_audio=lambda chunk, seq: asyncio.sleep(0),
                is_cancelled=lambda: True,
            )

        self.assertEqual([("session-1", "workflow_tts_cancelled")], coordinator.cancelled)
        self.assertEqual(["playback_start"], [kind for kind, _ in controls])


async def _append(items, item):
    items.append(item)


if __name__ == "__main__":
    unittest.main()
