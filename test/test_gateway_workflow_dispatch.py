import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ["CONFIG_DATABASE_URL"] = ""

from gateway import gateway_server as gateway
from gateway.workflow_coordinator import WorkflowRunResult
from gateway.workflow_tts_pipeline import WorkflowTTSPipelineResult


def _trace():
    return {
        "trace_id": "trace-1",
        "round_id": "round-1",
        "playback_id": "round-1:playback",
        "robot_id": "companion_01",
    }


class GatewayWorkflowDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_non_complex_falls_back_without_starting_pipeline(self):
        coordinator = SimpleNamespace(classify_complex=AsyncMock(return_value=False))
        with (
            patch.object(gateway, "get_workflow_coordinator", return_value=coordinator),
            patch.object(gateway, "WorkflowTTSPipeline") as pipeline,
        ):
            handled = await gateway._try_process_complex_workflow(
                text="查天气",
                session_id="session-1",
                websocket=object(),
                bot_id="xiaowen",
                bot_tts_settings={},
                trace=_trace(),
            )

        self.assertFalse(handled)
        pipeline.assert_not_called()

    async def test_classifier_failure_falls_back_to_original_stream_chat(self):
        coordinator = SimpleNamespace(
            classify_complex=AsyncMock(side_effect=RuntimeError("unimplemented"))
        )
        with patch.object(gateway, "get_workflow_coordinator", return_value=coordinator):
            handled = await gateway._try_process_complex_workflow(
                text="复杂请求",
                session_id="session-1",
                websocket=object(),
                bot_id="xiaowen",
                bot_tts_settings={},
                trace=_trace(),
            )

        self.assertFalse(handled)

    async def test_complex_request_uses_separate_pipeline(self):
        coordinator = SimpleNamespace(classify_complex=AsyncMock(return_value=True))
        pipeline_instance = SimpleNamespace(
            run=AsyncMock(
                return_value=WorkflowTTSPipelineResult(
                    workflow=WorkflowRunResult(
                        success=True,
                        plan_id="wf-1",
                        exit=True,
                        completed_steps=("step_1", "step_2", "step_3"),
                    ),
                    audio_chunks=2,
                    audio_bytes=100,
                    done_sent=True,
                    exit_sent=True,
                    terminal_expected=True,
                )
            )
        )
        manager = SimpleNamespace(
            is_interrupted=lambda session_id: False,
            is_current_round=lambda session_id, round_id: True,
            set_interrupted=Mock(),
        )
        with (
            patch.object(gateway, "get_workflow_coordinator", return_value=coordinator),
            patch.object(gateway, "WorkflowTTSPipeline", return_value=pipeline_instance),
            patch.object(gateway, "session_manager", manager),
        ):
            handled = await gateway._try_process_complex_workflow(
                text="查天气然后拨打视频",
                session_id="session-1",
                websocket=object(),
                bot_id="xiaowen",
                bot_tts_settings={"voice": "test"},
                trace=_trace(),
            )

        self.assertTrue(handled)
        request = pipeline_instance.run.await_args.args[0]
        self.assertEqual("companion_01", request.robot_id)
        self.assertEqual("round-1:playback", request.playback_id)
        manager.set_interrupted.assert_called_once_with("session-1", False)

    async def test_client_interrupt_cancels_workflow_without_closing_session_loop(self):
        coordinator = SimpleNamespace(classify_complex=AsyncMock(return_value=True))
        pipeline_instance = SimpleNamespace(
            run=AsyncMock(side_effect=asyncio.CancelledError())
        )
        manager = SimpleNamespace(
            is_interrupted=lambda session_id: True,
            is_current_round=lambda session_id, round_id: False,
        )
        with (
            patch.object(gateway, "get_workflow_coordinator", return_value=coordinator),
            patch.object(gateway, "WorkflowTTSPipeline", return_value=pipeline_instance),
            patch.object(gateway, "session_manager", manager),
        ):
            handled = await gateway._try_process_complex_workflow(
                text="查天气然后拨打视频",
                session_id="session-1",
                websocket=object(),
                bot_id="xiaowen",
                bot_tts_settings={},
                trace=_trace(),
            )

        self.assertTrue(handled)

    async def test_interrupt_during_classification_does_not_start_pipeline(self):
        coordinator = SimpleNamespace(classify_complex=AsyncMock(return_value=True))
        manager = SimpleNamespace(
            is_interrupted=lambda session_id: True,
            is_current_round=lambda session_id, round_id: False,
        )
        with (
            patch.object(gateway, "get_workflow_coordinator", return_value=coordinator),
            patch.object(gateway, "WorkflowTTSPipeline") as pipeline,
            patch.object(gateway, "session_manager", manager),
        ):
            handled = await gateway._try_process_complex_workflow(
                text="查天气再看穿搭",
                session_id="session-1",
                websocket=object(),
                bot_id="xiaowen",
                bot_tts_settings={},
                trace=_trace(),
            )

        self.assertTrue(handled)
        pipeline.assert_not_called()

    async def test_service_shutdown_cancellation_still_propagates(self):
        coordinator = SimpleNamespace(classify_complex=AsyncMock(return_value=True))
        pipeline_instance = SimpleNamespace(
            run=AsyncMock(side_effect=asyncio.CancelledError())
        )
        manager = SimpleNamespace(
            is_interrupted=lambda session_id: False,
            is_current_round=lambda session_id, round_id: True,
        )
        with (
            patch.object(gateway, "get_workflow_coordinator", return_value=coordinator),
            patch.object(gateway, "WorkflowTTSPipeline", return_value=pipeline_instance),
            patch.object(gateway, "session_manager", manager),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await gateway._try_process_complex_workflow(
                    text="查天气然后拨打视频",
                    session_id="session-1",
                    websocket=object(),
                    bot_id="xiaowen",
                    bot_tts_settings={},
                    trace=_trace(),
                )


if __name__ == "__main__":
    unittest.main()
