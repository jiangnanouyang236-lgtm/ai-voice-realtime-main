from __future__ import annotations

import logging
import re

from llm.agent_runtime import AgentContext, AgentResult, BaseAgent

logger = logging.getLogger(__name__)

VISITOR_AGENT_ID = "visitor_registration_agent"

VISITOR_ENTRY_PATTERNS = [
    r"访客.{0,4}(登记|注册|记录)",
    r"(登记|注册|记录).{0,4}访客",
    r"访问.{0,4}(登记|注册|记录)",
    r"(登记|注册|记录).{0,4}访问",
    r"(有人|客人|访客).{0,4}(来访|来了|要来)",
    r"来客.{0,4}(登记|注册|记录)",
    r"来访.{0,4}(登记|注册|记录)",
]

EXIT_WORDS = [
    "退出",
    "不测了",
    "不做了",
    "结束",
    "取消",
    "停一下",
    "先不测",
]

VISITOR_FIELDS = [
    {
        "key": "visitor_name",
        "label": "访客姓名",
        "question": "访客叫什么名字？",
    },
    {
        "key": "phone",
        "label": "联系电话",
        "question": "联系电话是多少？",
    },
    {
        "key": "host_name",
        "label": "被访人",
        "question": "访客要找谁？",
    },
]


def is_visitor_entry(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(re.search(pattern, normalized) for pattern in VISITOR_ENTRY_PATTERNS)


def is_explicit_exit(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in EXIT_WORDS)


def start_visitor_registration() -> tuple[dict, str]:
    state = {
        "agent_id": VISITOR_AGENT_ID,
        "current_index": 0,
        "fields": {},
    }
    return state, f"好的，开始访客登记。{VISITOR_FIELDS[0]['question']}"


def continue_visitor_registration(
    state: dict,
    user_text: str,
) -> tuple[dict | None, str]:
    if is_explicit_exit(user_text):
        return None, "好的，已退出访客登记，我们回到正常聊天。"

    current_index = int(state.get("current_index", 0))
    fields = dict(state.get("fields") or {})
    if current_index >= len(VISITOR_FIELDS):
        return None, _build_visitor_summary(fields)

    value = (user_text or "").strip()
    if not value:
        return state, VISITOR_FIELDS[current_index]["question"]

    current_field = VISITOR_FIELDS[current_index]
    if current_field["key"] == "phone":
        digits = re.sub(r"\D+", "", value)
        if len(digits) < 7:
            return state, "联系电话好像不完整，请再说一遍。"
        value = digits

    fields[current_field["key"]] = value
    next_index = current_index + 1
    if next_index >= len(VISITOR_FIELDS):
        return None, _build_visitor_summary(fields)

    next_state = {
        "agent_id": VISITOR_AGENT_ID,
        "current_index": next_index,
        "fields": fields,
    }
    return next_state, VISITOR_FIELDS[next_index]["question"]


async def submit_visitor_registration(
    fields: dict,
    context: AgentContext,
) -> str:
    result = await context.call_tool("visitor.submit_registration", fields)
    logger.info(
        "访客登记 FunctionCall 完成: ok=%s, message=%s, data=%s",
        result.ok,
        result.message,
        result.data,
    )
    if result.ok:
        return _build_visitor_summary(fields, submitted=True)
    return (
        "访客信息已记录，但提交暂时失败。"
        f"{_build_visitor_summary(fields, submitted=False)}"
    )


class VisitorRegistrationAgent(BaseAgent):
    id = VISITOR_AGENT_ID
    name = "访客登记"
    description = "按语音流程采集访客姓名、电话和被访人，并演示 Agent 内部 FunctionCall。"
    trigger_examples = ("我要登记访客", "有人来访，帮我登记", "做一下访客登记")
    allowed_tools = ("visitor.submit_registration",)
    allowed_tools = ("visitor.submit_registration",)

    def can_enter(self, text: str, context: AgentContext) -> bool:
        return is_visitor_entry(text)

    async def start(self, text: str, context: AgentContext) -> AgentResult:
        state, response = start_visitor_registration()
        return AgentResult(text=response, state=state, finished=False)

    async def handle(
        self,
        text: str,
        state: dict,
        context: AgentContext,
    ) -> AgentResult:
        next_state, response = continue_visitor_registration(state, text)
        if next_state is None and not is_explicit_exit(text):
            fields = _visitor_fields_from_finished_state(state, text)
            if fields:
                response = await submit_visitor_registration(fields, context)
        return AgentResult(
            text=response,
            state=next_state,
            finished=next_state is None,
        )


def create_agent() -> BaseAgent:
    return VisitorRegistrationAgent()


def _visitor_fields_from_finished_state(state: dict, user_text: str) -> dict:
    current_index = int(state.get("current_index", 0))
    if current_index >= len(VISITOR_FIELDS):
        return dict(state.get("fields") or {})

    fields = dict(state.get("fields") or {})
    field = VISITOR_FIELDS[current_index]
    value = (user_text or "").strip()
    if field["key"] == "phone":
        value = re.sub(r"\D+", "", value)
    if value:
        fields[field["key"]] = value
    if all(item["key"] in fields for item in VISITOR_FIELDS):
        return fields
    return {}


def _build_visitor_summary(fields: dict, *, submitted: bool = False) -> str:
    visitor_name = fields.get("visitor_name", "未填写")
    phone = fields.get("phone", "未填写")
    host_name = fields.get("host_name", "未填写")
    prefix = "访客登记完成，已提交。" if submitted else "访客登记完成。"
    return (
        f"{prefix}"
        f"访客：{visitor_name}。"
        f"电话：{phone}。"
        f"被访人：{host_name}。"
    )


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())
