from __future__ import annotations

from dataclasses import dataclass
import random
import re

from llm.agent_runtime import AgentContext, AgentResult, BaseAgent

RIDDLE_AGENT_ID = "riddle_agent"

ENTRY_PATTERNS = [
    r"(猜|玩|来).{0,4}(谜语|灯谜)",
    r"(谜语|灯谜).{0,4}(游戏|来一个|玩一下)",
]

EXIT_WORDS = [
    "不猜了",
    "退出谜语",
    "结束谜语",
    "不玩了",
    "退出游戏",
    "回到聊天",
    "退出",
]

NEXT_WORDS = [
    "继续",
    "再来",
    "再来一个",
    "下一题",
    "来一个",
    "可以",
    "好",
]


@dataclass(frozen=True)
class Riddle:
    id: str
    question: str
    answer: str
    aliases: tuple[str, ...]
    hint: str


RIDDLES: list[Riddle] = [
    Riddle(
        id="umbrella",
        question="什么东西下雨天最忙，晴天却常被遗忘？",
        answer="雨伞",
        aliases=("伞", "雨伞"),
        hint="它通常被你拿在手里。",
    ),
    Riddle(
        id="mirror",
        question="你笑它也笑，你哭它也哭，它是谁？",
        answer="镜子",
        aliases=("镜子", "镜"),
        hint="每天洗漱时很容易见到它。",
    ),
    Riddle(
        id="clock",
        question="有脚不会走，有嘴不会说，却天天提醒你时间。",
        answer="钟表",
        aliases=("钟", "表", "钟表", "闹钟"),
        hint="墙上、手腕上都可能有。",
    ),
    Riddle(
        id="book",
        question="肚子里全是字，越翻越有意思。是什么？",
        answer="书",
        aliases=("书", "书本", "本子"),
        hint="它常待在书架上。",
    ),
    Riddle(
        id="snow",
        question="从天上来，落地就白，太阳一出来就不见。",
        answer="雪",
        aliases=("雪", "雪花"),
        hint="冬天更常见。",
    ),
    Riddle(
        id="candle",
        question="越烧越短，越短越亮。是什么？",
        answer="蜡烛",
        aliases=("蜡烛", "烛"),
        hint="停电时它很有用。",
    ),
    Riddle(
        id="shadow",
        question="你走它也走，你停它也停，黑夜里却常不见。",
        answer="影子",
        aliases=("影子", "人影"),
        hint="有光的时候更容易看到它。",
    ),
    Riddle(
        id="toothbrush",
        question="天天进嘴不吃饭，只帮别人洗个澡。",
        answer="牙刷",
        aliases=("牙刷",),
        hint="早晚都可能用到。",
    ),
    Riddle(
        id="key",
        question="个头不大本领强，专门打开小房门。",
        answer="钥匙",
        aliases=("钥匙", "锁匙"),
        hint="常和锁在一起。",
    ),
    Riddle(
        id="soap",
        question="越洗自己越小，别人却越来越干净。",
        answer="肥皂",
        aliases=("肥皂", "香皂", "皂"),
        hint="洗手洗澡会用它。",
    ),
    Riddle(
        id="glasses",
        question="两个圈圈架鼻梁，帮人看清远和近。",
        answer="眼镜",
        aliases=("眼镜", "镜片"),
        hint="近视的人常戴。",
    ),
    Riddle(
        id="pencil",
        question="身体细长黑心肠，越写越短帮人忙。",
        answer="铅笔",
        aliases=("铅笔", "笔"),
        hint="写错了还能擦。",
    ),
    Riddle(
        id="eraser",
        question="别人写错它来忙，越帮越矮身上脏。",
        answer="橡皮",
        aliases=("橡皮", "橡皮擦"),
        hint="它常和铅笔做朋友。",
    ),
    Riddle(
        id="refrigerator",
        question="肚子冰冰能装菜，夏天最爱它帮忙。",
        answer="冰箱",
        aliases=("冰箱",),
        hint="厨房里常见的大电器。",
    ),
    Riddle(
        id="stairs",
        question="一格一格往上排，走它能上也能下。",
        answer="楼梯",
        aliases=("楼梯", "台阶", "阶梯"),
        hint="没有电也能上下楼。",
    ),
    Riddle(
        id="river",
        question="弯弯曲曲向前走，没有脚却不停留。",
        answer="河流",
        aliases=("河", "河流", "小河"),
        hint="它常往低处流。",
    ),
    Riddle(
        id="cloud",
        question="白白一团天上飘，变黑可能雨来到。",
        answer="云",
        aliases=("云", "云朵", "乌云"),
        hint="抬头看天容易见到。",
    ),
    Riddle(
        id="moon",
        question="晚上出来白又圆，有时弯弯像小船。",
        answer="月亮",
        aliases=("月亮", "月", "月牙"),
        hint="中秋节大家常看它。",
    ),
    Riddle(
        id="sun",
        question="早上东边升，晚上西边落，照得大地暖洋洋。",
        answer="太阳",
        aliases=("太阳", "日头"),
        hint="白天最亮的那个。",
    ),
    Riddle(
        id="fan",
        question="三片叶子不长树，一转起来送凉风。",
        answer="电风扇",
        aliases=("风扇", "电风扇", "扇子"),
        hint="夏天常用。",
    ),
    Riddle(
        id="telephone",
        question="离得很远也能聊，叮铃一响把人找。",
        answer="电话",
        aliases=("电话", "手机", "座机"),
        hint="它能帮人通话。",
    ),
    Riddle(
        id="map",
        question="不走一步知天下，山川道路都画下。",
        answer="地图",
        aliases=("地图",),
        hint="出门找路会用它。",
    ),
    Riddle(
        id="shoe",
        question="两兄弟，脚下住，走南闯北不怕路。",
        answer="鞋",
        aliases=("鞋", "鞋子"),
        hint="每天出门常穿。",
    ),
    Riddle(
        id="hat",
        question="头上一座小房子，遮风挡雨又遮阳。",
        answer="帽子",
        aliases=("帽子", "帽"),
        hint="它戴在头上。",
    ),
    Riddle(
        id="teapot",
        question="大肚小嘴一只耳，倒出热水香气飞。",
        answer="茶壶",
        aliases=("茶壶", "壶"),
        hint="喝茶时可能用到。",
    ),
    Riddle(
        id="chopsticks",
        question="两根小棍手中拿，夹菜吃饭全靠它。",
        answer="筷子",
        aliases=("筷子", "筷"),
        hint="中餐里特别常见。",
    ),
    Riddle(
        id="needle",
        question="身体细又尖，尾巴带只眼，衣服破了它来连。",
        answer="针",
        aliases=("针", "缝衣针"),
        hint="缝衣服会用。",
    ),
    Riddle(
        id="envelope",
        question="薄薄小房子，专门装话语，寄出走远路。",
        answer="信封",
        aliases=("信封", "信"),
        hint="写信时会用到。",
    ),
    Riddle(
        id="traffic_light",
        question="红黄绿三兄弟，站在路口管秩序。",
        answer="红绿灯",
        aliases=("红绿灯", "交通灯", "信号灯"),
        hint="过马路要看它。",
    ),
    Riddle(
        id="doorbell",
        question="小手一按它就叫，主人听见把门瞧。",
        answer="门铃",
        aliases=("门铃",),
        hint="它常在门口。",
    ),
    Riddle(
        id="comb",
        question="一排牙齿不会咬，天天帮人理头发。",
        answer="梳子",
        aliases=("梳子", "梳"),
        hint="早上整理头发常用它。",
    ),
    Riddle(
        id="scissors",
        question="两把小刀嘴对嘴，咔嚓咔嚓会裁纸。",
        answer="剪刀",
        aliases=("剪刀", "剪子"),
        hint="手工课上很常见。",
    ),
    Riddle(
        id="broom",
        question="长长一条尾巴多，地上灰尘它来拖。",
        answer="扫帚",
        aliases=("扫帚", "笤帚", "扫把"),
        hint="打扫房间会用到。",
    ),
    Riddle(
        id="cup",
        question="肚子不大能装水，天天陪人喝几回。",
        answer="杯子",
        aliases=("杯子", "水杯", "杯"),
        hint="喝水的时候会拿起它。",
    ),
    Riddle(
        id="spoon",
        question="小小脑袋长尾巴，舀汤吃饭都靠它。",
        answer="勺子",
        aliases=("勺子", "汤勺", "勺"),
        hint="喝汤时常用。",
    ),
    Riddle(
        id="pillow",
        question="软软一方不说话，晚上陪你把头放。",
        answer="枕头",
        aliases=("枕头", "枕"),
        hint="睡觉时离头最近。",
    ),
    Riddle(
        id="blanket",
        question="冬天抱住你，夜里盖住你，暖暖和和不言语。",
        answer="被子",
        aliases=("被子", "棉被", "被"),
        hint="睡觉时会盖在身上。",
    ),
    Riddle(
        id="chair",
        question="四条腿，不会走，累了坐上歇一歇。",
        answer="椅子",
        aliases=("椅子", "椅"),
        hint="它常在桌子旁边。",
    ),
    Riddle(
        id="table",
        question="四脚站得稳，肚皮平又宽，吃饭写字都用它。",
        answer="桌子",
        aliases=("桌子", "桌"),
        hint="饭菜和书本都能放上面。",
    ),
    Riddle(
        id="window",
        question="一面透明墙，能看外面光，打开还有风进房。",
        answer="窗户",
        aliases=("窗户", "窗"),
        hint="它通常在墙上。",
    ),
    Riddle(
        id="door",
        question="站在墙里把路挡，推开以后能进房。",
        answer="门",
        aliases=("门", "房门"),
        hint="进出房间要经过它。",
    ),
    Riddle(
        id="wallet",
        question="小小口袋随身带，钱和卡片住里边。",
        answer="钱包",
        aliases=("钱包", "钱夹"),
        hint="买东西时可能会打开它。",
    ),
    Riddle(
        id="bag",
        question="肚子能装好多样，背在身上去远方。",
        answer="书包",
        aliases=("书包", "背包", "包"),
        hint="上学或出门常背。",
    ),
    Riddle(
        id="camera",
        question="咔嚓一声留住景，笑脸风光都能藏。",
        answer="相机",
        aliases=("相机", "照相机", "摄像机"),
        hint="拍照片会用它。",
    ),
    Riddle(
        id="television",
        question="方方一扇窗，不开也会亮，故事新闻里面放。",
        answer="电视",
        aliases=("电视", "电视机"),
        hint="客厅里常见。",
    ),
    Riddle(
        id="light_bulb",
        question="玻璃脑袋一通电，黑夜马上亮起来。",
        answer="灯泡",
        aliases=("灯泡", "电灯", "灯"),
        hint="它能照明。",
    ),
    Riddle(
        id="battery",
        question="个头不大藏电量，玩具遥控靠它忙。",
        answer="电池",
        aliases=("电池",),
        hint="遥控器里常有它。",
    ),
    Riddle(
        id="remote_control",
        question="小板身上按钮多，坐着也能换节目。",
        answer="遥控器",
        aliases=("遥控器", "遥控"),
        hint="看电视时常拿着。",
    ),
    Riddle(
        id="calendar",
        question="一页一页记日子，今天明天它都知。",
        answer="日历",
        aliases=("日历", "挂历", "台历"),
        hint="它能帮你看日期。",
    ),
    Riddle(
        id="newspaper",
        question="薄薄几张满是字，新闻消息都在里。",
        answer="报纸",
        aliases=("报纸", "报"),
        hint="以前很多人早上读它。",
    ),
    Riddle(
        id="basket",
        question="肚子空空孔又多，买菜装果都不错。",
        answer="篮子",
        aliases=("篮子", "菜篮"),
        hint="去市场可能用到。",
    ),
    Riddle(
        id="ball",
        question="圆圆身体满地跑，踢它拍它都能跳。",
        answer="球",
        aliases=("球", "皮球"),
        hint="运动场上常见。",
    ),
    Riddle(
        id="kite",
        question="身轻线长天上飞，风一吹来它就追。",
        answer="风筝",
        aliases=("风筝",),
        hint="春天放它很开心。",
    ),
    Riddle(
        id="bridge",
        question="站在水上不怕湿，帮人过河去对岸。",
        answer="桥",
        aliases=("桥", "桥梁"),
        hint="河两边靠它相连。",
    ),
    Riddle(
        id="train",
        question="一节一节连成队，铁轨上面跑得飞。",
        answer="火车",
        aliases=("火车", "列车"),
        hint="它在铁轨上行驶。",
    ),
    Riddle(
        id="airplane",
        question="大鸟没有羽毛衣，载人穿云万里飞。",
        answer="飞机",
        aliases=("飞机",),
        hint="机场里能看到它。",
    ),
    Riddle(
        id="boat",
        question="水上小屋会前行，过河出海它有名。",
        answer="船",
        aliases=("船", "小船", "轮船"),
        hint="它在水上行驶。",
    ),
    Riddle(
        id="rainbow",
        question="雨后天边一座桥，七彩衣裳弯弯腰。",
        answer="彩虹",
        aliases=("彩虹", "虹"),
        hint="太阳和雨后更容易见到。",
    ),
    Riddle(
        id="star",
        question="夜里眨眼小亮点，天上一颗又一颗。",
        answer="星星",
        aliases=("星星", "星"),
        hint="晚上抬头看天能见到。",
    ),
    Riddle(
        id="ice",
        question="水变硬来冷冰冰，手里一握慢慢轻。",
        answer="冰",
        aliases=("冰", "冰块"),
        hint="放进饮料会变凉。",
    ),
]


class RiddleAgent(BaseAgent):
    id = RIDDLE_AGENT_ID
    name = "猜谜语"
    description = "语音猜谜小游戏，随机出题、提示、判断答案并支持继续下一题。"
    trigger_examples = ("猜个谜语", "来个灯谜", "玩一下谜语游戏")

    def can_enter(self, text: str, context: AgentContext) -> bool:
        normalized = _normalize_text(text)
        return any(re.search(pattern, normalized) for pattern in ENTRY_PATTERNS)

    async def start(self, text: str, context: AgentContext) -> AgentResult:
        state, response = _next_riddle([], score=0, round_count=0)
        return _riddle_result(text=response, state=state, finished=False)

    async def handle(
        self,
        text: str,
        state: dict,
        context: AgentContext,
    ) -> AgentResult:
        if _is_exit(text):
            score = int(state.get("score") or 0)
            rounds = int(state.get("round_count") or 0)
            return _riddle_result(
                text=f"好的，不猜谜语了。你这次答对 {score} 题，共玩了 {rounds} 题。",
                state=None,
                finished=True,
            )

        if state.get("awaiting_next"):
            if _is_next(text):
                next_state, response = _next_riddle(
                    list(state.get("used_ids") or []),
                    score=int(state.get("score") or 0),
                    round_count=int(state.get("round_count") or 0),
                )
                return _riddle_result(text=response, state=next_state, finished=False)
            return _riddle_result(
                text="如果还想猜，就说再来一个；不想玩了，可以说退出谜语。",
                state=state,
                finished=False,
            )

        current = _find_riddle(str(state.get("current_id") or ""))
        if current is None:
            next_state, response = _next_riddle(
                list(state.get("used_ids") or []),
                score=int(state.get("score") or 0),
                round_count=int(state.get("round_count") or 0),
            )
            return _riddle_result(text=response, state=next_state, finished=False)

        if _matches_answer(text, current):
            next_state = {
                **state,
                "score": int(state.get("score") or 0) + 1,
                "awaiting_next": True,
            }
            return _riddle_result(
                text=f"答对啦，答案就是{current.answer}。还要再来一个吗？",
                state=next_state,
                finished=False,
            )

        attempts = int(state.get("attempts") or 0) + 1
        if attempts == 1:
            next_state = {**state, "attempts": attempts}
            return _riddle_result(
                text=f"还不对，给你个提示：{current.hint}",
                state=next_state,
                finished=False,
            )

        next_state = {**state, "awaiting_next": True, "attempts": attempts}
        return _riddle_result(
            text=f"公布答案，是{current.answer}。还要再猜一个吗？",
            state=next_state,
            finished=False,
        )


def _next_riddle(
    used_ids: list[str],
    *,
    score: int,
    round_count: int,
) -> tuple[dict, str]:
    candidates = [item for item in RIDDLES if item.id not in used_ids]
    if not candidates:
        used_ids = []
        candidates = RIDDLES[:]
    riddle = random.choice(candidates)
    next_used = [*used_ids, riddle.id]
    state = {
        "agent_id": RIDDLE_AGENT_ID,
        "current_id": riddle.id,
        "used_ids": next_used,
        "score": score,
        "round_count": round_count + 1,
        "attempts": 0,
        "awaiting_next": False,
    }
    return state, f"来，猜一个：{riddle.question}"


def _riddle_result(
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


def _find_riddle(riddle_id: str) -> Riddle | None:
    for item in RIDDLES:
        if item.id == riddle_id:
            return item
    return None


def _matches_answer(text: str, riddle: Riddle) -> bool:
    normalized = _normalize_text(text)
    return any(_normalize_text(alias) in normalized for alias in riddle.aliases)


def _is_exit(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in EXIT_WORDS)


def _is_next(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in NEXT_WORDS)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def create_agent() -> BaseAgent:
    return RiddleAgent()
