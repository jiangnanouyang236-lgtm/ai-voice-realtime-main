from __future__ import annotations

import logging
import random
import re

from llm.agent_runtime import AgentContext, AgentResult, AgentStreamEvent, BaseAgent

logger = logging.getLogger(__name__)

STORY_AGENT_ID = "interactive_story_agent"

ENTRY_PATTERNS = [
    r"(想|要|来|玩|开始|进入).{0,6}(故事接龙|故事续写|互动故事|故事游戏|剧情接龙|剧情续写)",
    r"(故事接龙|故事续写|互动故事|故事游戏|剧情接龙|剧情续写).{0,6}(玩|开始|来|一下|模式)?",
    r"(想玩|要玩|来玩).{0,4}(修仙|侦探|科幻|童话|武侠|冒险).{0,4}(故事接龙|故事续写|互动故事|故事游戏)",
]

EXIT_WORDS = [
    "不玩了",
    "退出故事",
    "结束故事",
    "不讲了",
    "别讲了",
    "回到正常聊天",
    "退出这个故事",
]

SHORT_EXIT_COMMANDS = [
    "退出",
    "退出吧",
    "先退出",
    "退出了",
    "结束",
    "取消",
]

HELP_WORDS = [
    "怎么玩",
    "怎么退出",
    "怎么结束",
    "我该说什么",
    "不知道怎么",
    "不知道说什么",
    "规则",
    "帮助",
    "提示",
]

STYLE_HINTS = {
    "修仙": "修仙",
    "侦探": "侦探",
    "科幻": "科幻",
    "童话": "童话",
    "武侠": "武侠",
    "冒险": "冒险",
}

DEFAULT_STYLES = ["修仙", "侦探", "科幻", "童话", "冒险"]

FALLBACK_OPENINGS = {
    "修仙": "你是刚下山的小修士，山脚有集市和黑雾森林。你去哪边？",
    "侦探": "雨夜里，你收到一封无署名信，约你去旧剧院。你去吗？",
    "科幻": "飞船忽然停电，窗外有一颗蓝色星球在闪。你先查哪里？",
    "童话": "一只会说话的猫拦住你，说城堡丢了月亮。你跟它走吗？",
    "冒险": "你在海边捡到一张湿地图，上面标着红叉。你要出发吗？",
}


class InteractiveStoryAgent(BaseAgent):
    id = STORY_AGENT_ID
    name = "故事接龙"
    description = "LLM 驱动的短句语音故事接龙 Agent，支持多轮剧情续写和明确退出。"
    trigger_examples = ("想玩故事接龙", "我想玩故事续写", "来个修仙故事接龙")

    def can_enter(self, text: str, context: AgentContext) -> bool:
        normalized = _normalize_text(text)
        return any(re.search(pattern, normalized) for pattern in ENTRY_PATTERNS)

    async def start(self, text: str, context: AgentContext) -> AgentResult:
        style = _detect_style(text)
        prompt = _build_start_prompt(text, style)
        response = await _ask_story_llm(context, prompt)
        if not response:
            response = FALLBACK_OPENINGS.get(style, random.choice(list(FALLBACK_OPENINGS.values())))

        return _story_result(
            text=f"{_story_entry_intro(style)}{response}",
            state=_start_state(style, response),
            finished=False,
        )

    async def start_stream(self, text: str, context: AgentContext):
        style = _detect_style(text)
        prompt = _build_start_prompt(text, style)
        yield AgentStreamEvent(text=_story_entry_intro(style))
        emitted_parts: list[str] = []
        async for chunk in _ask_story_llm_stream(context, prompt):
            emitted_parts.append(chunk)
            yield AgentStreamEvent(text=chunk)

        response = _clean_response("".join(emitted_parts))
        if not response:
            response = FALLBACK_OPENINGS.get(style, random.choice(list(FALLBACK_OPENINGS.values())))
            yield AgentStreamEvent(text=response)

        yield AgentStreamEvent(
            result=_story_result(text=response, state=_start_state(style, response), finished=False)
        )

    async def handle(
        self,
        text: str,
        state: dict,
        context: AgentContext,
    ) -> AgentResult:
        if _is_explicit_exit(text):
            return _story_result(
                text="好的，不讲故事了，我们回到正常聊天。",
                state=None,
                finished=True,
            )
        if _is_help_request(text):
            return _story_result(
                text=_story_help_text(),
                state=state,
                finished=False,
            )

        style = str(state.get("style") or "冒险")
        summary = str(state.get("summary") or "")
        recent = state.get("recent") or []
        turn_count = int(state.get("turn_count") or 0) + 1

        prompt = _build_continue_prompt(style, summary, recent, text)
        response = await _ask_story_llm(context, prompt)
        if not response:
            response = _fallback_continue(style, text)

        next_state = _continue_state(state, text, response, turn_count, recent, summary, style)
        return _story_result(text=response, state=next_state, finished=False)

    async def handle_stream(
        self,
        text: str,
        state: dict,
        context: AgentContext,
    ):
        if _is_explicit_exit(text):
            response = "好的，不讲故事了，我们回到正常聊天。"
            yield AgentStreamEvent(
                text=response,
                result=_story_result(text=response, state=None, finished=True),
            )
            return
        if _is_help_request(text):
            response = _story_help_text()
            yield AgentStreamEvent(
                text=response,
                result=_story_result(text=response, state=state, finished=False),
            )
            return

        style = str(state.get("style") or "冒险")
        summary = str(state.get("summary") or "")
        recent = state.get("recent") or []
        turn_count = int(state.get("turn_count") or 0) + 1
        prompt = _build_continue_prompt(style, summary, recent, text)

        emitted_parts: list[str] = []
        async for chunk in _ask_story_llm_stream(context, prompt):
            emitted_parts.append(chunk)
            yield AgentStreamEvent(text=chunk)

        response = _clean_response("".join(emitted_parts))
        if not response:
            response = _fallback_continue(style, text)
            yield AgentStreamEvent(text=response)

        next_state = _continue_state(state, text, response, turn_count, recent, summary, style)
        yield AgentStreamEvent(
            result=_story_result(text=response, state=next_state, finished=False)
        )


def _story_result(
    *,
    text: str,
    state: dict | None,
    finished: bool,
) -> AgentResult:
    return AgentResult(
        text=text,
        state=state,
        finished=finished,
        metadata={"persist_history": False},
    )


def _story_system_prompt() -> str:
    return (
        "你是一个语音故事接龙 Agent。"
        "只输出故事内容，不解释系统规则。"
        "每次回复 35 到 65 个中文字，最多两句话。"
        "接住用户的行动，推进一个小情节，结尾给用户一个自然的选择或开放问题。"
        "语气自然、有画面感，但不要长篇描写。"
        "如果用户想躺平、回家、反常选择，也要顺着编。"
    )


def _story_entry_intro(style: str) -> str:
    return (
        f"那我们来玩{style}故事接龙吧。"
        "你可以自由地说想做什么，想退出就说退出故事。"
    )


def _story_help_text() -> str:
    return (
        "你可以直接说你想做什么，比如往前走、打开门、问问旁边的人。"
        "我会接着你的选择续写。想结束就说退出故事。"
    )


def _build_start_prompt(text: str, style: str) -> list[dict[str, str]]:
    seed = random.choice(_style_opening_seeds(style))
    return [
        {
            "role": "system",
            "content": _story_system_prompt(),
        },
        {
            "role": "user",
            "content": (
                f"用户想开始一个{style}互动故事。"
                f"用户原话：{text}"
                f"参考种子：{seed}"
                "请直接给出故事开场，不要寒暄，不要解释玩法。"
            ),
        },
    ]


def _build_continue_prompt(
    style: str,
    summary: str,
    recent: list,
    user_text: str,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": _story_system_prompt(),
        },
        {
            "role": "user",
            "content": (
                f"故事类型：{style}\n"
                f"当前简短摘要：{summary}\n"
                f"最近片段：{_format_recent(recent)}\n"
                f"用户行动：{user_text}\n"
                "请继续故事。"
            ),
        },
    ]


def _start_state(style: str, response: str) -> dict:
    return {
        "agent_id": STORY_AGENT_ID,
        "style": style,
        "setting": response,
        "summary": response,
        "turn_count": 0,
        "recent": [{"assistant": response}],
    }


def _continue_state(
    state: dict,
    user_text: str,
    response: str,
    turn_count: int,
    recent: list,
    summary: str,
    style: str,
) -> dict:
    next_recent = _trim_recent([
        *recent,
        {"user": user_text, "assistant": response},
    ])
    return {
        "agent_id": STORY_AGENT_ID,
        "style": style,
        "setting": state.get("setting") or "",
        "summary": _compact_summary(summary, user_text, response),
        "turn_count": turn_count,
        "recent": next_recent,
    }


async def _ask_story_llm(
    context: AgentContext,
    messages: list[dict[str, str]],
) -> str | None:
    try:
        response = await context.ask_llm(
            messages,
            temperature=0.9,
            max_tokens=80,
        )
    except Exception as exc:
        logger.warning("互动故事 LLM 生成失败: %s", exc)
        return None
    return _clean_response(response)


async def _ask_story_llm_stream(
    context: AgentContext,
    messages: list[dict[str, str]],
):
    emitted_chars = 0
    try:
        async for raw_chunk in context.ask_llm_stream(
            messages,
            temperature=0.9,
            max_tokens=80,
        ):
            chunk = re.sub(r"\s+", " ", (raw_chunk or ""))
            if not chunk:
                continue
            remaining = 75 - emitted_chars
            if remaining <= 0:
                return
            text = chunk[:remaining]
            emitted_chars += len(text)
            if len(text) < len(chunk) or emitted_chars >= 75:
                text = text.rstrip("，。！？,.!?") + "。"
            if text:
                yield text
            if emitted_chars >= 75:
                return
    except Exception as exc:
        logger.warning("互动故事 LLM 流式生成失败: %s", exc)


def _clean_response(text: str | None) -> str | None:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return None
    # 语音场景宁可截短，也不要一次输出变成小作文。
    if len(cleaned) > 75:
        cleaned = cleaned[:75].rstrip("，。！？,.!?") + "。"
    return cleaned


def _detect_style(text: str) -> str:
    normalized = _normalize_text(text)
    for keyword, style in STYLE_HINTS.items():
        if keyword in normalized:
            return style
    return random.choice(DEFAULT_STYLES)


def _style_opening_seeds(style: str) -> list[str]:
    seeds = {
        "修仙": [
            "主角刚下山，师父留下一封奇怪的信。",
            "主角想回山养老，但山门突然关上了。",
        ],
        "侦探": [
            "雨夜旧剧院出现一封无署名信。",
            "咖啡馆里有人把一枚钥匙推到主角面前。",
        ],
        "科幻": [
            "飞船停电，窗外出现未知蓝色星球。",
            "家用机器人突然说它记起了未来。",
        ],
        "童话": [
            "会说话的猫说城堡丢了月亮。",
            "口袋里的纽扣变成了一扇小门。",
        ],
        "冒险": [
            "海边捡到一张湿地图，上面有红叉。",
            "山洞里传来钟声，入口却写着别回头。",
        ],
    }
    return seeds.get(style, seeds["冒险"])


def _fallback_continue(style: str, user_text: str) -> str:
    action = (user_text or "继续").strip()
    if style == "修仙":
        return f"你决定{action}，师父的纸鹤却追了上来。它说山下有缘分，你要听吗？"
    if style == "侦探":
        return f"你选择{action}，线索却指向一个锁住的房间。你先找钥匙吗？"
    if style == "科幻":
        return f"你准备{action}，屏幕亮起一行倒计时。你要重启系统吗？"
    return f"你决定{action}，前方忽然出现新的岔路。你往左还是往右？"


def _format_recent(recent: list) -> str:
    if not recent:
        return "无"
    parts = []
    for item in recent[-3:]:
        user = item.get("user")
        assistant = item.get("assistant")
        if user:
            parts.append(f"用户：{user}")
        if assistant:
            parts.append(f"故事：{assistant}")
    return "；".join(parts) if parts else "无"


def _trim_recent(recent: list) -> list:
    return recent[-4:]


def _compact_summary(summary: str, user_text: str, response: str) -> str:
    text = f"{summary} 用户选择：{user_text}。剧情：{response}"
    return text[-220:]


def _is_explicit_exit(text: str) -> bool:
    normalized = _normalize_text(text)
    command = _normalize_command_text(text)
    if any(word in normalized for word in EXIT_WORDS):
        return True
    return command in SHORT_EXIT_COMMANDS


def _is_help_request(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in HELP_WORDS)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _normalize_command_text(text: str) -> str:
    return re.sub(r"[\s，。！？,.!?、~～]+", "", (text or "").strip().lower())


def create_agent() -> BaseAgent:
    return InteractiveStoryAgent()
