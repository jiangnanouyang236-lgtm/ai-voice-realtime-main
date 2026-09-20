import asyncio
from types import SimpleNamespace
import unittest

from gateway.workflow_coordinator import WorkflowRunRequest
from gateway.workflow_grpc_client import GrpcWorkflowClient


def _request():
    return WorkflowRunRequest(
        text="测试",
        session_id="session-1",
        bot_id="xiaowen",
        robot_id="companion_01",
        trace_id="trace-1",
        round_id="round-1",
        playback_id="round-1:playback",
    )


class _BlockingCall:
    def __init__(self, gate=None):
        self.gate = gate
        self.cancelled = False

    def __iter__(self):
        yield SimpleNamespace(text="第一段", is_final=False, error_code="")
        if self.gate is not None:
            self.gate.wait(timeout=2.0)
        if not self.cancelled:
            yield SimpleNamespace(text="第二段", is_final=False, error_code="")

    def cancel(self):
        self.cancelled = True
        if self.gate is not None:
            self.gate.set()


class _Stub:
    def __init__(self, call=None):
        self.call = call or _BlockingCall()
        self.requests = []

    def PlanComplex(self, request, timeout):
        self.requests.append(("plan", request, timeout))
        return SimpleNamespace(success=True)

    def ClassifyComplex(self, request, timeout):
        self.requests.append(("classify", request, timeout))
        return SimpleNamespace(success=True, complex=True)

    def ExecuteStep(self, request, timeout):
        self.requests.append(("execute", request, timeout))
        return SimpleNamespace(success=True)

    def CancelWorkflow(self, request, timeout):
        self.requests.append(("cancel", request, timeout))
        return SimpleNamespace(success=True)

    def StreamSynthesis(self, request, timeout):
        self.requests.append(("stream", request, timeout))
        return self.call


class WorkflowGrpcClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_unary_requests_preserve_identity_and_playback_proof(self):
        stub = _Stub()
        client = GrpcWorkflowClient(stub_provider=lambda: stub)

        classified = await client.classify(_request())
        await client.plan(_request())
        await client.execute_step(
            _request(),
            plan_id="wf-1",
            step_id="step-3",
            playback_completed=True,
        )
        await client.cancel(plan_id="wf-1", session_id="session-1", reason="interrupt")

        self.assertTrue(classified.complex)
        execute = stub.requests[2][1]
        self.assertEqual("companion_01", execute.robot_id)
        self.assertEqual("trace-1", execute.trace_id)
        self.assertTrue(execute.playback_completed)
        self.assertEqual("round-1:playback", execute.playback_id)

    async def test_stream_yields_first_chunk_without_waiting_for_completion(self):
        import threading

        gate = threading.Event()
        call = _BlockingCall(gate)
        stub = _Stub(call)
        client = GrpcWorkflowClient(stub_provider=lambda: stub)
        stream = client.stream_synthesis(_request(), plan_id="wf-1")

        first = await asyncio.wait_for(anext(stream), timeout=0.5)

        self.assertEqual("第一段", first.text)
        await stream.aclose()
        self.assertTrue(call.cancelled)

    async def test_stream_forwards_all_chunks_in_order(self):
        stub = _Stub()
        client = GrpcWorkflowClient(stub_provider=lambda: stub)

        chunks = [
            chunk.text
            async for chunk in client.stream_synthesis(_request(), plan_id="wf-1")
        ]

        self.assertEqual(["第一段", "第二段"], chunks)
        request = stub.requests[0][1]
        self.assertEqual("round-1:playback", request.playback_id)


if __name__ == "__main__":
    unittest.main()
