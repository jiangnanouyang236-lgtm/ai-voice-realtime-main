"""复杂任务 Planner 核心；通过依赖注入调用主模型，不接入现有 gRPC。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from typing import Any, Awaitable, Callable, Mapping, Sequence

from llm.workflow_protocol import (
    ValidatedWorkflowPlan,
    WorkflowValidationError,
    validate_workflow_plan,
)


PLANNER_TEMPERATURE = 0.0
PLANNER_MAX_TOKENS = 1200
PLANNER_MAX_ATTEMPTS = 2
PLANNER_HISTORY_TURNS = 5

logger = logging.getLogger(__name__)

PlannerGenerate = Callable[..., Awaitable[str]]


class WorkflowPlanningError(RuntimeError):
    def __init__(self, message: str, *, attempts: int, last_error_code: str):
        super().__init__(message)
        self.attempts = attempts
        self.last_error_code = last_error_code


@dataclass(frozen=True)
class WorkflowPlanningResult:
    plan: ValidatedWorkflowPlan
    attempts: int
    raw_text: str


class ComplexWorkflowPlanner:
    def __init__(
        self,
        *,
        generate: PlannerGenerate,
        tool_schemas: Mapping[str, Mapping[str, Any]],
        terminal_tool_names: set[str] | frozenset[str],
        max_attempts: int = PLANNER_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        self._generate = generate
        self._tool_schemas = dict(tool_schemas)
        self._terminal_tool_names = frozenset(terminal_tool_names)
        self._max_attempts = max_attempts

    async def plan(
        self,
        *,
        user_text: str,
        conversation_messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> WorkflowPlanningResult:
        if not isinstance(user_text, str) or not user_text.strip():
            raise ValueError("user_text 必须是非空字符串")

        base_messages = build_planner_messages(
            user_text=user_text.strip(),
            conversation_messages=conversation_messages,
            tool_schemas=self._tool_schemas,
            terminal_tool_names=self._terminal_tool_names,
        )
        feedback: str | None = None
        last_error_code = "unknown"
        last_raw_text = ""

        for attempt in range(1, self._max_attempts + 1):
            messages = list(base_messages)
            if feedback:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一次计划未通过程序校验。请修正后重新输出完整 JSON，"
                            "不要解释；重新检查顶层字段以及每一步的全部必填字段。"
                            f"校验错误：{feedback}"
                        ),
                    }
                )
            try:
                raw_text = await self._generate(
                    messages,
                    temperature=PLANNER_TEMPERATURE,
                    max_tokens=PLANNER_MAX_TOKENS,
                )
            except asyncio.TimeoutError:
                last_error_code = "planner_timeout"
                feedback = "planner_timeout：规划模型调用超时"
                continue
            except Exception:
                last_error_code = "planner_model_error"
                feedback = "planner_model_error：规划模型调用失败"
                continue
            last_raw_text = str(raw_text or "").strip()
            try:
                raw_plan = json.loads(last_raw_text)
            except (TypeError, json.JSONDecodeError):
                last_error_code = "invalid_json"
                feedback = "invalid_json：输出不是单个合法 JSON 对象"
                logger.warning(
                    "复杂任务 Planner 输出不是合法 JSON: attempt=%s chars=%s",
                    attempt,
                    len(last_raw_text),
                )
                continue

            normalized_plan = _normalize_model_plan(raw_plan)
            try:
                validated = validate_workflow_plan(
                    normalized_plan,
                    tool_schemas=self._tool_schemas,
                    terminal_tool_names=self._terminal_tool_names,
                )
            except WorkflowValidationError as exc:
                last_error_code = exc.code
                feedback = f"{exc.code}：{exc}"
                logger.warning(
                    "复杂任务 Planner 校验失败: attempt=%s code=%s message=%s",
                    attempt,
                    exc.code,
                    exc,
                )
                continue

            return WorkflowPlanningResult(plan=validated, attempts=attempt, raw_text=last_raw_text)

        raise WorkflowPlanningError(
            "复杂任务计划未通过校验，未执行任何工具",
            attempts=self._max_attempts,
            last_error_code=last_error_code,
        )


def build_planner_messages(
    *,
    user_text: str,
    conversation_messages: Sequence[Mapping[str, Any]] | None,
    tool_schemas: Mapping[str, Mapping[str, Any]],
    terminal_tool_names: Sequence[str] | set[str] | frozenset[str],
) -> list[dict[str, str]]:
    history = recent_complete_turns(conversation_messages, max_turns=PLANNER_HISTORY_TURNS)
    capabilities = [
        {"tool_name": name, "arguments_schema": schema}
        for name, schema in sorted(tool_schemas.items())
    ]
    weather_vision_example = _build_weather_vision_example(tool_schemas)
    system_prompt = (
        "你是机器人语音助手的复杂任务 Planner。只输出一个 JSON 对象，不要 Markdown、解释或代码围栏。"
        "version 必须是 1；steps 只能有 1 到 8 个。"
        "顶层必须严格包含 version、goal、steps。"
        "步骤 type 只允许 tool、vision_analyze、respond；必须且只能有一个 respond。"
        "每个步骤都必须包含 id、type、intent、depends_on、failure_policy、terminal；"
        "id 必须按 step_1、step_2 连续编号，depends_on 只能填写这些步骤 ID。"
        "tool 步骤还必须包含 tool_name、arguments；"
        "respond 步骤还必须包含 vision，且不能包含 arguments 或 text；"
        "vision_analyze 步骤还必须包含 output_key、allowed_values。"
        "所有非工具步骤的 terminal 必须为 false；failure_policy 必须是 abort、partial_response 或 skip。"
        "tool_name 和 arguments 必须来自允许能力；不要生成 robot_id、session_id、密钥、token 或 URL。"
        "图片由程序获取：需要先看图再决定动作时使用 vision_analyze；"
        "只需在最终回答结合最新图片时，在 respond 设置 vision=latest_required 或 latest_optional。"
        "例如‘查询天气，再评价我的穿搭’应规划为天气 tool -> respond，"
        "respond 设置 vision=latest_required 并依赖天气 tool；不要额外生成 vision_analyze。"
        "respond 必须直接或间接依赖所有需要汇总的非终止 tool/vision_analyze 步骤。"
        "terminal 值仅作提示，程序会按服务端策略重新计算；终止型任务由程序移动到最后。"
        "depends_on 必须完整表达结果依赖；failure_policy 只允许 abort、partial_response、skip。"
        "条件只允许 when={source,operator,value}，operator 只允许 equals 或 not_equals。"
        "允许能力："
        + json.dumps(capabilities, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "。终止型工具："
        + json.dumps(sorted(terminal_tool_names), ensure_ascii=False, separators=(",", ":"))
        + "。"
        + weather_vision_example
    )
    context_lines = []
    for message in history:
        label = "用户" if message["role"] == "user" else "助手"
        context_lines.append(f"{label}：{message['content']}")
    context = "\n".join(context_lines) if context_lines else "（无）"
    user_prompt = (
        f"最近 {PLANNER_HISTORY_TURNS} 个完整对话轮次：\n{context}\n\n"
        f"用户最新请求：\n{user_text}\n\n"
        "请重点处理最新请求；只有存在省略、指代、继续、修改或取消时才参考历史。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _normalize_model_plan(raw_plan: Any) -> Any:
    """只修正不可能产生副作用的格式偏差；工具与参数仍交给严格校验器。"""
    if not isinstance(raw_plan, Mapping):
        return raw_plan
    normalized = dict(raw_plan)
    raw_steps = raw_plan.get("steps")
    if not isinstance(raw_steps, list):
        return normalized
    steps = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, Mapping):
            steps.append(raw_step)
            continue
        step = dict(raw_step)
        if step.get("type") != "tool" and isinstance(step.get("terminal"), bool):
            step["terminal"] = False
        steps.append(step)
    normalized["steps"] = steps
    return normalized


def _build_weather_vision_example(
    tool_schemas: Mapping[str, Mapping[str, Any]],
) -> str:
    for tool_name, schema in sorted(tool_schemas.items()):
        if "websearch" not in tool_name.lower() and "weather" not in tool_name.lower():
            continue
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(properties, Mapping):
            continue
        argument_name = next(
            (
                name
                for name in ("query", "search_query", "q", "keyword", "city")
                if name in properties
            ),
            None,
        )
        if not argument_name:
            continue
        example = {
            "version": 1,
            "goal": "查询天气并评价穿搭",
            "steps": [
                {
                    "id": "step_1",
                    "type": "tool",
                    "intent": "查询天气",
                    "depends_on": [],
                    "failure_policy": "partial_response",
                    "terminal": False,
                    "tool_name": tool_name,
                    "arguments": {argument_name: "青岛今天天气"},
                },
                {
                    "id": "step_2",
                    "type": "respond",
                    "intent": "结合天气和最新图片评价穿搭",
                    "depends_on": ["step_1"],
                    "failure_policy": "partial_response",
                    "terminal": False,
                    "vision": "latest_required",
                },
            ],
        }
        return "天气加穿搭的合法格式范例：" + json.dumps(
            example,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return ""


def recent_complete_turns(
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    max_turns: int,
) -> list[dict[str, str]]:
    """仅保留完整 user/assistant 对；排除工具协议文本和半轮。"""
    if max_turns < 1:
        return []
    turns: list[tuple[dict[str, str], dict[str, str]]] = []
    pending_user: dict[str, str] | None = None
    for message in messages or ():
        role = message.get("role") if isinstance(message, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
            continue
        clean_content = content.strip()
        if clean_content.startswith("[系统：工具 "):
            continue
        if role == "user":
            pending_user = {"role": "user", "content": clean_content}
            continue
        if pending_user is None:
            continue
        turns.append((pending_user, {"role": "assistant", "content": clean_content}))
        pending_user = None

    flattened: list[dict[str, str]] = []
    for user_message, assistant_message in turns[-max_turns:]:
        flattened.extend((user_message, assistant_message))
    return flattened
