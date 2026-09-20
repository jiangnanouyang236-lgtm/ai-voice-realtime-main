"""集中管理语音快速回复，并提供有界、线程安全的防重复抽取。"""

from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import random
import threading


MIN_QUICK_REPLY_POOL_SIZE = 16
_MAX_SELECTOR_STATES = 2048
SINGING_PROGRESS_TEXT = "好～我准备一下。"


def _combine(stems: tuple[str, ...], tails: tuple[str, ...]) -> tuple[str, ...]:
    phrases = tuple(dict.fromkeys(f"{stem}{tail}" for stem in stems for tail in tails))
    if len(phrases) < MIN_QUICK_REPLY_POOL_SIZE:
        raise ValueError(
            f"快速回复语料不足: {len(phrases)} < {MIN_QUICK_REPLY_POOL_SIZE}"
        )
    return phrases


def _short_pool(
    pool_key: str,
    phrases: tuple[str, ...],
    *,
    max_visible_chars: int,
) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(phrases))
    if len(unique) < MIN_QUICK_REPLY_POOL_SIZE:
        raise ValueError(
            f"快速回复语料不足: {pool_key} {len(unique)} < {MIN_QUICK_REPLY_POOL_SIZE}"
        )
    too_long = [
        phrase
        for phrase in unique
        if len(phrase.replace("[EXIT]", "")) > max_visible_chars
    ]
    if too_long:
        raise ValueError(f"快速回复过长: {pool_key} {too_long[0]}")
    return unique


QUICK_REPLY_POOLS = {
    "client.startup_ready": _short_pool(
        "client.startup_ready",
        (
            "我准备好了。", "准备好了。", "我在。", "已经就绪。",
            "可以开始了。", "随时可以。", "我上线啦。", "启动完成。",
            "状态就绪。", "已经准备好。", "我来啦。", "可以用了。",
            "我在这儿。", "准备完成。", "一切就绪。", "现在可以啦。",
        ),
        max_visible_chars=7,
    ),
    "client.wake_idle": _short_pool(
        "client.wake_idle",
        (
            "我在，你说。", "嗯，我在。", "我听着。", "在呢。",
            "你说吧。", "嗯，你说。", "我来了。", "好，我在。",
            "听着呢。", "我在这儿。", "说吧。", "嗯，听着呢。",
            "好，你说。", "我醒啦。", "来了。", "我正听着。",
        ),
        max_visible_chars=7,
    ),
    "client.wake_interrupt": _short_pool(
        "client.wake_interrupt",
        (
            "好，我停下了。", "嗯，你说。", "我在听。", "好，我听着。",
            "已经停下了。", "嗯，我不说了。", "好，先听你说。", "我停了，你说。",
            "收到，我听着。", "好，现在听你。", "我先停一下。", "停下了。",
            "好，你说吧。", "嗯，听你说。", "我暂停了。", "收到，你说。",
        ),
        max_visible_chars=8,
    ),
    "client.sleep_exit": _short_pool(
        "client.sleep_exit",
        (
            "好，我待机。", "我先休息啦。", "好，待会儿见。", "我先安静啦。",
            "嗯，我退下啦。", "我先待命。", "好，我先走啦。", "我先不打扰。",
            "那我休息啦。", "好，先这样。", "我先离开啦。", "嗯，回头见。",
            "好，稍后见。", "我先歇会儿。", "好的，我待机。", "那我先走啦。",
        ),
        max_visible_chars=8,
    ),
    "session.exit": _short_pool(
        "session.exit",
        tuple(
            f"{phrase}[EXIT]"
            for phrase in (
                "好，我先走啦。", "那就先这样。", "好，待会儿见。", "我先休息啦。",
                "嗯，回头见。", "我先退下啦。", "好，我待机。", "那我先走啦。",
                "好，稍后见。", "我先离开啦。", "嗯，先这样。", "好，我先安静。",
                "那就回头见。", "我先不打扰。", "好，我去休息。", "嗯，我先走啦。",
            )
        ),
        max_visible_chars=8,
    ),
    "tool.websearch": _short_pool(
        "tool.websearch",
        (
            "我查一下。", "我去看看。", "我来搜索。", "稍等，我查查。",
            "我确认一下。", "我找一下。", "我马上查。", "好，我来查。",
            "我去核实。", "我看看资料。", "我查查最新的。", "我去确认。",
            "好，稍等。", "我搜一下。", "我来找找。", "正在查询。",
        ),
        max_visible_chars=8,
    ),
    "tool.weather": _short_pool(
        "tool.weather",
        (
            "我看看天气。", "我查下天气。", "稍等，我看看。", "好，我来查。",
            "我确认一下。", "我看看预报。", "正在查天气。", "我马上看看。",
            "好，查一下。", "我去看天气。", "我来看看。", "稍等一下。",
            "我查查预报。", "我去确认。", "好，我看看。", "天气我来查。",
        ),
        max_visible_chars=8,
    ),
    "tool.task": _short_pool(
        "tool.task",
        (
            "我来设置。", "好，我安排。", "我处理一下。", "我来确认。",
            "稍等，我设置。", "好，我来办。", "我马上安排。", "我来记下。",
            "正在设置。", "我处理提醒。", "好，交给我。", "我来安排。",
            "稍等一下。", "我确认提醒。", "我马上处理。", "我来记录。",
        ),
        max_visible_chars=8,
    ),
    "tool.utils": _short_pool(
        "tool.utils",
        (
            "我确认一下。", "我看看。", "我查一下。", "稍等，我看看。",
            "好，我确认。", "我马上看。", "我来核实。", "正在确认。",
            "我去看看。", "我来查看。", "好，稍等。", "我检查一下。",
            "我找一下。", "我马上确认。", "稍等一下。", "我来查查。",
        ),
        max_visible_chars=8,
    ),
    "tool.generic": _short_pool(
        "tool.generic",
        (
            "我来处理。", "好，我来办。", "我确认一下。", "稍等一下。",
            "我马上处理。", "好，交给我。", "我来看看。", "正在处理。",
            "我来安排。", "好，我开始。", "我先确认。", "我马上看。",
            "我来解决。", "稍等，我来。", "好，我处理。", "我去看看。",
        ),
        max_visible_chars=8,
    ),
    "tool.complex": _short_pool(
        "tool.complex",
        (
            "我先梳理一下。", "这要多走几步。", "我分步处理。", "稍等，我来处理。",
            "我先理清步骤。", "我来逐步完成。", "这需要一点时间。", "我先整理一下。",
            "好，我分步来。", "我开始处理。", "我来协调一下。", "我先分析一下。",
            "稍等，我来梳理。", "我会逐步处理。", "我先确认步骤。", "好，我认真处理。",
        ),
        max_visible_chars=9,
    ),
}


_ROBOT_ACTION_VARIANTS = {
    "generic": (
        "我来处理",
        "我这就处理",
        "交给我吧",
        "我来操作",
        "现在开始处理",
        "我马上处理",
        "我来执行",
        "这就为你处理",
        "我现在开始",
        "我来完成",
    ),
    "move_forward": (
        "我往前移动",
        "我这就往前走",
        "现在往前移动",
        "我来向前走",
        "我马上往前",
        "这就向前移动",
        "我开始往前走",
        "我来往前移动",
        "现在向前走",
        "我这就前进",
    ),
    "move_backward": (
        "我往后移动",
        "我这就往后走",
        "现在往后移动",
        "我来向后走",
        "我马上往后",
        "这就向后移动",
        "我开始往后走",
        "我来往后移动",
        "现在向后走",
        "我这就后退",
    ),
    "turn_left": (
        "我向左转",
        "我这就左转",
        "现在向左转",
        "我来往左转",
        "我马上左转",
        "这就转向左边",
        "我开始向左转",
        "我来左转",
        "现在转向左边",
        "我这就往左转",
    ),
    "turn_right": (
        "我向右转",
        "我这就右转",
        "现在向右转",
        "我来往右转",
        "我马上右转",
        "这就转向右边",
        "我开始向右转",
        "我来右转",
        "现在转向右边",
        "我这就往右转",
    ),
    "stop": (
        "我停下来",
        "我这就停",
        "现在停止动作",
        "我来暂停",
        "我马上停下",
        "这就停止",
        "我不再继续动作",
        "我现在停下来",
        "我来结束当前动作",
        "我这就暂停",
    ),
    "greet": (
        "我来打个招呼",
        "我这就问个好",
        "现在和你打招呼",
        "我来向你问好",
        "我马上打招呼",
        "这就和你问好",
        "我开始打招呼",
        "我来问个好",
        "现在向你问好",
        "我这就打招呼",
    ),
    "handshake": (
        "我来和你握手",
        "我这就握手",
        "现在和你握手",
        "我来握个手",
        "我马上握手",
        "这就和你握手",
        "我开始握手",
        "我来与你握手",
        "现在握个手",
        "我这就和你握手",
    ),
    "cheer": (
        "我来为你喝彩",
        "我这就欢呼",
        "现在为你喝彩",
        "我来欢呼一下",
        "我马上喝彩",
        "这就为你欢呼",
        "我开始喝彩",
        "我来为你欢呼",
        "现在欢呼一下",
        "我这就喝彩",
    ),
    "recharge": (
        "我去充电",
        "我这就去充电",
        "现在前往充电",
        "我来返回充电",
        "我马上去充电",
        "这就前往充电",
        "我开始返回充电",
        "我来准备充电",
        "现在去补充电量",
        "我这就回去充电",
    ),
    "video_call": (
        "我来呼叫视频电话",
        "我这就发起视频通话",
        "现在呼叫视频电话",
        "我来发起视频通话",
        "我马上呼叫视频电话",
        "这就开始视频通话",
        "我开始呼叫视频电话",
        "我来拨打视频电话",
        "现在发起视频通话",
        "我这就呼叫视频电话",
    ),
}

_ROBOT_PREFIXES = (
    "好的，",
    "收到，",
    "没问题，",
    "好，",
    "可以，",
    "明白，",
    "行，",
    "好呀，",
    "知道了，",
    "嗯，",
)

_ROBOT_ENDINGS = ("。", "！")


@lru_cache(maxsize=None)
def robot_action_phrase_pool(action: str) -> tuple[str, ...]:
    variants = _ROBOT_ACTION_VARIANTS.get(action, _ROBOT_ACTION_VARIANTS["generic"])
    stems = tuple(
        f"{prefix}{variant}" for prefix in _ROBOT_PREFIXES for variant in variants
    )
    return _combine(stems, _ROBOT_ENDINGS)


class QuickReplySelector:
    """每个会话、每个语料池一轮用完前不重复，且限制驻留状态数量。"""

    def __init__(
        self,
        *,
        max_states: int = _MAX_SELECTOR_STATES,
        rng: random.Random | None = None,
    ):
        self._max_states = max(1, max_states)
        self._rng = rng or random.SystemRandom()
        self._lock = threading.Lock()
        self._bags: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        self._last: dict[tuple[str, str], str] = {}

    def pick(
        self, pool_key: str, phrases: tuple[str, ...], session_id: str | None = None
    ) -> str:
        if not phrases:
            raise ValueError(f"快速回复语料池为空: {pool_key}")
        state_key = (str(session_id or "__global__"), pool_key)
        with self._lock:
            bag = self._bags.get(state_key)
            if not bag:
                bag = list(phrases)
                self._rng.shuffle(bag)
                previous = self._last.get(state_key)
                if previous and len(bag) > 1 and bag[-1] == previous:
                    bag[-1], bag[-2] = bag[-2], bag[-1]
                self._bags[state_key] = bag
            else:
                self._bags.move_to_end(state_key)
            phrase = bag.pop()
            self._last[state_key] = phrase
            while len(self._bags) > self._max_states:
                expired_key, _ = self._bags.popitem(last=False)
                self._last.pop(expired_key, None)
            return phrase


_SELECTOR = QuickReplySelector()


def quick_reply_pool(pool_key: str) -> tuple[str, ...]:
    try:
        return QUICK_REPLY_POOLS[pool_key]
    except KeyError as exc:
        raise ValueError(f"未知快速回复语料池: {pool_key}") from exc


def pick_quick_reply(pool_key: str, session_id: str | None = None) -> str:
    phrases = quick_reply_pool(pool_key)
    if session_id is None:
        return phrases[0]
    return _SELECTOR.pick(pool_key, phrases, session_id)


def pick_robot_action_reply(action: str, session_id: str | None = None) -> str:
    phrases = robot_action_phrase_pool(action)
    if session_id is None:
        return phrases[0]
    return _SELECTOR.pick(f"robot.{action}", phrases, session_id)
