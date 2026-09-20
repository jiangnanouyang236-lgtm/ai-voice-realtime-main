from __future__ import annotations

from dataclasses import dataclass
import logging
import random
import re
import time
from typing import Optional
import uuid

from llm.agent_runtime import (
    AgentContext,
    AgentResult,
    AnswerNormalizer,
    BaseAgent,
)

logger = logging.getLogger(__name__)

COGNITIVE_AGENT_ID = "cognitive_screening_agent"
REPORT_VERSION = "1.0"


@dataclass(frozen=True)
class ScreeningQuestion:
    id: str
    dimension: str
    title: str
    question: str


QUESTIONS: list[ScreeningQuestion] = [
    ScreeningQuestion(
        id="recent_memory",
        dimension="近期记忆",
        title="近期事件遗忘",
        question="是否经常忘记刚发生的事，比如半小时前放下的物品位置，或者刚吃过的食物？",
    ),
    ScreeningQuestion(
        id="repeat_question",
        dimension="记忆力",
        title="重复提问",
        question="对同一个问题，是否会反复询问，比如家人已经解释过了但仍然不记得？",
    ),
    ScreeningQuestion(
        id="word_finding",
        dimension="语言表达",
        title="词汇寻找困难",
        question="说话时是否频繁卡壳，找不到合适的词语？",
    ),
]

SCORES = {
    "经常": 0,
    "偶尔": 1,
    "从不": 2,
}

ENTRY_PATTERNS = [
    r"认知.{0,4}(检测|筛查|早筛|初筛|评估|测试)",
    r"(记忆力|记忆).{0,4}(检测|筛查|早筛|初筛|评估|测试)",
    r"测.{0,4}(认知|记忆)",
    r"(做|来|进行).{0,4}(认知|记忆).{0,4}(筛|测|评估)",
    r"(筛|测|评估).{0,4}(认知|记忆)",
    r"阿尔茨海默.{0,4}(筛查|早筛|初筛|检测|测试|评估)",
    r"老年痴呆.{0,4}(筛查|早筛|初筛|检测|测试|评估)",
]

CONCERN_PATTERNS = [
    r"(最近|这阵子|这段时间|近来)?我?.{0,6}(感觉|觉得|发现)?.{0,6}(记忆|记性|脑子).{0,8}(跟不上|不好|不太好|变差|下降|不行|退步|老忘|总忘|容易忘)",
    r"(最近|这阵子|这段时间|近来).{0,8}(老是|总是|经常|常常|容易).{0,6}(忘事|忘东西|记不住)",
    r"(记忆|记性).{0,6}(有点|有些|越来越|明显)?.{0,6}(差|差了|不行|不好|下降|退步)",
]

INFO_QUERY_PATTERNS = [
    r"(查|搜索|了解|介绍|讲讲|说说).{0,8}(认知|记忆|量表|筛查|早筛|初筛)",
    r"(认知|记忆|筛查|早筛|初筛|量表).{0,8}(是什么|有哪些|资料|方法|怎么做)",
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

FREQUENT_WORDS = [
    "经常",
    "总是",
    "老是",
    "一直",
    "很频繁",
    "频繁",
    "常常",
]

OCCASIONAL_WORDS = [
    "偶尔",
    "有时候",
    "有时",
    "偶然",
    "不太多",
    "不经常",
    "有一点",
    "有点",
    "少数",
]

NEVER_WORDS = [
    "从不",
    "没有",
    "没",
    "不会",
    "基本没有",
    "很少",
    "几乎没有",
    "不存在",
]

CONFIRM_WORDS = [
    "开始",
    "好",
    "好的",
    "可以",
    "做一下",
    "测一下",
    "来吧",
    "现在",
    "嗯",
]

DECLINE_WORDS = [
    "不用",
    "先不用",
    "不用了",
    "不要",
    "不好",
    "暂时不",
    "不做",
    "不测",
    "算了",
    "下次",
    "等会",
    "等等",
]


def is_cognitive_entry(text: str) -> bool:
    normalized = _normalize_text(text)
    if any(re.search(pattern, normalized) for pattern in INFO_QUERY_PATTERNS):
        return False
    return _is_direct_screening_request(normalized) or _is_memory_concern(normalized)


def _is_direct_screening_request(normalized_text: str) -> bool:
    return any(re.search(pattern, normalized_text) for pattern in ENTRY_PATTERNS)


def _is_memory_concern(normalized_text: str) -> bool:
    return any(re.search(pattern, normalized_text) for pattern in CONCERN_PATTERNS)


def _is_confirmation_yes(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in CONFIRM_WORDS)


def _is_confirmation_no(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in DECLINE_WORDS)


def is_explicit_exit(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in EXIT_WORDS)


def start_cognitive_screening() -> tuple[dict, str]:
    question_order = _new_question_order()
    state = {
        "agent_id": COGNITIVE_AGENT_ID,
        "current_index": 0,
        "question_order": question_order,
        "answers": [],
    }
    return state, _format_question(
        "好的，那我们来做个小游戏吧，一共三题。",
        question_order=question_order,
    )


def start_cognitive_confirmation() -> tuple[dict, str]:
    return (
        {
            "agent_id": COGNITIVE_AGENT_ID,
            "awaiting_confirmation": True,
        },
        "听起来你最近有些担心记忆状态。要不要一起做个三题小游戏？",
    )


async def continue_cognitive_screening(
    state: dict,
    user_text: str,
    *,
    llm_normalizer: AnswerNormalizer | None = None,
    context: AgentContext | None = None,
) -> tuple[dict | None, str]:
    if is_explicit_exit(user_text):
        return None, "好的，已退出小游戏，我们回到正常聊天。"

    answer = normalize_answer_by_rule(user_text)
    if answer is None and llm_normalizer is not None:
        answer = await llm_normalizer(user_text)
        if answer:
            logger.info("认知检测答案由 LLM 归一: %r -> %s", user_text, answer)

    if answer is None:
        return state, "这题请回答经常、偶尔或从不。想结束可以说退出。"

    current_index = int(state.get("current_index", 0))
    question_order = _question_order_from_state(state)
    if current_index >= len(question_order):
        return None, _build_summary(state)

    question = QUESTIONS[question_order[current_index]]
    answers = list(state.get("answers") or [])
    answers.append({
        "question_id": question.id,
        "dimension": question.dimension,
        "title": question.title,
        "question": question.question,
        "answer": answer,
        "score": SCORES[answer],
    })

    next_state = {
        "agent_id": COGNITIVE_AGENT_ID,
        "current_index": current_index + 1,
        "question_order": question_order,
        "answers": answers,
    }

    if next_state["current_index"] >= len(question_order):
        summary = _build_summary(next_state)
        if context is not None:
            report = build_cognitive_report(next_state, context, summary=summary)
            scheduled = context.fire_and_forget_tool("cognitive_report.publish", report)
            logger.info(
                "认知检测报告已生成: report_id=%s robot_id=%s scheduled=%s",
                report["report_id"],
                report.get("robot_id") or "-",
                scheduled,
            )
        return None, summary

    return next_state, _format_question("", next_state["current_index"], question_order=question_order)


class CognitiveScreeningAgent(BaseAgent):
    id = COGNITIVE_AGENT_ID
    name = "认知检测"
    description = "三题式语音认知早筛 Demo，用于快速体验记忆和表达能力问答。"
    trigger_examples = (
        "我要进行认知早筛",
        "做一下记忆力评估",
        "帮我做认知检测",
        "我感觉最近记忆有点跟不上了",
    )
    allowed_tools = ("cognitive_report.publish",)

    def can_enter(self, text: str, context: AgentContext) -> bool:
        return is_cognitive_entry(text)

    async def start(self, text: str, context: AgentContext) -> AgentResult:
        normalized = _normalize_text(text)
        if _is_direct_screening_request(normalized):
            state, response = start_cognitive_screening()
        else:
            state, response = start_cognitive_confirmation()
        return AgentResult(text=response, state=state, finished=False)

    async def handle(
        self,
        text: str,
        state: dict,
        context: AgentContext,
    ) -> AgentResult:
        if state.get("awaiting_confirmation"):
            if _is_confirmation_no(text):
                return AgentResult(
                    text="好的，那我们先不做小游戏，继续正常聊天。",
                    state=None,
                    finished=True,
                )
            if _is_confirmation_yes(text):
                next_state, response = start_cognitive_screening()
                return AgentResult(text=response, state=next_state, finished=False)
            return AgentResult(
                text="如果想开始，请说开始；如果暂时不做，可以说先不用。",
                state=state,
                finished=False,
            )

        next_state, response = await continue_cognitive_screening(
            state,
            text,
            llm_normalizer=context.llm_normalizer,
            context=context,
        )
        return AgentResult(
            text=response,
            state=next_state,
            finished=next_state is None,
        )


def create_agent() -> BaseAgent:
    return CognitiveScreeningAgent()


def normalize_answer_by_rule(text: str) -> Optional[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return None

    if "没那么" in normalized or "没有那么" in normalized or "不怎么" in normalized:
        return "偶尔"
    if any(word in normalized for word in NEVER_WORDS):
        return "从不"
    if any(word in normalized for word in OCCASIONAL_WORDS):
        return "偶尔"
    if any(word in normalized for word in FREQUENT_WORDS):
        return "经常"
    return None


def build_cognitive_report(state: dict, context: AgentContext, *, summary: str) -> dict:
    answers = list(state.get("answers") or [])
    total_score = _total_score(answers)
    completed_at = int(time.time() * 1000)
    return {
        "report_id": f"cog-{uuid.uuid4().hex}",
        "robot_id": context.robot_id or "",
        "session_id": context.session_id,
        "agent_id": COGNITIVE_AGENT_ID,
        "version": REPORT_VERSION,
        "completed_at": completed_at,
        "total_score": total_score,
        "max_score": len(QUESTIONS) * max(SCORES.values()),
        "risk_level": _risk_level(total_score),
        "risk_text": _risk_level_text(total_score),
        "summary": summary,
        "answers": answers,
    }


def _format_question(
    prefix: str = "",
    index: int = 0,
    *,
    question_order: list[int] | None = None,
) -> str:
    order = question_order or list(range(len(QUESTIONS)))
    question = QUESTIONS[order[index]]
    return (
        f"{prefix}第 {index + 1} 题：{question.question}"
        "请回答经常、偶尔或从不。"
    )


def _build_summary(state: dict) -> str:
    answers = list(state.get("answers") or [])
    total_score = _total_score(answers)
    answer_summary = "；".join(
        f"{item['title']}：{item['answer']}"
        for item in answers
    )
    level_text = _risk_level_text(total_score)
    return (
        f"小游戏完成啦，本次总分 {total_score} 分。"
        f"{level_text}"
        f"我记录到：{answer_summary}。"
        "这只是一次日常互动参考，不代表医学诊断。"
    )


def _risk_level_text(total_score: int) -> str:
    if total_score >= 5:
        return "目前没有明显需要担心的信号。"
    if total_score >= 3:
        return "有一些情况可以继续观察。"
    return "建议和家人一起留意，必要时咨询专业人士。"


def _risk_level(total_score: int) -> str:
    if total_score >= 5:
        return "normal"
    if total_score >= 3:
        return "observe"
    return "attention"


def _total_score(answers: list[dict]) -> int:
    return sum(int(item.get("score", 0)) for item in answers)


def _new_question_order() -> list[int]:
    order = list(range(len(QUESTIONS)))
    random.shuffle(order)
    return order


def _question_order_from_state(state: dict) -> list[int]:
    raw_order = state.get("question_order")
    if isinstance(raw_order, list):
        order = [
            int(item)
            for item in raw_order
            if isinstance(item, int) and 0 <= int(item) < len(QUESTIONS)
        ]
        if len(order) == len(QUESTIONS) and len(set(order)) == len(QUESTIONS):
            return order
    return list(range(len(QUESTIONS)))


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())
