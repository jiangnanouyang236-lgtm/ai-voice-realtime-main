"""独立复杂任务 Coordinator；默认不接入现有语音主链。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from gateway.workflow_playback_barrier import (
    PLAYBACK_COMPLETE,
    PlaybackBarrierRegistry,
)


MAX_COORDINATED_STEPS = 8
DEFAULT_WORKFLOW_FAILURE_TEXT = (
    "这个任务有点复杂，我暂时没能安全地安排好，你可以分开告诉我。"
)


class WorkflowCoordinationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkflowRunRequest:
    text: str
    session_id: str
    bot_id: str
    robot_id: str
    trace_id: str
    round_id: str
    playback_id: str


@dataclass(frozen=True)
class WorkflowRunResult:
    success: bool
    plan_id: str = ""
    exit: bool = False
    completed_steps: tuple[str, ...] = ()
    error_code: str = ""
    message: str = ""


@dataclass
class _ActiveWorkflow:
    request: WorkflowRunRequest
    plan_id: str = ""
    cancel_reason: str = ""
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    cancel_remote_sent: bool = False


class WorkflowClient(Protocol):
    async def classify(self, request: WorkflowRunRequest) -> Any: ...

    async def plan(self, request: WorkflowRunRequest) -> Any: ...

    async def execute_step(
        self,
        request: WorkflowRunRequest,
        *,
        plan_id: str,
        step_id: str,
        playback_completed: bool,
    ) -> Any: ...

    def stream_synthesis(
        self,
        request: WorkflowRunRequest,
        *,
        plan_id: str,
    ) -> AsyncIterator[Any]: ...

    async def cancel(self, *, plan_id: str, session_id: str, reason: str) -> Any: ...


TextEmitter = Callable[[str], Awaitable[None]]
StepObserver = Callable[[str, str], Awaitable[None]]
PlanObserver = Callable[[str, bool], Awaitable[None]]
SynthesisObserver = Callable[[], Awaitable[None]]


class WorkflowCoordinator:
    def __init__(
        self,
        *,
        client: WorkflowClient,
        playback_barriers: PlaybackBarrierRegistry,
        playback_timeout_sec: float = 30.0,
    ) -> None:
        if playback_timeout_sec <= 0:
            raise ValueError("playback_timeout_sec 必须大于 0")
        self._client = client
        self._barriers = playback_barriers
        self._playback_timeout_sec = playback_timeout_sec
        self._active: dict[str, _ActiveWorkflow] = {}
        self._lock = asyncio.Lock()

    async def classify_complex(self, request: WorkflowRunRequest) -> bool:
        _validate_request(request)
        response = await self._client.classify(request)
        if not getattr(response, "success", False):
            return False
        return bool(getattr(response, "complex", False))

    async def run(
        self,
        request: WorkflowRunRequest,
        *,
        emit_text: TextEmitter,
        observe_step: StepObserver | None = None,
        observe_plan: PlanObserver | None = None,
        synthesis_finished: SynthesisObserver | None = None,
        failure_text: str = DEFAULT_WORKFLOW_FAILURE_TEXT,
    ) -> WorkflowRunResult:
        _validate_request(request)
        active = _ActiveWorkflow(request=request)
        active.task = asyncio.current_task()
        async with self._lock:
            previous = self._active.get(request.session_id)
            if previous is not None:
                raise WorkflowCoordinationError(
                    "workflow_already_active",
                    "当前会话已有复杂任务正在执行",
                )
            self._active[request.session_id] = active

        completed_steps: list[str] = []
        synthesis_closed = False

        async def emit_failure_if_needed() -> None:
            nonlocal synthesis_closed
            if synthesis_closed:
                return
            try:
                if failure_text:
                    await emit_text(failure_text)
                if synthesis_finished is not None:
                    await synthesis_finished()
            except Exception:
                pass
            finally:
                synthesis_closed = True

        try:
            plan_response = await self._client.plan(request)
            if not getattr(plan_response, "success", False):
                raise WorkflowCoordinationError(
                    getattr(plan_response, "error_code", "plan_failed") or "plan_failed",
                    getattr(plan_response, "message", "复杂任务规划失败") or "复杂任务规划失败",
                )
            active.plan_id = str(getattr(plan_response, "plan_id", "") or "").strip()
            steps = _parse_steps(getattr(plan_response, "plan_json", ""))
            if not active.plan_id:
                return _failed_result("invalid_plan_response", "规划服务未返回 plan_id")
            terminal_step = next((step for step in steps if step["terminal"]), None)
            if observe_plan is not None:
                await observe_plan(active.plan_id, terminal_step is not None)
            waiter = None
            if terminal_step is not None:
                waiter = self._barriers.prepare(
                    request.session_id,
                    request.round_id,
                    request.playback_id,
                )

            for step in steps:
                self._raise_if_cancelled(active)
                step_id = step["id"]
                if observe_step is not None:
                    await observe_step(step_id, "started")
                if step["type"] == "respond":
                    async for chunk in self._client.stream_synthesis(
                        request,
                        plan_id=active.plan_id,
                    ):
                        self._raise_if_cancelled(active)
                        error_code = str(getattr(chunk, "error_code", "") or "")
                        if error_code:
                            raise WorkflowCoordinationError(error_code, "复杂任务回复生成失败")
                        text = str(getattr(chunk, "text", "") or "")
                        if text:
                            await emit_text(text)
                    if synthesis_finished is not None:
                        await synthesis_finished()
                    synthesis_closed = True
                    completed_steps.append(step_id)
                    if observe_step is not None:
                        await observe_step(step_id, "completed")
                    if terminal_step is not None and waiter is not None:
                        report = await waiter.wait(self._playback_timeout_sec)
                        if report.status != PLAYBACK_COMPLETE:
                            report_code = (
                                report.status
                                if report.status.startswith("playback_")
                                else f"playback_{report.status}"
                            )
                            raise WorkflowCoordinationError(
                                report_code,
                                "播报未正常完成，终止动作不会执行",
                            )
                    continue

                response = await self._client.execute_step(
                    request,
                    plan_id=active.plan_id,
                    step_id=step_id,
                    playback_completed=bool(step["terminal"]),
                )
                if not getattr(response, "success", False):
                    raise WorkflowCoordinationError(
                        getattr(response, "error_code", "step_failed") or "step_failed",
                        getattr(response, "message", "复杂任务步骤执行失败") or "复杂任务步骤执行失败",
                    )
                completed_steps.append(step_id)
                if observe_step is not None:
                    await observe_step(step_id, "completed")
                if step["terminal"]:
                    return WorkflowRunResult(
                        success=True,
                        plan_id=active.plan_id,
                        exit=bool(getattr(response, "exit", False)),
                        completed_steps=tuple(completed_steps),
                    )

            return WorkflowRunResult(
                success=True,
                plan_id=active.plan_id,
                completed_steps=tuple(completed_steps),
            )
        except WorkflowCoordinationError as exc:
            await emit_failure_if_needed()
            self._barriers.cancel_session(request.session_id, reason=exc.code)
            await self._cancel_remote(active, exc.code)
            return WorkflowRunResult(
                success=False,
                plan_id=active.plan_id,
                completed_steps=tuple(completed_steps),
                error_code=exc.code,
                message=str(exc),
            )
        except asyncio.CancelledError:
            self._barriers.cancel_session(request.session_id, reason="coordinator_cancelled")
            await asyncio.shield(self._cancel_remote(active, "coordinator_cancelled"))
            raise
        except Exception:
            await emit_failure_if_needed()
            self._barriers.cancel_session(request.session_id, reason="coordinator_internal_error")
            await self._cancel_remote(active, "coordinator_internal_error")
            return WorkflowRunResult(
                success=False,
                plan_id=active.plan_id,
                completed_steps=tuple(completed_steps),
                error_code="coordinator_internal_error",
                message="复杂任务编排暂时不可用",
            )
        finally:
            async with self._lock:
                if self._active.get(request.session_id) is active:
                    self._active.pop(request.session_id, None)

    async def cancel_session(self, session_id: str, *, reason: str) -> bool:
        safe_session_id = str(session_id or "").strip()
        async with self._lock:
            active = self._active.get(safe_session_id)
        if active is None:
            return False
        active.cancel_reason = str(reason or "cancelled")
        active.cancelled.set()
        self._barriers.cancel_session(safe_session_id, reason=active.cancel_reason)
        if active.task is not None and active.task is not asyncio.current_task():
            active.task.cancel()
        await self._cancel_remote(active, active.cancel_reason)
        return True

    async def snapshot(self) -> dict[str, int]:
        async with self._lock:
            return {"active_workflows": len(self._active)}

    async def _cancel_remote(self, active: _ActiveWorkflow, reason: str) -> None:
        if not active.plan_id or active.cancel_remote_sent:
            return
        active.cancel_remote_sent = True
        try:
            await self._client.cancel(
                plan_id=active.plan_id,
                session_id=active.request.session_id,
                reason=reason,
            )
        except Exception:
            return

    @staticmethod
    def _raise_if_cancelled(active: _ActiveWorkflow) -> None:
        if active.cancelled.is_set():
            raise WorkflowCoordinationError(
                "workflow_cancelled",
                active.cancel_reason or "复杂任务已取消",
            )


def _validate_request(request: WorkflowRunRequest) -> None:
    for name in (
        "text",
        "session_id",
        "bot_id",
        "robot_id",
        "trace_id",
        "round_id",
        "playback_id",
    ):
        if not str(getattr(request, name, "") or "").strip():
            raise WorkflowCoordinationError("invalid_request", f"{name} 不能为空")


def _parse_steps(plan_json: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(plan_json)
        raw_steps = payload["steps"]
    except (TypeError, KeyError, json.JSONDecodeError):
        raise WorkflowCoordinationError("invalid_plan_response", "规划结果不是合法 JSON")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_COORDINATED_STEPS:
        raise WorkflowCoordinationError("invalid_plan_response", "规划步骤数量不合法")
    steps = []
    terminal_count = 0
    respond_count = 0
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            raise WorkflowCoordinationError("invalid_plan_response", "规划步骤格式不合法")
        step_id = item.get("id")
        step_type = item.get("type")
        terminal = item.get("terminal")
        if not isinstance(step_id, str) or not step_id or step_type not in {
            "tool",
            "vision_analyze",
            "respond",
        } or not isinstance(terminal, bool):
            raise WorkflowCoordinationError("invalid_plan_response", "规划步骤字段不合法")
        respond_count += int(step_type == "respond")
        terminal_count += int(terminal)
        if terminal and index != len(raw_steps) - 1:
            raise WorkflowCoordinationError("invalid_plan_response", "终止步骤必须位于最后")
        steps.append({"id": step_id, "type": step_type, "terminal": terminal})
    if respond_count != 1 or terminal_count > 1:
        raise WorkflowCoordinationError("invalid_plan_response", "规划必须包含一个回复步骤")
    return steps


def _failed_result(code: str, message: str) -> WorkflowRunResult:
    return WorkflowRunResult(success=False, error_code=code, message=message)
