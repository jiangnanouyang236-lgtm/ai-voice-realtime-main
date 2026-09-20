from __future__ import annotations

from dataclasses import dataclass
import re

from voice_quick_replies import (
    SINGING_PROGRESS_TEXT,
    pick_quick_reply,
    pick_robot_action_reply,
)


@dataclass(frozen=True)
class ToolLatencyRoute:
    kind: str
    progress_text: str | None = None
    tool_prefix: str | None = None
    selected_tool_name: str | None = None
    require_tool_call: bool = False
    model_text: str | None = None
    source: str | None = None
    category: str | None = None


# 所有具有运行态 bot_id 的 Bot 使用同一套路由策略；工具能力仍由各 Bot 的绑定决定。
ROUTER_TOOL_CATEGORIES = {"websearch", "robot", "singing", "task", "utils", "complex"}

ROUTER_CATEGORY_TOOL_PREFIX = {
    "websearch": "websearch.",
    "robot": "robot_remote.",
    "singing": "singing_remote.",
    "task": "robots_task_service.",
    "utils": "utils_remote.",
}

ROUTER_CATEGORY_PROGRESS_POOL = {
    "websearch": "tool.websearch",
    "robot": "robot.generic",
    "task": "tool.task",
    "utils": "tool.utils",
}

ROUTER_DECISION_CODE_TO_DECISION = {
    "e": ("exit", None),
    "c": ("chat", None),
    "v": ("chat", "vision"),
    "w": ("tool", "websearch"),
    "r": ("tool", "robot"),
    "s": ("tool", "singing"),
    "t": ("tool", "task"),
    "u": ("tool", "utils"),
    "x": ("tool", "complex"),
}
WHOLE_SESSION_EXIT_COMMANDS = (
    "退出",
    "退出吧",
    "再见",
    "不聊了",
    "结束对话",
    "退下吧",
    "你先休息吧",
    "cya",
)
NON_WHOLE_SESSION_EXIT_PHRASES = (
    "退出这个话题",
    "换个话题",
    "退出小游戏",
    "退出故事",
    "退出登记",
    "退出这个故事",
)
EXIT_TRAILING_PUNCTUATION = "。！？!?.,，；;：:~～…"

LOW_TOOL_RISK_CHAT_KEYWORDS = (
    "你好",
    "您好",
    "早上好",
    "中午好",
    "下午好",
    "晚上好",
    "你今天怎么样",
    "你怎么样",
    "你好吗",
    "陪我聊",
    "聊聊天",
    "讲个笑话",
    "讲个故事",
    "讲故事",
    "说个故事",
    "谢谢",
    "辛苦了",
)
NARRATIVE_CHAT_KEYWORDS = (
    "讲个笑话",
    "笑话",
    "讲个故事",
    "讲故事",
    "说个故事",
    "故事",
)
EXPLANATION_CHAT_KEYWORDS = (
    "解释",
    "为什么",
    "什么是",
    "哪些因素",
    "怎么来的",
    "怎么用",
    "什么意思",
    "怎么看",
    "你觉得",
)
CAPABILITY_CHAT_PATTERNS = (
    "你会",
    "你能",
    "你具备",
    "你可以",
)
NEGATIVE_CHAT_PATTERNS = (
    "不想聊",
    "不想再聊",
    "别聊",
)
ROBOT_EVALUATION_QUESTION_PATTERNS = (
    "厉害吗",
    "熟练吗",
    "擅长吗",
)

WEBSEARCH_KEYWORDS = (
    "天气",
    "新闻",
    "热搜",
    "最新",
    "实时",
    "联网",
    "搜索",
    "股价",
    "股票",
    "汇率",
    "黄金",
    "金价",
    "赛事",
    "比分",
    "weather",
    "rains",
)

WEATHER_KEYWORDS = ("天气", "气温", "下雨", "下雪", "刮风", "空气质量")
TIME_KEYWORDS = (
    "几点",
    "时间",
    "今天几号",
    "今天星期",
    "星期几",
    "日期",
    "农历",
    "阴历",
    "节假日",
    "时间戳",
)
TASK_KEYWORDS = (
    "提醒",
    "remind",
    "闹钟",
    "定时",
    "事项",
    "任务列表",
    "待办",
)
VISION_SEQUENCE_SIGNALS = (
    "画面",
    "镜头",
    "摄像头",
    "桌上",
    "桌面",
    "手里",
    "左手",
    "右手",
    "身后",
    "穿着",
    "穿的",
    "门关",
    "灯亮",
    "look at",
    "wearing",
)
UNSUPPORTED_ROBOT_CAPABILITY_KEYWORDS = (
    "空调",
    "开灯",
    "关灯",
    "灯光",
    "电视",
    "窗帘",
    "调亮",
    "调暗",
)
NEGATED_ROBOT_START_PREFIXES = (
    "不要",
    "不用",
    "先别",
    "别开始",
    "先不",
)
ROBOT_START_ACTION_KEYWORDS = (
    "跳舞",
    "跳",
    "巡逻",
    "巡检",
    "建图",
    "创建地图",
    "回桩",
    "充电",
    "跟随",
    "追随",
    "视频通话",
    "视频电话",
    "前进",
    "后退",
    "左转",
    "右转",
    "往前",
    "向前",
    "往后",
    "向后",
    "移动",
)

SINGING_PLAY_KEYWORDS = (
    "唱歌",
    "唱首",
    "唱一首",
    "唱一下",
    "唱一个",
    "唱一个首",
    "唱个歌",
    "来首歌",
    "来一首歌",
    "来个歌",
)
SINGING_LIST_KEYWORDS = (
    "会唱什么歌",
    "会唱哪些歌",
    "会唱哪几首",
    "有什么歌会唱",
    "都会唱什么",
)
UNSUPPORTED_DANCE_STYLE_KEYWORDS = (
    "街舞",
    "爵士舞",
    "民族舞",
    "芭蕾",
    "机械舞",
    "广场舞",
    "拉丁舞",
)
AMBIGUOUS_SIDE_EFFECT_TEXTS = {
    "走",
    "回",
    "弄一下",
    "取消掉",
    "不要了",
    "继续",
    "再来一次",
}
AFFIRMATIVE_CONFIRMATION_TEXTS = {
    "可以",
    "可以了",
    "可以的",
    "好",
    "好的",
    "好啊",
    "好呀",
    "行",
    "行吧",
    "没问题",
    "对",
    "对的",
    "是",
    "是的",
    "嗯",
    "嗯嗯",
    "开始吧",
    "看一下吧",
    "帮我看一下",
    "帮我看看",
    "那你看一下",
}
PENDING_ENVIRONMENT_CONFIRMATION_KEYWORDS = (
    "视觉识别",
    "查看环境",
    "环境理解",
    "看一下周围",
    "看看周围",
    "周围有没有",
    "附近有没有",
    "有没有人",
    "有没有长发",
    "有没有长头发",
    "长发的人",
    "长头发的人",
)

ROBOT_TOOL_INTENT_RULES = (
    (
        ("停下", "停住", "停止", "别动", "取消当前任务", "取消任务", "把音乐关掉", "关闭音乐"),
        (".cancel_robot_task", ".move_robot"),
    ),
    (
        ("巡检", "巡逻", "开始巡逻", "去巡逻"),
        (".patrol",),
    ),
    (
        ("追随", "跟随", "跟着我"),
        (".follow",),
    ),
    (
        ("回桩", "回去充电", "充电", "回充"),
        (".recharge_robot", ".return_to_charge"),
    ),
    (
        ("跳舞", "跳个舞", "舞蹈", "段舞", "就是现在"),
        (".dance",),
    ),
    (
        (
            "拨打视频电话",
            "呼叫视频电话",
            "打个视频电话",
            "打视频电话",
            "发起视频通话",
            "开始视频通话",
            "开启视频通话",
            "我要视频通话",
        ),
        (".call_video",),
    ),
    (
        ("打招呼", "打个招呼", "和我打个招呼", "招手", "招个手", "招招手", "挥挥手", "挥手", "问个好"),
        (".move_robot",),
    ),
    (
        ("握手", "握个手", "和我握个手", "来握个手"),
        (".move_robot",),
    ),
    (
        ("喝彩", "喝个彩", "庆祝一下", "欢呼", "欢呼一下", "鼓掌"),
        (".move_robot",),
    ),
    (
        ("手势检测", "检测手势", "识别手势", "手势"),
        (".detect_gesture",),
    ),
    (
        ("宠物检测", "检测宠物", "识别宠物", "看看宠物", "有没有宠物"),
        (".detect_pet",),
    ),
    (
        (
            "环境检测",
            "检测环境",
            "环境识别",
            "场景识别",
            "环境理解",
            "理解环境",
            "看一下周围环境",
            "看看周围有什么",
            "识别场景",
            "当前场景",
            "视觉识别",
            "识别一下",
            "看一下周围",
            "看看周围",
            "周围有没有",
            "附近有没有",
            "有没有人",
            "有没有长发",
            "有没有长头发",
            "长发的人",
            "长头发的人",
            "头发的人",
        ),
        (".understand_environment",),
    ),
    (
        ("创建地图", "开始建图", "建图", "建个地图", "生成地图", "地图建一下"),
        (".create_map",),
    ),
    (
        (
            "往前",
            "向前",
            "前进",
            "往前挪",
            "往后",
            "向后",
            "后退",
            "左转",
            "往左拐",
            "右转",
            "往右拐",
            "动一下",
        ),
        (".move_robot",),
    ),
)

ROBOT_MOVE_ACTION_RULES = (
    (
        "stop",
        ("停下", "停住", "停止", "别动", "取消当前任务", "取消任务"),
    ),
    (
        "greet",
        ("打招呼", "打个招呼", "和我打个招呼", "招手", "招个手", "招招手", "挥挥手", "挥手", "问个好"),
    ),
    (
        "handshake",
        ("握手", "握个手", "和我握个手", "来握个手"),
    ),
    (
        "cheer",
        ("喝彩", "喝个彩", "庆祝一下", "欢呼", "欢呼一下", "鼓掌"),
    ),
    (
        "forward",
        ("往前", "向前", "前进", "往前挪", "动一下"),
    ),
    (
        "backward",
        ("往后", "向后", "后退"),
    ),
    (
        "turn_left",
        ("左转", "往左拐"),
    ),
    (
        "turn_right",
        ("右转", "往右拐"),
    ),
)

ROBOT_DIRECT_NO_ARG_TOOL_SUFFIXES = (
    ".create_map",
    ".patrol",
    ".follow",
    ".recharge_robot",
    ".return_to_charge",
    ".dance",
    ".call_video",
    ".detect_gesture",
    ".detect_pet",
    ".understand_environment",
    ".cancel_robot_task",
)


def build_tool_latency_route(
    text: str,
    tools: list[dict] | None,
    *,
    bot_id: str | None = None,
    bot_name: str | None = None,
    messages: list[dict] | None = None,
    session_id: str | None = None,
) -> ToolLatencyRoute:
    if not text or not tools:
        return ToolLatencyRoute("legacy")

    if not _is_xiaowen_profile(bot_id, bot_name):
        return ToolLatencyRoute("legacy")

    if not messages and is_ambiguous_side_effect_request(text):
        return ToolLatencyRoute("chat", source="deterministic_safety")

    if is_negated_robot_start_request(text):
        return ToolLatencyRoute("chat", source="deterministic_safety")

    if is_negated_singing_request(text):
        return ToolLatencyRoute("chat", source="deterministic_safety")

    if is_unsupported_robot_request(text):
        return ToolLatencyRoute(
            "chat",
            source="deterministic_unavailable",
            category="unavailable_robot_capability",
        )

    if is_explicit_complex_request(text):
        return ToolLatencyRoute(
            "tool",
            source="deterministic",
            category="complex",
        )

    if not messages and is_incomplete_task_creation_request(text):
        return ToolLatencyRoute("chat", source="deterministic_safety")

    singing_intent = detect_singing_intent(text)
    if singing_intent:
        singing_tool_name = select_singing_tool_name(singing_intent, tools)
        if not singing_tool_name:
            return ToolLatencyRoute(
                "chat",
                source="deterministic_unavailable",
                category="unavailable_singing",
            )
        return ToolLatencyRoute(
            "tool",
            SINGING_PROGRESS_TEXT if singing_intent == "play" else None,
            tool_prefix="singing_remote.",
            selected_tool_name=singing_tool_name,
            require_tool_call=True,
            category="singing",
        )

    confirmed_robot_tool_name = select_confirmed_robot_tool_name(text, tools, messages)
    if confirmed_robot_tool_name:
        return ToolLatencyRoute(
            "tool",
            _build_robot_progress_text(text, confirmed_robot_tool_name, session_id),
            tool_prefix="robot_remote.",
            selected_tool_name=confirmed_robot_tool_name,
            require_tool_call=True,
            category="robot",
        )

    if _is_chat_intent(text):
        return ToolLatencyRoute("chat")

    robot_tool_name = select_robot_tool_name(text, tools)
    if robot_tool_name:
        return ToolLatencyRoute(
            "tool",
            _build_robot_progress_text(text, robot_tool_name, session_id),
            tool_prefix="robot_remote.",
            selected_tool_name=robot_tool_name,
            require_tool_call=True,
            category="robot",
        )

    if _contains_any(text, TASK_KEYWORDS):
        return _tool_route("robots_task_service.", tools, _build_task_progress_text(session_id))

    if _contains_any(text, TIME_KEYWORDS):
        return _tool_route("utils_remote.", tools, pick_quick_reply("tool.utils", session_id))

    if _is_websearch_query(text):
        return _build_websearch_route(text, tools, session_id)

    return ToolLatencyRoute("legacy")


def is_explicit_complex_request(text: str) -> bool:
    """识别条件触发或多工具顺序请求，避免被首个机器人动作降级。"""
    normalized = (text or "").lower()
    if not normalized or _is_chat_intent(normalized):
        return False

    has_time_signal = _contains_any(normalized, TIME_KEYWORDS) or bool(
        re.search(r"\b(?:current\s+time|time)\b", normalized)
    )

    matched_robot_intents = sum(
        1
        for keywords, _suffixes in ROBOT_TOOL_INTENT_RULES
        if any(keyword.lower() in normalized for keyword in keywords)
    )
    matched_singing_intents = int(detect_singing_intent(normalized) is not None)
    other_tool_signals = sum(
        (
            _contains_any(normalized, TASK_KEYWORDS),
            has_time_signal,
            _is_websearch_query(normalized),
            _contains_any(normalized, VISION_SEQUENCE_SIGNALS),
        )
    )
    tool_signal_count = matched_robot_intents + matched_singing_intents + other_tool_signals
    conditional = any(
        marker in normalized
        for marker in ("如果", "要是", "有的话", "没有的话", "的话", "否则", "满足条件", "if ")
    ) or bool(re.search(r"(?:没|有|是|不是|会|不会).{1,12}就", normalized))
    sequenced = any(
        marker in normalized
        for marker in ("先", "然后", "接着", "再", "之后", " then ")
    )
    sequence_parts = [
        part.strip("，,。；; ")
        for part in re.split(r"先|然后|接着|再|之后", normalized)
        if part.strip("，,。；; ")
    ]
    sequence_tool_segments = sum(
        1
        for part in sequence_parts
        if _contains_any(part, TASK_KEYWORDS)
        or _contains_any(part, TIME_KEYWORDS)
        or bool(re.search(r"\b(?:current\s+time|time)\b", part))
        or _is_websearch_query(part)
        or _contains_any(part, VISION_SEQUENCE_SIGNALS)
        or any(
            keyword.lower() in part
            for keywords, _suffixes in ROBOT_TOOL_INTENT_RULES
            for keyword in keywords
        )
        or detect_singing_intent(part) is not None
    )
    return other_tool_signals >= 2 or (conditional and tool_signal_count >= 1) or (
        sequenced and (tool_signal_count >= 2 or sequence_tool_segments >= 2)
    )


def detect_required_tool_category(text: str) -> str | None:
    """识别没有可用工具时仍必须阻止模型猜测的明确工具意图。"""
    if (
        not text
        or _is_chat_intent(text)
        or is_ambiguous_side_effect_request(text)
        or is_negated_robot_start_request(text)
    ):
        return None
    if is_unsupported_robot_request(text):
        return "robot_capability"
    if is_explicit_complex_request(text):
        return "complex"
    if is_incomplete_task_creation_request(text):
        return None
    if detect_singing_intent(text):
        return "singing"
    if _contains_any(text, TASK_KEYWORDS):
        return "task"
    if _contains_any(text, TIME_KEYWORDS):
        return "utils"
    if _is_websearch_query(text):
        return "websearch"
    normalized = text.lower()
    if any(
        keyword.lower() in normalized
        for keywords, _suffixes in ROBOT_TOOL_INTENT_RULES
        for keyword in keywords
    ):
        return "robot"
    return None


def is_unsupported_robot_request(text: str) -> bool:
    """识别 Robot MCP 未定义的能力或参数，禁止静默降级为固定动作。"""
    normalized = (text or "").lower()
    if any(keyword in normalized for keyword in UNSUPPORTED_ROBOT_CAPABILITY_KEYWORDS):
        return True
    if "音量" in normalized and any(keyword in normalized for keyword in ("音乐", "唱歌", "声音")):
        return True
    if "音乐声" in normalized and any(keyword in normalized for keyword in ("大", "小", "高", "低")):
        return True
    if "跳" in normalized and any(keyword in normalized for keyword in UNSUPPORTED_DANCE_STYLE_KEYWORDS):
        return True
    action_prefix = r"(?:往前|向前|前进|往后|向后|后退|左转|右转)"
    number = r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)"
    if re.search(action_prefix + r".{0,6}" + number + r"\s*(?:米|厘米|度|圈)", normalized):
        return True
    if re.search(
        action_prefix + r".{0,6}(?:\d+(?:\.\d+)?|[零二两三四五六七八九十百]+)\s*步",
        normalized,
    ):
        return True
    if re.search(r"(?:给|帮).{1,12}(?:打视频|视频通话|视频电话)", normalized) and not re.search(
        r"(?:给|帮)我(?:打|发起|开启|开始|呼叫)", normalized
    ):
        return True
    return False


def is_negated_robot_start_request(text: str) -> bool:
    """拒绝尚未开始的动作；“别跳了/停下”仍由取消工具处理。"""
    normalized = re.sub(r"\s+", "", (text or "").lower())
    if normalized in {"别动", "别跳了", "别唱了"} or any(
        keyword in normalized for keyword in ("停下", "停住", "停止")
    ):
        return False
    prefix_negated = any(prefix in normalized for prefix in NEGATED_ROBOT_START_PREFIXES)
    colloquial_negated = bool(
        re.search(r"^别(?:给我|再)?(?:跳|唱|巡逻|巡检|前进|后退|左转|右转|移动|打视频)", normalized)
    )
    return (prefix_negated or colloquial_negated) and any(
        action in normalized for action in ROBOT_START_ACTION_KEYWORDS
    )


def is_negated_singing_request(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    return normalized.startswith(("不要唱", "不用唱", "先别唱", "别唱", "不唱"))


def is_ambiguous_side_effect_request(text: str) -> bool:
    normalized = re.sub(r"[\s。！？!?.,，；;：:~～…]+", "", (text or "").lower())
    return normalized in AMBIGUOUS_SIDE_EFFECT_TEXTS


def is_incomplete_task_creation_request(text: str) -> bool:
    """创建提醒必须同时具有提醒内容和触发时间，否则先走 Chat 澄清。"""
    normalized = re.sub(r"\s+", "", (text or "").strip().lower())
    if not normalized or not any(keyword in normalized for keyword in ("提醒", "叫我")):
        return False
    if any(keyword in normalized for keyword in ("删除", "删掉", "删了", "取消", "列出", "查看", "查询", "有哪些")):
        return False
    has_time = bool(
        re.search(
            r"(?:今天|明天|后天|今晚|明早|今早|上午|下午|晚上|凌晨|下周|周[一二三四五六日天]|星期[一二三四五六日天]|"
            r"\d{1,2}(?:[:：]\d{1,2})?点?|[一二两三四五六七八九十半]+(?:点|分钟后|小时后)|\d+(?:分钟|小时|秒)后|"
            r"过(?:\d+|[一二两三四五六七八九十半]+)(?:分钟|小时)|半个钟头后|一会儿|稍后|半小时后|\d{1,2}月\d{1,2}[日号])",
            normalized,
        )
    )
    content = re.sub(r"^.*?(?:提醒(?:我)?|叫我)", "", normalized)
    content = re.sub(r"^(?:一下|一下子)", "", content)
    content = content.strip("呗吧呀啊")
    content = re.sub(r"[。！？!?.,，；;：:~～…]+$", "", content)
    return not has_time or not content


def infer_deterministic_robot_tool_args(text: str, tool_name: str) -> dict | None:
    """Build arguments for high-confidence robot routes without another LLM decision."""
    normalized = (text or "").lower()
    if _tool_name_matches(tool_name, ".move_robot"):
        for action, keywords in ROBOT_MOVE_ACTION_RULES:
            if any(keyword.lower() in normalized for keyword in keywords):
                return {"action": action}
        return None

    if any(_tool_name_matches(tool_name, suffix) for suffix in ROBOT_DIRECT_NO_ARG_TOOL_SUFFIXES):
        return {}

    return None


def detect_singing_intent(text: str) -> str | None:
    normalized = re.sub(r"[\s。！？!?.,，；;：:~～…]+", "", (text or "").lower())
    if not normalized:
        return None
    if normalized.startswith(("不要", "不用", "先别", "别唱", "不唱")):
        return None
    if any(keyword in normalized for keyword in SINGING_LIST_KEYWORDS):
        return "list"
    if re.search(r"(?:你)?会唱.+吗$", normalized) or normalized in {"你会唱歌吗", "会唱歌吗"}:
        return "list"
    if any(marker in normalized for marker in ("唱歌好听吗", "唱得好听吗", "唱歌怎么样")):
        return None
    if any(keyword in normalized for keyword in SINGING_PLAY_KEYWORDS):
        return "play"
    return None


def select_singing_tool_name(intent: str, tools: list[dict] | None) -> str | None:
    suffix = ".play_song" if intent == "play" else ".list_songs"
    for tool in filter_tools_by_prefix(tools, "singing_remote."):
        tool_name = _tool_name(tool)
        if _tool_name_matches(tool_name, suffix):
            return tool_name
    return None


def infer_deterministic_singing_tool_args(text: str, tool_name: str) -> dict | None:
    if _tool_name_matches(tool_name, ".play_song"):
        return {"query": (text or "").strip()}
    if not _tool_name_matches(tool_name, ".list_songs"):
        return None
    normalized = re.sub(r"[\s。！？!?.,，；;：:~～…]+", "", (text or "").lower())
    match = re.search(r"会唱(.+?)(?:的歌)?吗$", normalized)
    if match:
        query = match.group(1).strip("的")
        if query not in {"什么歌", "哪些歌", "哪几首", "歌"}:
            return {"query": query, "limit": 5}
    if any(keyword in normalized for keyword in SINGING_LIST_KEYWORDS) or normalized in {
        "你会唱歌吗",
        "会唱歌吗",
    }:
        return {"query": "", "limit": 5}
    query = normalized
    return {"query": query, "limit": 5}


def select_confirmed_robot_tool_name(
    text: str,
    tools: list[dict] | None,
    messages: list[dict] | None,
) -> str | None:
    if not _is_short_affirmative_confirmation(text):
        return None
    assistant_text = _last_assistant_text(messages)
    if not assistant_text:
        return None
    if not _contains_any(assistant_text, PENDING_ENVIRONMENT_CONFIRMATION_KEYWORDS):
        return None
    return _first_robot_tool_matching_suffix(tools, (".understand_environment",))


def select_robot_tool_name(text: str, tools: list[dict] | None) -> str | None:
    normalized = (text or "").lower()
    robot_tools = filter_tools_by_prefix(tools, "robot_remote.")
    if not robot_tools:
        robot_tools = [
            tool
            for tool in tools or []
            if any(_tool_name_matches(_tool_name(tool), suffix) for suffix in _robot_suffixes())
        ]
    for keywords, suffixes in ROBOT_TOOL_INTENT_RULES:
        if not any(keyword.lower() in normalized for keyword in keywords):
            continue
        if ".call_video" in suffixes and any(
            keyword in normalized
            for keyword in (
                "不要",
                "不用",
                "别打",
                "别拨",
                "别呼叫",
                "别发起",
                "先别",
                "取消视频",
                "停止视频",
                "怎么",
                "如何",
                "怎样",
            )
        ):
            continue
        for suffix in suffixes:
            for tool in robot_tools:
                tool_name = _tool_name(tool)
                if _tool_name_matches(tool_name, suffix):
                    return tool_name
    return None


def _first_robot_tool_matching_suffix(tools: list[dict] | None, suffixes: tuple[str, ...]) -> str | None:
    robot_tools = filter_tools_by_prefix(tools, "robot_remote.")
    if not robot_tools:
        robot_tools = [
            tool
            for tool in tools or []
            if any(_tool_name_matches(_tool_name(tool), suffix) for suffix in _robot_suffixes())
        ]
    for suffix in suffixes:
        for tool in robot_tools:
            tool_name = _tool_name(tool)
            if _tool_name_matches(tool_name, suffix):
                return tool_name
    return None


def _last_assistant_text(messages: list[dict] | None) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "assistant":
            return str(message.get("content") or "")
    return ""


def _is_short_affirmative_confirmation(text: str) -> bool:
    normalized = re.sub(r"[\s。！？!?.,，；;：:~～…]+", "", text or "").lower()
    if not normalized or len(normalized) > 8:
        return False
    return normalized in AFFIRMATIVE_CONFIRMATION_TEXTS


def filter_tools_by_prefix(tools: list[dict] | None, prefix: str) -> list[dict]:
    safe_prefix = prefix.replace(".", "__")
    return [
        tool
        for tool in tools or []
        if _tool_name(tool).startswith(prefix) or _tool_name(tool).startswith(safe_prefix)
    ]


def first_tool_name(tools: list[dict] | None) -> str | None:
    for tool in tools or []:
        name = _tool_name(tool)
        if name:
            return name
    return None


def parse_llm_router_decision(text: str) -> tuple[str, str | None] | None:
    normalized = str(text or "").strip().lower()
    if normalized in ROUTER_DECISION_CODE_TO_DECISION:
        return ROUTER_DECISION_CODE_TO_DECISION[normalized]
    if normalized == "exit":
        return "exit", None
    if normalized == "1":
        return "chat", None
    if not normalized.startswith("2:"):
        return None
    category = normalized[2:].strip().lower()
    if category not in ROUTER_TOOL_CATEGORIES:
        return None
    return "tool", category


def router_category_to_tool_prefix(category: str | None) -> str | None:
    return ROUTER_CATEGORY_TOOL_PREFIX.get((category or "").strip().lower())


def router_progress_text(category: str | None, session_id: str | None = None) -> str | None:
    key = (category or "").strip().lower()
    if key == "singing":
        return SINGING_PROGRESS_TEXT
    pool_key = ROUTER_CATEGORY_PROGRESS_POOL.get(key)
    if not pool_key:
        return None
    if pool_key == "robot.generic":
        return pick_robot_action_reply("generic", session_id)
    return pick_quick_reply(pool_key, session_id)


def build_fixed_exit_response(seed_text: str, session_id: str | None = None) -> str:
    del seed_text
    return pick_quick_reply("session.exit", session_id)


def build_whole_session_exit_response(text: str, session_id: str | None = None) -> str | None:
    normalized = (text or "").strip().lower().strip(EXIT_TRAILING_PUNCTUATION)
    if not normalized:
        return None
    if any(phrase in normalized for phrase in NON_WHOLE_SESSION_EXIT_PHRASES):
        return None
    if normalized in WHOLE_SESSION_EXIT_COMMANDS:
        return build_fixed_exit_response(normalized, session_id)
    return None


def _build_websearch_route(
    text: str,
    tools: list[dict] | None,
    session_id: str | None = None,
) -> ToolLatencyRoute:
    model_text = None
    progress_pool = "tool.websearch"
    if _contains_any(text, WEATHER_KEYWORDS):
        progress_pool = "tool.weather"
        if _weather_query_should_default_to_qingdao(text):
            model_text = "帮我查一下青岛今天的天气。"
    progress_text = pick_quick_reply(progress_pool, session_id)

    return _tool_route("websearch.", tools, progress_text, model_text=model_text)


def _build_task_progress_text(session_id: str | None = None) -> str:
    return pick_quick_reply("tool.task", session_id)


def _build_robot_progress_text(
    text: str,
    tool_name: str,
    session_id: str | None = None,
) -> str:
    normalized = (text or "").strip().lower()
    if _tool_name_matches(tool_name, ".cancel_robot_task") or any(
        keyword in normalized
        for keyword in ("停下", "停止", "别动", "取消当前任务", "取消任务")
    ):
        return pick_robot_action_reply("stop", session_id)
    if _tool_name_matches(tool_name, ".recharge_robot") or _tool_name_matches(tool_name, ".return_to_charge"):
        return pick_robot_action_reply("recharge", session_id)
    if _tool_name_matches(tool_name, ".call_video"):
        return pick_robot_action_reply("video_call", session_id)
    if _tool_name_matches(tool_name, ".move_robot"):
        if any(keyword in normalized for keyword in ("往前", "向前", "前进")):
            return pick_robot_action_reply("move_forward", session_id)
        if any(keyword in normalized for keyword in ("往后", "向后", "后退")):
            return pick_robot_action_reply("move_backward", session_id)
        if "左转" in normalized:
            return pick_robot_action_reply("turn_left", session_id)
        if "右转" in normalized:
            return pick_robot_action_reply("turn_right", session_id)
        if any(keyword in normalized for keyword in ("打招呼", "打个招呼", "招手", "招个手", "招招手", "挥挥手", "挥手", "问个好")):
            return pick_robot_action_reply("greet", session_id)
        if any(keyword in normalized for keyword in ("握手", "握个手")):
            return pick_robot_action_reply("handshake", session_id)
        if any(keyword in normalized for keyword in ("喝彩", "喝个彩", "庆祝一下", "欢呼", "鼓掌")):
            return pick_robot_action_reply("cheer", session_id)
    return pick_robot_action_reply("generic", session_id)


def _weather_query_should_default_to_qingdao(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    # Only rewrite clearly location-less weather questions. If any meaningful
    # content survives these intent/time words, keep the user's original query
    # so city names such as 北京、深圳、黄岛 are not overwritten by the default.
    residue = normalized
    for token in (
        "请你",
        "请",
        "帮我",
        "给我",
        "帮忙",
        "我想知道",
        "想知道",
        "查一下",
        "查查",
        "查询",
        "看一下",
        "看看",
        "今天",
        "明天",
        "后天",
        "现在",
        "当前",
        "最近",
        "这几天",
        "这两天",
        "周末",
        "天气预报",
        "天气",
        "气温",
        "温度",
        "下雨",
        "下雪",
        "刮风",
        "空气质量",
        "怎么样",
        "如何",
        "多少",
        "有没有",
        "会不会",
        "是不是",
        "一下",
        "吗",
        "呢",
        "啊",
        "呀",
        "的",
    ):
        residue = residue.replace(token, "")
    residue = residue.strip(" 。！？!?.,，；;：:~～…")
    return not residue


def _tool_route(
    tool_prefix: str,
    tools: list[dict] | None,
    progress_text: str,
    *,
    model_text: str | None = None,
) -> ToolLatencyRoute:
    scoped_tools = filter_tools_by_prefix(tools, tool_prefix)
    tool_name = first_tool_name(scoped_tools)
    if not tool_name:
        return ToolLatencyRoute("legacy")
    return ToolLatencyRoute(
        "tool",
        progress_text,
        tool_prefix=tool_prefix,
        selected_tool_name=tool_name,
        require_tool_call=True,
        model_text=model_text,
        category=next(
            (
                category
                for category, category_prefix in ROUTER_CATEGORY_TOOL_PREFIX.items()
                if category_prefix == tool_prefix
            ),
            None,
        ),
    )


def _is_xiaowen_profile(bot_id: str | None, bot_name: str | None) -> bool:
    """判断 bot 是否启用 4B 路由 + tool 强制。

    所有具有运行态 bot_id 的 Bot 使用同一套路由规则；具体能力只由
    Runtime Snapshot 中绑定的工具集决定，不能按 Bot ID/名称分流。
    """
    safe_bot_id = (bot_id or "").strip().lower()
    return bool(safe_bot_id)


def is_xiaowen_profile(bot_id: str | None, bot_name: str | None) -> bool:
    return _is_xiaowen_profile(bot_id, bot_name)


def _is_low_tool_risk_chat(text: str) -> bool:
    return _contains_any(text, LOW_TOOL_RISK_CHAT_KEYWORDS)


def _is_explanation_chat(text: str) -> bool:
    return _contains_any(text, EXPLANATION_CHAT_KEYWORDS)


def _is_capability_question(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized.endswith(("?", "？", "吗", "吗?", "吗？")):
        return False
    # 对眼前画面的实际识别请求不是能力闲聊，应交给视觉路由判断。
    if _contains_any(
        normalized,
        ("当前画面", "画面里", "镜头里", "摄像头", "我穿", "我的表情"),
    ):
        return False
    return any(pattern.lower() in normalized for pattern in CAPABILITY_CHAT_PATTERNS)


def _is_chat_intent(text: str) -> bool:
    return (
        _is_narrative_chat(text)
        or _is_low_tool_risk_chat(text)
        or _is_explanation_chat(text)
        or _is_capability_question(text)
        or _contains_any(text, NEGATIVE_CHAT_PATTERNS)
        or _is_robot_evaluation_question(text)
        or _is_hypothetical_chat(text)
        or any(
            marker in (text or "").lower()
            for marker in ("唱歌好听吗", "唱得好听吗", "唱歌怎么样")
        )
    )


def _is_robot_evaluation_question(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return any(pattern in normalized for pattern in ROBOT_EVALUATION_QUESTION_PATTERNS) and any(
        keyword in normalized for keyword in ("跳", "唱", "走", "巡逻", "表演")
    )


def _is_hypothetical_chat(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return normalized.startswith(("假如", "假设")) and any(
        marker in normalized for marker in ("会怎样", "会怎么样", "会发生什么", "怎么办")
    )


def _is_narrative_chat(text: str) -> bool:
    return _contains_any(text, NARRATIVE_CHAT_KEYWORDS)


def _is_websearch_query(text: str) -> bool:
    return _contains_any(text, WEBSEARCH_KEYWORDS) or _contains_any(text, WEATHER_KEYWORDS)


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    normalized = (text or "").strip().lower()
    return any(keyword.lower() in normalized for keyword in keywords)


def _tool_name(tool: dict) -> str:
    return tool.get("function", {}).get("name", "")


def _tool_name_matches(tool_name: str, suffix: str) -> bool:
    return (tool_name or "").endswith(suffix) or (tool_name or "").endswith(suffix.replace(".", "__"))


def _robot_suffixes() -> tuple[str, ...]:
    suffixes: list[str] = []
    for _, rule_suffixes in ROBOT_TOOL_INTENT_RULES:
        suffixes.extend(rule_suffixes)
    return tuple(dict.fromkeys(suffixes))
