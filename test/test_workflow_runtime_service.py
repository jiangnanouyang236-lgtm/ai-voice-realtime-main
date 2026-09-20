import asyncio
import json
import unittest

from llm import workflow_service_pb2
from llm.workflow_planner import WorkflowPlanningResult
from llm.workflow_protocol import validate_workflow_plan
from llm.workflow_runtime_service import (
    RUNTIME_CANCELLED,
    RUNTIME_WAITING_PLAYBACK,
    StepExecutionOutcome,
    WorkflowClassification,
    WorkflowRuntimeService,
)


WEATHER = "websearch__search"
CALL = "robot_remote__call_video"


def _validated_plan(*, terminal=True, conditional=False):
    schemas = {
        WEATHER: {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        CALL: {"type": "object", "properties": {}, "additionalProperties": False},
    }
    steps = [
        {
            "id": "step_1",
            "type": "tool",
            "intent": "查询天气",
            "tool_name": WEATHER,
            "arguments": {"query": "青岛天气"},
            "depends_on": [],
            "failure_policy": "partial_response",
            "terminal": False,
        },
        {
            "id": "step_2",
            "type": "respond",
            "intent": "播报结果",
            "depends_on": ["step_1"],
            "failure_policy": "partial_response",
            "terminal": False,
            "vision": "none",
        },
    ]
    if terminal:
        steps.append(
            {
                "id": "step_3",
                "type": "tool",
                "intent": "拨打视频",
                "tool_name": CALL,
                "arguments": {},
                "depends_on": [],
                "failure_policy": "abort",
                "terminal": False,
            }
        )
    return validate_workflow_plan(
        {"version": 1, "goal": "查询并执行", "steps": steps},
        tool_schemas=schemas,
        terminal_tool_names={CALL},
    )


def _plan_request():
    return workflow_service_pb2.PlanComplexRequest(
        text="查天气然后拨打视频",
        session_id="session-1",
        bot_id="xiaowen",
        robot_id="companion_01",
        trace_id="trace-1",
        round_id="round-1",
    )


class WorkflowRuntimeServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.executed = []
        self.synthesis_chunks = ["今天", "有雨。", "现在为你拨打视频。"]

        async def planner(request):
            return WorkflowPlanningResult(
                plan=_validated_plan(), attempts=1, raw_text="{}"
            )

        async def classify(request):
            return WorkflowClassification(
                route_kind="tool", category="complex", source="test"
            )

        async def execute(runtime, step):
            self.executed.append(step.id)
            return StepExecutionOutcome(
                success=True,
                status="success",
                result={"published": True} if step.terminal else {"weather": "rain"},
            )

        async def synthesize(runtime, step):
            for chunk in self.synthesis_chunks:
                await asyncio.sleep(0)
                yield chunk

        self.service = WorkflowRuntimeService(
            classify=classify,
            planner=planner,
            execute_step=execute,
            synthesize=synthesize,
        )

    async def test_classify_complex(self):
        response = await self.service.ClassifyComplex(_plan_request(), None)

        self.assertTrue(response.success)
        self.assertTrue(response.complex)
        self.assertEqual("complex", response.category)

    async def plan(self):
        response = await self.service.PlanComplex(_plan_request(), None)
        self.assertTrue(response.success)
        return response

    async def execute(self, plan_id, step_id, *, playback_completed=False, playback_id=""):
        return await self.service.ExecuteStep(
            workflow_service_pb2.ExecuteStepRequest(
                plan_id=plan_id,
                step_id=step_id,
                session_id="session-1",
                robot_id="companion_01",
                trace_id="trace-1",
                round_id="round-1",
                playback_completed=playback_completed,
                playback_id=playback_id,
            ),
            None,
        )

    async def synthesize(self, plan_id):
        request = workflow_service_pb2.SynthesisRequest(
            plan_id=plan_id,
            session_id="session-1",
            bot_id="xiaowen",
            robot_id="companion_01",
            trace_id="trace-1",
            round_id="round-1",
            playback_id="round-1:playback",
        )
        return [chunk async for chunk in self.service.StreamSynthesis(request, None)]

    async def test_plan_execute_synthesize_then_terminal(self):
        planned = await self.plan()
        self.assertEqual(1, planned.attempts)
        self.assertEqual("step_1", json.loads(planned.plan_json)["steps"][0]["id"])

        weather = await self.execute(planned.plan_id, "step_1")
        self.assertTrue(weather.success)
        chunks = await self.synthesize(planned.plan_id)
        self.assertEqual(self.synthesis_chunks, [chunk.text for chunk in chunks if not chunk.is_final])
        self.assertTrue(chunks[-1].is_final)
        self.assertFalse(chunks[-1].error_code)

        runtime = await self.service.store.get(planned.plan_id)
        self.assertEqual(RUNTIME_WAITING_PLAYBACK, runtime.status)
        blocked = await self.execute(planned.plan_id, "step_3")
        self.assertEqual("playback_not_completed", blocked.error_code)
        terminal = await self.execute(
            planned.plan_id,
            "step_3",
            playback_completed=True,
            playback_id="round-1:playback",
        )
        self.assertTrue(terminal.success)
        self.assertTrue(terminal.terminal)
        self.assertTrue(terminal.exit)
        self.assertEqual(["step_1", "step_3"], self.executed)

    async def test_out_of_order_step_is_rejected_without_execution(self):
        planned = await self.plan()

        response = await self.execute(planned.plan_id, "step_3")

        self.assertFalse(response.success)
        self.assertEqual("out_of_order_step", response.error_code)
        self.assertEqual([], self.executed)

    async def test_wrong_session_robot_and_round_are_rejected(self):
        planned = await self.plan()
        base = dict(
            plan_id=planned.plan_id,
            step_id="step_1",
            session_id="wrong",
            robot_id="companion_01",
            trace_id="trace-1",
            round_id="round-1",
            playback_id="round-1:playback",
        )
        response = await self.service.ExecuteStep(
            workflow_service_pb2.ExecuteStepRequest(**base), None
        )
        self.assertEqual("workflow_session_mismatch", response.error_code)

        base.update(session_id="session-1", robot_id="wrong")
        response = await self.service.ExecuteStep(
            workflow_service_pb2.ExecuteStepRequest(**base), None
        )
        self.assertEqual("workflow_robot_mismatch", response.error_code)

        base.update(robot_id="companion_01", round_id="wrong")
        response = await self.service.ExecuteStep(
            workflow_service_pb2.ExecuteStepRequest(**base), None
        )
        self.assertEqual("workflow_round_mismatch", response.error_code)

    async def test_cancel_prevents_later_steps(self):
        planned = await self.plan()
        cancelled = await self.service.CancelWorkflow(
            workflow_service_pb2.CancelWorkflowRequest(
                plan_id=planned.plan_id,
                session_id="session-1",
                reason="client_interrupt",
            ),
            None,
        )

        self.assertTrue(cancelled.cancelled)
        response = await self.execute(planned.plan_id, "step_1")
        self.assertEqual("workflow_cancelled", response.error_code)
        runtime = await self.service.store.get(planned.plan_id)
        self.assertEqual(RUNTIME_CANCELLED, runtime.status)

    async def test_synthesis_chunks_are_not_buffered(self):
        planned = await self.plan()
        await self.execute(planned.plan_id, "step_1")
        request = workflow_service_pb2.SynthesisRequest(
            plan_id=planned.plan_id,
            session_id="session-1",
            bot_id="xiaowen",
            robot_id="companion_01",
            trace_id="trace-1",
            round_id="round-1",
            playback_id="round-1:playback",
        )
        stream = self.service.StreamSynthesis(request, None)

        first = await anext(stream)

        self.assertEqual("今天", first.text)
        self.assertFalse(first.is_final)
        await stream.aclose()
        runtime = await self.service.store.get(planned.plan_id)
        self.assertEqual(RUNTIME_CANCELLED, runtime.status)
        self.assertFalse(runtime.operation_in_progress)

    async def test_identity_and_playback_id_mismatch_are_rejected(self):
        planned = await self.plan()
        request = workflow_service_pb2.ExecuteStepRequest(
            plan_id=planned.plan_id,
            step_id="step_1",
            session_id="session-1",
            robot_id="companion_01",
            trace_id="wrong",
            round_id="round-1",
        )
        response = await self.service.ExecuteStep(request, None)
        self.assertEqual("workflow_trace_mismatch", response.error_code)

        await self.execute(planned.plan_id, "step_1")
        await self.synthesize(planned.plan_id)
        response = await self.execute(
            planned.plan_id,
            "step_3",
            playback_completed=True,
            playback_id="wrong:playback",
        )
        self.assertEqual("playback_id_mismatch", response.error_code)

    async def test_invalid_plan_request_does_not_call_planner(self):
        response = await self.service.PlanComplex(
            workflow_service_pb2.PlanComplexRequest(text="缺少身份"), None
        )

        self.assertFalse(response.success)
        self.assertEqual("invalid_request", response.error_code)


if __name__ == "__main__":
    unittest.main()
