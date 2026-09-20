"""独立 WorkflowService 的可测试运行核心；默认不注册到 gRPC server。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import json
import time
from typing import Any, AsyncIterator, Awaitable, Callable
import uuid

from llm import workflow_service_pb2, workflow_service_pb2_grpc
from llm.workflow_planner import WorkflowPlanningError, WorkflowPlanningResult
from llm.workflow_protocol import ValidatedWorkflowPlan, WorkflowStep, workflow_plan_to_json


RUNTIME_READY = "ready"
RUNTIME_RUNNING = "running"
RUNTIME_WAITING_PLAYBACK = "waiting_playback"
RUNTIME_COMPLETED = "completed"
RUNTIME_EXITED = "exited"
RUNTIME_FAILED = "failed"
RUNTIME_CANCELLED = "cancelled"


@dataclass(frozen=True)
class StepExecutionOutcome:
    success: bool
    status: str
    result: Any = None
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True)
class WorkflowClassification:
    route_kind: str
    category: str = ""
    source: str = ""


@dataclass
class WorkflowRuntime:
    plan_id: str
    session_id: str
    bot_id: str
    robot_id: str
    trace_id: str
    round_id: str
    user_text: str
    plan: ValidatedWorkflowPlan
    created_at: float
    next_index: int = 0
    status: str = RUNTIME_READY
    cancelled: bool = False
    cancel_reason: str = ""
    operation_in_progress: bool = False
    expected_playback_id: str = ""
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


PlannerCallback = Callable[[Any], Awaitable[WorkflowPlanningResult]]
ClassifierCallback = Callable[[Any], Awaitable[WorkflowClassification]]
StepCallback = Callable[[WorkflowRuntime, WorkflowStep], Awaitable[StepExecutionOutcome]]
SynthesisCallback = Callable[[WorkflowRuntime, WorkflowStep], AsyncIterator[str]]


class WorkflowRuntimeStore:
    def __init__(self, *, max_entries: int = 128, ttl_sec: float = 900.0) -> None:
        if max_entries < 1 or ttl_sec <= 0:
            raise ValueError("工作流缓存容量和 TTL 必须大于 0")
        self._max_entries = max_entries
        self._ttl_sec = ttl_sec
        self._items: OrderedDict[str, WorkflowRuntime] = OrderedDict()
        self._lock = asyncio.Lock()

    async def put(self, runtime: WorkflowRuntime) -> None:
        async with self._lock:
            self._purge_locked()
            self._items[runtime.plan_id] = runtime
            self._items.move_to_end(runtime.plan_id)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)

    async def get(self, plan_id: str) -> WorkflowRuntime | None:
        async with self._lock:
            self._purge_locked()
            runtime = self._items.get(plan_id)
            if runtime is not None:
                self._items.move_to_end(plan_id)
            return runtime

    async def cancel(self, plan_id: str, session_id: str, reason: str) -> bool:
        runtime = await self.get(plan_id)
        if runtime is None or runtime.session_id != session_id:
            return False
        async with runtime.lock:
            if runtime.status in {RUNTIME_COMPLETED, RUNTIME_EXITED, RUNTIME_FAILED, RUNTIME_CANCELLED}:
                return runtime.status == RUNTIME_CANCELLED
            runtime.cancelled = True
            runtime.cancel_reason = reason
            runtime.status = RUNTIME_CANCELLED
            return True

    async def snapshot(self) -> dict[str, int]:
        async with self._lock:
            self._purge_locked()
            return {"active_workflows": len(self._items)}

    def _purge_locked(self) -> None:
        cutoff = time.monotonic() - self._ttl_sec
        for plan_id in list(self._items):
            if self._items[plan_id].created_at < cutoff:
                self._items.pop(plan_id, None)


class WorkflowRuntimeService(workflow_service_pb2_grpc.WorkflowServiceServicer):
    def __init__(
        self,
        *,
        classify: ClassifierCallback,
        planner: PlannerCallback,
        execute_step: StepCallback,
        synthesize: SynthesisCallback,
        store: WorkflowRuntimeStore | None = None,
    ) -> None:
        self._classify = classify
        self._planner = planner
        self._execute_step = execute_step
        self._synthesize = synthesize
        self.store = store or WorkflowRuntimeStore()

    async def ClassifyComplex(self, request, context):
        error = _validate_plan_request(request)
        if error:
            return workflow_service_pb2.ClassifyComplexResponse(
                success=False, error_code="invalid_request", message=error
            )
        try:
            classification = await self._classify(request)
        except Exception:
            return workflow_service_pb2.ClassifyComplexResponse(
                success=False,
                error_code="classifier_internal_error",
                message="复杂任务分类服务暂时不可用",
            )
        return workflow_service_pb2.ClassifyComplexResponse(
            success=True,
            complex=(
                classification.route_kind == "tool"
                and classification.category == "complex"
            ),
            route_kind=classification.route_kind,
            category=classification.category,
            source=classification.source,
        )

    async def PlanComplex(self, request, context):
        error = _validate_plan_request(request)
        if error:
            return workflow_service_pb2.PlanComplexResponse(
                success=False, error_code="invalid_request", message=error
            )
        try:
            planning = await self._planner(request)
        except WorkflowPlanningError as exc:
            return workflow_service_pb2.PlanComplexResponse(
                success=False,
                attempts=exc.attempts,
                error_code=exc.last_error_code,
                message="复杂任务计划未通过校验，未执行任何工具",
            )
        except Exception:
            return workflow_service_pb2.PlanComplexResponse(
                success=False,
                error_code="planner_internal_error",
                message="复杂任务规划服务暂时不可用",
            )

        plan_id = f"wf_{uuid.uuid4().hex}"
        runtime = WorkflowRuntime(
            plan_id=plan_id,
            session_id=request.session_id.strip(),
            bot_id=request.bot_id.strip(),
            robot_id=request.robot_id.strip(),
            trace_id=request.trace_id.strip(),
            round_id=request.round_id.strip(),
            user_text=request.text.strip(),
            plan=planning.plan,
            created_at=time.monotonic(),
        )
        await self.store.put(runtime)
        return workflow_service_pb2.PlanComplexResponse(
            success=True,
            plan_id=plan_id,
            plan_json=workflow_plan_to_json(planning.plan),
            attempts=planning.attempts,
        )

    async def ExecuteStep(self, request, context):
        runtime, error = await self._resolve_runtime(
            request.plan_id,
            request.session_id,
            request.robot_id,
            trace_id=request.trace_id,
        )
        if error:
            return _step_error(*error)

        async with runtime.lock:
            error_response = _check_runtime_before_operation(runtime, request.round_id)
            if error_response:
                return error_response
            step = _next_step(runtime)
            if step is None:
                return _step_error("workflow_finished", "工作流已经结束")
            if step.id != request.step_id:
                return _step_error("out_of_order_step", f"下一步骤应为 {step.id}")
            if step.type == "respond":
                return _step_error("synthesis_required", "respond 步骤必须调用 StreamSynthesis")
            if step.terminal and runtime.status == RUNTIME_WAITING_PLAYBACK:
                if not request.playback_completed:
                    return _step_error("playback_not_completed", "终止型步骤必须等待播报完成")
                if not runtime.expected_playback_id or request.playback_id != runtime.expected_playback_id:
                    return _step_error("playback_id_mismatch", "播报轮次不匹配")
            condition_result = _condition_result(runtime, step)
            if condition_result is False:
                runtime.results[step.id] = {"status": "skipped", "result": None}
                runtime.next_index += 1
                return workflow_service_pb2.ExecuteStepResponse(
                    success=True,
                    status="skipped",
                    result_json="null",
                    terminal=step.terminal,
                )
            if condition_result is None and step.condition is not None:
                return _step_error("condition_unavailable", "条件来源结果不可用")
            runtime.operation_in_progress = True
            runtime.status = RUNTIME_RUNNING

        try:
            outcome = await self._execute_step(runtime, step)
        except asyncio.CancelledError:
            await asyncio.shield(_mark_runtime_cancelled(runtime, "step_execution_cancelled"))
            raise
        except Exception:
            outcome = StepExecutionOutcome(
                success=False,
                status="failed",
                error_code="step_internal_error",
                message="步骤执行服务暂时不可用",
            )

        async with runtime.lock:
            runtime.operation_in_progress = False
            if runtime.cancelled:
                return _step_error("workflow_cancelled", "工作流已取消")
            stored_status = outcome.status if outcome.status else ("success" if outcome.success else "failed")
            runtime.results[step.id] = {"status": stored_status, "result": outcome.result}
            if outcome.success:
                runtime.next_index += 1
                if step.terminal:
                    runtime.status = RUNTIME_EXITED
                else:
                    runtime.status = RUNTIME_READY
            elif step.failure_policy == "abort":
                runtime.status = RUNTIME_FAILED
            else:
                runtime.next_index += 1
                runtime.status = RUNTIME_READY
            return workflow_service_pb2.ExecuteStepResponse(
                success=outcome.success,
                status=stored_status,
                result_json=_json_result(outcome.result),
                error_code=outcome.error_code,
                message=outcome.message,
                terminal=step.terminal,
                exit=bool(outcome.success and step.terminal),
            )

    async def StreamSynthesis(self, request, context):
        runtime, error = await self._resolve_runtime(
            request.plan_id,
            request.session_id,
            request.robot_id,
            trace_id=request.trace_id,
            bot_id=request.bot_id,
        )
        if error:
            yield workflow_service_pb2.SynthesisChunk(is_final=True, error_code=error[0])
            return

        async with runtime.lock:
            error_response = _check_runtime_before_synthesis(runtime, request.round_id)
            if error_response:
                yield workflow_service_pb2.SynthesisChunk(
                    is_final=True, error_code=error_response.error_code
                )
                return
            step = _next_step(runtime)
            if step is None or step.type != "respond":
                yield workflow_service_pb2.SynthesisChunk(
                    is_final=True, error_code="synthesis_not_expected"
                )
                return
            if runtime.plan.requires_playback_barrier and not request.playback_id.strip():
                yield workflow_service_pb2.SynthesisChunk(
                    is_final=True, error_code="playback_id_required"
                )
                return
            runtime.operation_in_progress = True
            runtime.status = RUNTIME_RUNNING
            runtime.expected_playback_id = request.playback_id.strip()

        emitted_text = False
        failed_code = ""
        stream_aborted = False
        try:
            async for text in self._synthesize(runtime, step):
                if runtime.cancelled:
                    failed_code = "workflow_cancelled"
                    break
                if not isinstance(text, str) or not text:
                    continue
                emitted_text = True
                yield workflow_service_pb2.SynthesisChunk(text=text, is_final=False)
        except (asyncio.CancelledError, GeneratorExit):
            stream_aborted = True
            raise
        except Exception:
            failed_code = "synthesis_internal_error"
        finally:
            async with runtime.lock:
                runtime.operation_in_progress = False
                if stream_aborted:
                    runtime.cancelled = True
                    runtime.cancel_reason = "synthesis_stream_closed"
                    runtime.status = RUNTIME_CANCELLED
                elif runtime.cancelled:
                    runtime.status = RUNTIME_CANCELLED
                    failed_code = "workflow_cancelled"
                elif failed_code or not emitted_text:
                    runtime.status = RUNTIME_FAILED
                    failed_code = failed_code or "empty_synthesis"
                else:
                    runtime.results[step.id] = {"status": "success", "result": None}
                    runtime.next_index += 1
                    runtime.status = (
                        RUNTIME_WAITING_PLAYBACK
                        if runtime.plan.terminal_step_id is not None
                        else RUNTIME_COMPLETED
                    )
        yield workflow_service_pb2.SynthesisChunk(is_final=True, error_code=failed_code)

    async def CancelWorkflow(self, request, context):
        if not request.plan_id.strip() or not request.session_id.strip():
            return workflow_service_pb2.CancelWorkflowResponse(
                success=False, message="plan_id 和 session_id 不能为空"
            )
        cancelled = await self.store.cancel(
            request.plan_id.strip(), request.session_id.strip(), request.reason.strip() or "cancelled"
        )
        return workflow_service_pb2.CancelWorkflowResponse(
            success=True,
            cancelled=cancelled,
            message="工作流已取消" if cancelled else "工作流不存在或已经结束",
        )

    async def _resolve_runtime(
        self,
        plan_id: str,
        session_id: str,
        robot_id: str,
        *,
        trace_id: str,
        bot_id: str | None = None,
    ):
        safe_plan_id = str(plan_id or "").strip()
        runtime = await self.store.get(safe_plan_id) if safe_plan_id else None
        if runtime is None:
            return None, ("workflow_not_found", "工作流不存在或已过期")
        if runtime.session_id != str(session_id or "").strip():
            return None, ("workflow_session_mismatch", "工作流会话不匹配")
        if runtime.robot_id != str(robot_id or "").strip():
            return None, ("workflow_robot_mismatch", "工作流机器人身份不匹配")
        if runtime.trace_id != str(trace_id or "").strip():
            return None, ("workflow_trace_mismatch", "工作流追踪标识不匹配")
        if bot_id is not None and runtime.bot_id != str(bot_id or "").strip():
            return None, ("workflow_bot_mismatch", "工作流 Bot 身份不匹配")
        return runtime, None


def _validate_plan_request(request) -> str | None:
    for name in ("text", "session_id", "bot_id", "robot_id", "trace_id", "round_id"):
        if not str(getattr(request, name, "") or "").strip():
            return f"{name} 不能为空"
    return None


def _check_runtime_before_operation(runtime: WorkflowRuntime, round_id: str):
    if runtime.round_id != str(round_id or "").strip():
        return _step_error("workflow_round_mismatch", "工作流轮次不匹配")
    if runtime.cancelled or runtime.status == RUNTIME_CANCELLED:
        return _step_error("workflow_cancelled", "工作流已取消")
    if runtime.operation_in_progress:
        return _step_error("workflow_busy", "工作流已有步骤正在执行")
    if runtime.status in {RUNTIME_COMPLETED, RUNTIME_EXITED, RUNTIME_FAILED}:
        return _step_error("workflow_finished", "工作流已经结束")
    return None


def _check_runtime_before_synthesis(runtime: WorkflowRuntime, round_id: str):
    return _check_runtime_before_operation(runtime, round_id)


def _next_step(runtime: WorkflowRuntime) -> WorkflowStep | None:
    if runtime.next_index >= len(runtime.plan.steps):
        return None
    return runtime.plan.steps[runtime.next_index]


def _condition_result(runtime: WorkflowRuntime, step: WorkflowStep) -> bool | None:
    condition = step.condition
    if condition is None:
        return True
    source = runtime.results.get(condition.source_step_id)
    if not source or source.get("status") != "success":
        return None
    source_result = source.get("result")
    if isinstance(source_result, dict):
        value = source_result.get(condition.output_key)
    else:
        value = source_result
    equals = value == condition.value
    return equals if condition.operator == "equals" else not equals


def _step_error(code: str, message: str):
    return workflow_service_pb2.ExecuteStepResponse(
        success=False, status="failed", error_code=code, message=message
    )


def _json_result(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


async def _mark_runtime_cancelled(runtime: WorkflowRuntime, reason: str) -> None:
    async with runtime.lock:
        runtime.operation_in_progress = False
        runtime.cancelled = True
        runtime.cancel_reason = reason
        runtime.status = RUNTIME_CANCELLED
