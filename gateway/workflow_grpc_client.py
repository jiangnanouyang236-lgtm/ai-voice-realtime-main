"""同步 gRPC stub 到 asyncio Coordinator 的增量适配器。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Callable

from llm import workflow_service_pb2

from gateway.workflow_coordinator import WorkflowRunRequest


_STREAM_END = object()


class _StreamFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error


class GrpcWorkflowClient:
    def __init__(
        self,
        *,
        stub_provider: Callable[[], Any],
        unary_timeout_sec: float = 30.0,
        stream_timeout_sec: float = 120.0,
        queue_maxsize: int = 32,
    ) -> None:
        if unary_timeout_sec <= 0 or stream_timeout_sec <= 0 or queue_maxsize < 1:
            raise ValueError("gRPC timeout 和 queue_maxsize 必须大于 0")
        self._stub_provider = stub_provider
        self._unary_timeout_sec = unary_timeout_sec
        self._stream_timeout_sec = stream_timeout_sec
        self._queue_maxsize = queue_maxsize

    async def classify(self, request: WorkflowRunRequest):
        payload = workflow_service_pb2.ClassifyComplexRequest(
            text=request.text,
            session_id=request.session_id,
            bot_id=request.bot_id,
            robot_id=request.robot_id,
            trace_id=request.trace_id,
            round_id=request.round_id,
        )
        stub = self._stub_provider()
        return await asyncio.to_thread(
            stub.ClassifyComplex,
            payload,
            timeout=self._unary_timeout_sec,
        )

    async def plan(self, request: WorkflowRunRequest):
        payload = workflow_service_pb2.PlanComplexRequest(
            text=request.text,
            session_id=request.session_id,
            bot_id=request.bot_id,
            robot_id=request.robot_id,
            trace_id=request.trace_id,
            round_id=request.round_id,
        )
        stub = self._stub_provider()
        return await asyncio.to_thread(
            stub.PlanComplex,
            payload,
            timeout=self._unary_timeout_sec,
        )

    async def execute_step(
        self,
        request: WorkflowRunRequest,
        *,
        plan_id: str,
        step_id: str,
        playback_completed: bool,
    ):
        payload = workflow_service_pb2.ExecuteStepRequest(
            plan_id=plan_id,
            step_id=step_id,
            session_id=request.session_id,
            robot_id=request.robot_id,
            trace_id=request.trace_id,
            round_id=request.round_id,
            playback_completed=playback_completed,
            playback_id=request.playback_id if playback_completed else "",
        )
        stub = self._stub_provider()
        return await asyncio.to_thread(
            stub.ExecuteStep,
            payload,
            timeout=self._unary_timeout_sec,
        )

    async def cancel(self, *, plan_id: str, session_id: str, reason: str):
        payload = workflow_service_pb2.CancelWorkflowRequest(
            plan_id=plan_id,
            session_id=session_id,
            reason=reason,
        )
        stub = self._stub_provider()
        return await asyncio.to_thread(
            stub.CancelWorkflow,
            payload,
            timeout=self._unary_timeout_sec,
        )

    async def stream_synthesis(
        self,
        request: WorkflowRunRequest,
        *,
        plan_id: str,
    ):
        payload = workflow_service_pb2.SynthesisRequest(
            plan_id=plan_id,
            session_id=request.session_id,
            bot_id=request.bot_id,
            robot_id=request.robot_id,
            trace_id=request.trace_id,
            round_id=request.round_id,
            playback_id=request.playback_id,
        )
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_maxsize)
        stop = threading.Event()
        call_holder: dict[str, Any] = {"call": None}

        def put(item: Any) -> bool:
            if stop.is_set():
                return False
            future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
            try:
                future.result(timeout=5.0)
                return True
            except (TimeoutError, concurrent.futures.CancelledError):
                future.cancel()
                return False

        def run_stream() -> None:
            try:
                call = self._stub_provider().StreamSynthesis(
                    payload,
                    timeout=self._stream_timeout_sec,
                )
                call_holder["call"] = call
                if stop.is_set():
                    cancel = getattr(call, "cancel", None)
                    if callable(cancel):
                        cancel()
                    return
                for chunk in call:
                    if stop.is_set() or not put(chunk):
                        break
            except Exception as exc:
                if not stop.is_set():
                    put(_StreamFailure(exc))
            finally:
                call_holder["call"] = None
                put(_STREAM_END)

        future = loop.run_in_executor(None, run_stream)
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                if isinstance(item, _StreamFailure):
                    raise item.error
                yield item
        finally:
            stop.set()
            call = call_holder.get("call")
            cancel = getattr(call, "cancel", None)
            if callable(cancel):
                cancel()
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
