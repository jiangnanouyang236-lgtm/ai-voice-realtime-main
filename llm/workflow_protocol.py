"""复杂任务计划的纯数据协议与离线校验器。

本模块不调用模型、MCP、Gateway 或 TTS，也不接入现有 StreamChat 路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Mapping, Sequence


WORKFLOW_VERSION = 1
MAX_WORKFLOW_STEPS = 8
STEP_TYPES = frozenset({"tool", "vision_analyze", "respond"})
FAILURE_POLICIES = frozenset({"abort", "partial_response", "skip"})
VISION_MODES = frozenset({"none", "latest_optional", "latest_required"})
CONDITION_OPERATORS = frozenset({"equals", "not_equals"})
FORBIDDEN_MODEL_ARGUMENT_KEYS = frozenset(
    {
        "robot_id",
        "robot_secret",
        "session_id",
        "api_key",
        "authorization",
        "token",
    }
)

_STEP_ID_RE = re.compile(r"^step_[1-9][0-9]*$")
_COMMON_STEP_FIELDS = frozenset(
    {"id", "type", "intent", "depends_on", "failure_policy", "terminal"}
)
_TYPE_FIELDS = {
    "tool": frozenset({"tool_name", "arguments", "when"}),
    "vision_analyze": frozenset({"output_key", "allowed_values"}),
    "respond": frozenset({"vision"}),
}


class WorkflowValidationError(ValueError):
    """计划在执行任何副作用前被拒绝。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkflowCondition:
    source_step_id: str
    output_key: str
    operator: str
    value: Any


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    type: str
    intent: str
    depends_on: tuple[str, ...]
    failure_policy: str
    terminal: bool
    tool_name: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    condition: WorkflowCondition | None = None
    output_key: str | None = None
    allowed_values: tuple[Any, ...] = ()
    vision: str = "none"


@dataclass(frozen=True)
class ValidatedWorkflowPlan:
    version: int
    goal: str
    steps: tuple[WorkflowStep, ...]
    terminal_step_id: str | None
    requires_playback_barrier: bool


def workflow_plan_to_dict(plan: ValidatedWorkflowPlan) -> dict[str, Any]:
    steps = []
    for step in plan.steps:
        item: dict[str, Any] = {
            "id": step.id,
            "type": step.type,
            "intent": step.intent,
            "depends_on": list(step.depends_on),
            "failure_policy": step.failure_policy,
            "terminal": step.terminal,
        }
        if step.type == "tool":
            item["tool_name"] = step.tool_name
            item["arguments"] = dict(step.arguments)
            if step.condition is not None:
                item["when"] = {
                    "source": f"{step.condition.source_step_id}.{step.condition.output_key}",
                    "operator": step.condition.operator,
                    "value": step.condition.value,
                }
        elif step.type == "vision_analyze":
            item["output_key"] = step.output_key
            item["allowed_values"] = list(step.allowed_values)
        else:
            item["vision"] = step.vision
        steps.append(item)
    return {"version": plan.version, "goal": plan.goal, "steps": steps}


def workflow_plan_to_json(plan: ValidatedWorkflowPlan) -> str:
    return json.dumps(workflow_plan_to_dict(plan), ensure_ascii=False, separators=(",", ":"))


def tool_schemas_from_openai_tools(tools: Sequence[Mapping[str, Any]] | None) -> dict[str, Mapping[str, Any]]:
    """从现有 OpenAI tools 描述中提取名称与参数 Schema。"""
    result: dict[str, Mapping[str, Any]] = {}
    for tool in tools or ():
        function = tool.get("function") if isinstance(tool, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        parameters = function.get("parameters", {})
        if isinstance(name, str) and name and isinstance(parameters, Mapping):
            result[name] = parameters
    return result


def validate_workflow_plan(
    raw_plan: Mapping[str, Any],
    *,
    tool_schemas: Mapping[str, Mapping[str, Any]],
    terminal_tool_names: set[str] | frozenset[str],
) -> ValidatedWorkflowPlan:
    """整体校验并规范化计划；成功前不执行任何外部调用。"""
    if not isinstance(raw_plan, Mapping):
        _fail("invalid_plan", "计划必须是 JSON 对象")
    unknown_top = set(raw_plan) - {"version", "goal", "steps"}
    if unknown_top:
        _fail("unknown_plan_fields", f"计划包含未知字段: {sorted(unknown_top)}")
    if raw_plan.get("version") != WORKFLOW_VERSION:
        _fail("unsupported_version", f"只支持计划版本 {WORKFLOW_VERSION}")
    goal = raw_plan.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        _fail("invalid_goal", "goal 必须是非空字符串")
    raw_steps = raw_plan.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_WORKFLOW_STEPS:
        _fail("invalid_step_count", f"steps 数量必须为 1-{MAX_WORKFLOW_STEPS}")

    step_ids: list[str] = []
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            _fail("invalid_step", "每个步骤必须是 JSON 对象")
        step_id = raw_step.get("id")
        if not isinstance(step_id, str) or not _STEP_ID_RE.fullmatch(step_id):
            _fail("invalid_step_id", f"无效步骤 ID: {step_id!r}")
        if step_id in raw_by_id:
            _fail("duplicate_step_id", f"重复步骤 ID: {step_id}")
        step_ids.append(step_id)
        raw_by_id[step_id] = raw_step

    parsed = [
        _parse_step(
            raw_by_id[step_id],
            known_step_ids=frozenset(step_ids),
            tool_schemas=tool_schemas,
            terminal_tool_names=terminal_tool_names,
        )
        for step_id in step_ids
    ]
    parsed_by_id = {step.id: step for step in parsed}

    _validate_dependencies(parsed, parsed_by_id)
    _validate_conditions(parsed, parsed_by_id)
    _validate_response_step(parsed)
    terminal_steps = [step for step in parsed if step.terminal]
    if len(terminal_steps) > 1:
        _fail("multiple_terminal_steps", "一个计划最多允许一个终止型步骤")
    if terminal_steps:
        terminal_id = terminal_steps[0].id
        if any(terminal_id in step.depends_on for step in parsed):
            _fail("terminal_has_dependents", "终止型步骤不能被其它步骤依赖")
    else:
        terminal_id = None

    ordered = _stable_topological_sort(parsed)
    if terminal_id:
        ordered = [step for step in ordered if step.id != terminal_id] + [parsed_by_id[terminal_id]]

    respond = next(step for step in parsed if step.type == "respond")
    result_steps = {step.id for step in parsed if step.type in {"tool", "vision_analyze"} and not step.terminal}
    respond_ancestors = _dependency_ancestors(respond.id, parsed_by_id)
    missing_results = result_steps - respond_ancestors
    if missing_results:
        _fail(
            "response_missing_dependencies",
            f"最终 respond 未依赖结果步骤: {sorted(missing_results)}",
        )

    return ValidatedWorkflowPlan(
        version=WORKFLOW_VERSION,
        goal=goal.strip(),
        steps=tuple(ordered),
        terminal_step_id=terminal_id,
        requires_playback_barrier=terminal_id is not None,
    )


def _parse_step(
    raw: Mapping[str, Any],
    *,
    known_step_ids: frozenset[str],
    tool_schemas: Mapping[str, Mapping[str, Any]],
    terminal_tool_names: set[str] | frozenset[str],
) -> WorkflowStep:
    step_id = str(raw["id"])
    step_type = raw.get("type")
    if step_type not in STEP_TYPES:
        _fail("invalid_step_type", f"{step_id} 的 type 不合法: {step_type!r}")
    allowed_fields = _COMMON_STEP_FIELDS | _TYPE_FIELDS[step_type]
    unknown_fields = set(raw) - allowed_fields
    if unknown_fields:
        _fail("unknown_step_fields", f"{step_id} 包含未知字段: {sorted(unknown_fields)}")

    intent = raw.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        _fail("invalid_intent", f"{step_id} 的 intent 必须是非空字符串")
    depends_on_raw = raw.get("depends_on")
    if not isinstance(depends_on_raw, list) or not all(isinstance(item, str) for item in depends_on_raw):
        _fail("invalid_dependencies", f"{step_id} 的 depends_on 必须是字符串数组")
    if len(depends_on_raw) != len(set(depends_on_raw)):
        _fail("duplicate_dependencies", f"{step_id} 包含重复依赖")
    if any(item not in known_step_ids for item in depends_on_raw):
        _fail("unknown_dependency", f"{step_id} 引用了不存在的依赖")
    if step_id in depends_on_raw:
        _fail("self_dependency", f"{step_id} 不能依赖自身")

    failure_policy = raw.get("failure_policy")
    if failure_policy not in FAILURE_POLICIES:
        _fail("invalid_failure_policy", f"{step_id} 的 failure_policy 不合法")
    if not isinstance(raw.get("terminal"), bool):
        _fail("invalid_terminal", f"{step_id} 的 terminal 必须是布尔值")

    if step_type == "tool":
        tool_name = raw.get("tool_name")
        arguments = raw.get("arguments")
        if not isinstance(tool_name, str) or tool_name not in tool_schemas:
            _fail("unknown_tool", f"{step_id} 使用未知或未授权工具: {tool_name!r}")
        if not isinstance(arguments, Mapping):
            _fail("invalid_tool_arguments", f"{step_id} 的 arguments 必须是对象")
        forbidden = _find_forbidden_argument_keys(arguments)
        if forbidden:
            _fail("forbidden_tool_arguments", f"{step_id} 包含服务端身份字段: {sorted(forbidden)}")
        _validate_json_schema(arguments, tool_schemas[tool_name], path=f"{step_id}.arguments")
        condition = _parse_condition(raw.get("when"), step_id) if "when" in raw else None
        return WorkflowStep(
            id=step_id,
            type=step_type,
            intent=intent.strip(),
            depends_on=tuple(depends_on_raw),
            failure_policy=failure_policy,
            terminal=tool_name in terminal_tool_names,
            tool_name=tool_name,
            arguments=dict(arguments),
            condition=condition,
        )

    if raw.get("terminal"):
        _fail("non_tool_terminal", f"{step_id} 不是工具步骤，不能标记 terminal")
    if step_type == "vision_analyze":
        output_key = raw.get("output_key")
        allowed_values = raw.get("allowed_values")
        if not isinstance(output_key, str) or not output_key.strip():
            _fail("invalid_output_key", f"{step_id} 的 output_key 必须是非空字符串")
        if not isinstance(allowed_values, list) or not allowed_values:
            _fail("invalid_allowed_values", f"{step_id} 的 allowed_values 不能为空")
        if len({repr(value) for value in allowed_values}) != len(allowed_values):
            _fail("duplicate_allowed_values", f"{step_id} 的 allowed_values 不能重复")
        return WorkflowStep(
            id=step_id,
            type=step_type,
            intent=intent.strip(),
            depends_on=tuple(depends_on_raw),
            failure_policy=failure_policy,
            terminal=False,
            output_key=output_key.strip(),
            allowed_values=tuple(allowed_values),
        )

    vision = raw.get("vision")
    if vision not in VISION_MODES:
        _fail("invalid_vision_mode", f"{step_id} 的 vision 不合法")
    return WorkflowStep(
        id=step_id,
        type=step_type,
        intent=intent.strip(),
        depends_on=tuple(depends_on_raw),
        failure_policy=failure_policy,
        terminal=False,
        vision=vision,
    )


def _parse_condition(raw: Any, step_id: str) -> WorkflowCondition:
    if not isinstance(raw, Mapping) or set(raw) != {"source", "operator", "value"}:
        _fail("invalid_condition", f"{step_id} 的 when 字段不合法")
    source = raw.get("source")
    if not isinstance(source, str) or "." not in source:
        _fail("invalid_condition_source", f"{step_id} 的条件 source 不合法")
    source_step_id, output_key = source.split(".", 1)
    if not source_step_id or not output_key:
        _fail("invalid_condition_source", f"{step_id} 的条件 source 不合法")
    operator = raw.get("operator")
    if operator not in CONDITION_OPERATORS:
        _fail("invalid_condition_operator", f"{step_id} 的条件 operator 不合法")
    return WorkflowCondition(source_step_id, output_key, operator, raw.get("value"))


def _validate_dependencies(steps: Sequence[WorkflowStep], by_id: Mapping[str, WorkflowStep]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> None:
        if step_id in visiting:
            _fail("dependency_cycle", "计划存在循环依赖")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in by_id[step_id].depends_on:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step in steps:
        visit(step.id)


def _validate_conditions(steps: Sequence[WorkflowStep], by_id: Mapping[str, WorkflowStep]) -> None:
    for step in steps:
        condition = step.condition
        if condition is None:
            continue
        source = by_id.get(condition.source_step_id)
        if source is None or source.type != "vision_analyze" or source.output_key != condition.output_key:
            _fail("invalid_condition_source", f"{step.id} 的条件没有引用有效视觉输出")
        if condition.source_step_id not in _dependency_ancestors(step.id, by_id):
            _fail("condition_missing_dependency", f"{step.id} 必须依赖条件来源步骤")
        if condition.value not in source.allowed_values:
            _fail("invalid_condition_value", f"{step.id} 的条件值不在视觉输出白名单内")


def _validate_response_step(steps: Sequence[WorkflowStep]) -> None:
    responses = [step for step in steps if step.type == "respond"]
    if len(responses) != 1:
        _fail("invalid_response_count", "复杂计划必须且只能包含一个 respond 步骤")


def _stable_topological_sort(steps: Sequence[WorkflowStep]) -> list[WorkflowStep]:
    order = {step.id: index for index, step in enumerate(steps)}
    by_id = {step.id: step for step in steps}
    pending = {step.id: set(step.depends_on) for step in steps}
    result: list[WorkflowStep] = []
    while pending:
        ready = sorted((step_id for step_id, dependencies in pending.items() if not dependencies), key=order.get)
        if not ready:
            _fail("dependency_cycle", "计划存在循环依赖")
        for step_id in ready:
            result.append(by_id[step_id])
            pending.pop(step_id)
            for dependencies in pending.values():
                dependencies.discard(step_id)
    return result


def _dependency_ancestors(step_id: str, by_id: Mapping[str, WorkflowStep]) -> set[str]:
    result: set[str] = set()
    stack = list(by_id[step_id].depends_on)
    while stack:
        dependency = stack.pop()
        if dependency in result:
            continue
        result.add(dependency)
        stack.extend(by_id[dependency].depends_on)
    return result


def _find_forbidden_argument_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_MODEL_ARGUMENT_KEYS:
                found.add(normalized)
            found.update(_find_forbidden_argument_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_forbidden_argument_keys(item))
    return found


def _validate_json_schema(value: Any, schema: Mapping[str, Any], *, path: str) -> None:
    if "enum" in schema and value not in schema["enum"]:
        _fail("tool_schema_mismatch", f"{path} 不在枚举范围内")
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            _fail("tool_schema_mismatch", f"{path} 必须是对象")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            _fail("tool_schema_mismatch", f"{path} 缺少必填字段: {missing}")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                _fail("tool_schema_mismatch", f"{path} 包含未知字段: {sorted(unknown)}")
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, Mapping):
                _validate_json_schema(item, child_schema, path=f"{path}.{key}")
    elif expected_type == "array":
        if not isinstance(value, list):
            _fail("tool_schema_mismatch", f"{path} 必须是数组")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, path=f"{path}[{index}]")
    elif expected_type == "string" and not isinstance(value, str):
        _fail("tool_schema_mismatch", f"{path} 必须是字符串")
    elif expected_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        _fail("tool_schema_mismatch", f"{path} 必须是整数")
    elif expected_type == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        _fail("tool_schema_mismatch", f"{path} 必须是数字")
    elif expected_type == "boolean" and not isinstance(value, bool):
        _fail("tool_schema_mismatch", f"{path} 必须是布尔值")


def _fail(code: str, message: str) -> None:
    raise WorkflowValidationError(code, message)
