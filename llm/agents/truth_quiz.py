from __future__ import annotations

from dataclasses import dataclass
import random
import re

from llm.agent_runtime import AgentContext, AgentResult, BaseAgent

TRUTH_QUIZ_AGENT_ID = "truth_quiz_agent"

ENTRY_PATTERNS = [
    r"(真假|对错).{0,4}(判断|游戏|问答)",
    r"(玩|来).{0,4}(真假|对错)",
    r"判断.{0,4}(真假|对错)",
]

EXIT_WORDS = [
    "不玩了",
    "退出真假",
    "结束真假",
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

TRUE_WORDS = [
    "真的",
    "真",
    "对",
    "是",
    "正确",
    "没错",
]

FALSE_WORDS = [
    "假的",
    "假",
    "错",
    "不对",
    "不是",
    "错误",
]


@dataclass(frozen=True)
class TruthStatement:
    id: str
    statement: str
    answer: bool
    explanation: str


STATEMENTS: list[TruthStatement] = [
    TruthStatement(
        id="octopus",
        statement="章鱼有三颗心脏。",
        answer=True,
        explanation="是真的，章鱼有三颗心脏。",
    ),
    TruthStatement(
        id="banana_tree",
        statement="香蕉长在真正的木质树上。",
        answer=False,
        explanation="是假的，香蕉植株更接近大型草本植物。",
    ),
    TruthStatement(
        id="honey",
        statement="蜂蜜在合适保存条件下很难变质。",
        answer=True,
        explanation="是真的，低水分和高糖环境让它很耐保存。",
    ),
    TruthStatement(
        id="sunflower",
        statement="所有向日葵都会一直跟着太阳转。",
        answer=False,
        explanation="是假的，成熟向日葵通常固定朝向东方。",
    ),
    TruthStatement(
        id="sound_space",
        statement="声音可以在真空中传播。",
        answer=False,
        explanation="是假的，声音需要介质传播。",
    ),
    TruthStatement(
        id="bat_mammal",
        statement="蝙蝠是哺乳动物。",
        answer=True,
        explanation="是真的，蝙蝠会哺乳幼崽。",
    ),
    TruthStatement(
        id="penguin_fly",
        statement="企鹅会飞。",
        answer=False,
        explanation="是假的，企鹅不会飞，但很会游泳。",
    ),
    TruthStatement(
        id="water_boils",
        statement="标准大气压下，水通常在一百度沸腾。",
        answer=True,
        explanation="是真的，这是常见物理常识。",
    ),
    TruthStatement(
        id="human_bones",
        statement="成年人身体里通常有二百零六块骨头。",
        answer=True,
        explanation="是真的，成年人通常约有二百零六块骨头。",
    ),
    TruthStatement(
        id="shark_bone",
        statement="鲨鱼的骨骼主要由软骨构成。",
        answer=True,
        explanation="是真的，鲨鱼属于软骨鱼类。",
    ),
    TruthStatement(
        id="earth_flat",
        statement="地球是完全平的。",
        answer=False,
        explanation="是假的，地球整体接近球体。",
    ),
    TruthStatement(
        id="venus_hot",
        statement="金星表面非常炎热。",
        answer=True,
        explanation="是真的，金星有强烈温室效应。",
    ),
    TruthStatement(
        id="camel_hump_water",
        statement="骆驼的驼峰主要储存水。",
        answer=False,
        explanation="是假的，驼峰主要储存脂肪。",
    ),
    TruthStatement(
        id="whale_fish",
        statement="鲸鱼是鱼类。",
        answer=False,
        explanation="是假的，鲸鱼是哺乳动物。",
    ),
    TruthStatement(
        id="lightning_hot",
        statement="闪电的温度可以非常高。",
        answer=True,
        explanation="是真的，闪电通道温度可远高于太阳表面。",
    ),
    TruthStatement(
        id="spider_insect",
        statement="蜘蛛是昆虫。",
        answer=False,
        explanation="是假的，蜘蛛有八条腿，属于蛛形纲。",
    ),
    TruthStatement(
        id="gold_symbol",
        statement="黄金的化学符号是 Au。",
        answer=True,
        explanation="是真的，Au 来自黄金的拉丁名。",
    ),
    TruthStatement(
        id="oxygen_symbol",
        statement="氧气的化学符号是 O。",
        answer=True,
        explanation="基本正确，元素氧的符号是 O。",
    ),
    TruthStatement(
        id="plants_photosynthesis",
        statement="绿色植物能进行光合作用。",
        answer=True,
        explanation="是真的，光合作用能制造有机物并释放氧气。",
    ),
    TruthStatement(
        id="tomato_vegetable",
        statement="从植物学角度看，番茄是果实。",
        answer=True,
        explanation="是真的，番茄在植物学上属于果实。",
    ),
    TruthStatement(
        id="ostrich_fast",
        statement="鸵鸟跑得很快。",
        answer=True,
        explanation="是真的，鸵鸟是奔跑速度很快的鸟类。",
    ),
    TruthStatement(
        id="polar_bear_south",
        statement="野生北极熊主要生活在南极。",
        answer=False,
        explanation="是假的，北极熊主要生活在北极地区。",
    ),
    TruthStatement(
        id="moon_light",
        statement="月亮自己会发出强光。",
        answer=False,
        explanation="是假的，我们看到的月光主要来自太阳反射。",
    ),
    TruthStatement(
        id="sound_faster_than_light",
        statement="声音比光传播得更快。",
        answer=False,
        explanation="是假的，光速远远快于声速。",
    ),
    TruthStatement(
        id="blood_red",
        statement="人的血液通常是红色的。",
        answer=True,
        explanation="是真的，血红蛋白让血液呈红色。",
    ),
    TruthStatement(
        id="koala_fingerprint",
        statement="考拉的指纹和人类指纹很相似。",
        answer=True,
        explanation="是真的，考拉指纹纹路很像人类。",
    ),
    TruthStatement(
        id="coffee_bean",
        statement="咖啡豆其实是咖啡果里的种子。",
        answer=True,
        explanation="是真的，咖啡豆是咖啡果实里的种子。",
    ),
    TruthStatement(
        id="glass_liquid",
        statement="普通玻璃在室温下会像水一样快速流动。",
        answer=False,
        explanation="是假的，玻璃不会在日常尺度下快速流动。",
    ),
    TruthStatement(
        id="rainbow_colors",
        statement="彩虹常被说成有七种颜色。",
        answer=True,
        explanation="是真的，常见说法是红橙黄绿青蓝紫。",
    ),
    TruthStatement(
        id="mushroom_plant",
        statement="蘑菇属于植物。",
        answer=False,
        explanation="是假的，蘑菇属于真菌。",
    ),
    TruthStatement(
        id="bee_dance",
        statement="蜜蜂会用特殊舞蹈传递食物位置。",
        answer=True,
        explanation="是真的，蜜蜂可以通过舞蹈传递方向和距离信息。",
    ),
    TruthStatement(
        id="snail_teeth",
        statement="蜗牛嘴里可能有很多细小的齿状结构。",
        answer=True,
        explanation="是真的，蜗牛有齿舌，上面有许多细小结构。",
    ),
    TruthStatement(
        id="dolphin_mammal",
        statement="海豚是哺乳动物。",
        answer=True,
        explanation="是真的，海豚用肺呼吸，也会哺乳幼崽。",
    ),
    TruthStatement(
        id="cactus_water",
        statement="仙人掌能在茎里储存水分。",
        answer=True,
        explanation="是真的，这有助于它适应干旱环境。",
    ),
    TruthStatement(
        id="metal_expands",
        statement="很多金属受热会膨胀。",
        answer=True,
        explanation="是真的，热胀冷缩是常见物理现象。",
    ),
    TruthStatement(
        id="ice_floats",
        statement="冰通常会浮在水面上。",
        answer=True,
        explanation="是真的，冰的密度通常比液态水小。",
    ),
    TruthStatement(
        id="salt_water_freeze",
        statement="盐水通常比纯水更不容易结冰。",
        answer=True,
        explanation="是真的，加入盐会降低水的凝固点。",
    ),
    TruthStatement(
        id="earth_revolves_sun",
        statement="地球绕太阳公转。",
        answer=True,
        explanation="是真的，地球约一年绕太阳一周。",
    ),
    TruthStatement(
        id="mars_red",
        statement="火星常被称为红色星球。",
        answer=True,
        explanation="是真的，火星表面氧化铁让它看起来偏红。",
    ),
    TruthStatement(
        id="jupiter_largest",
        statement="木星是太阳系中最大的行星。",
        answer=True,
        explanation="是真的，木星体积和质量都很大。",
    ),
    TruthStatement(
        id="human_lungs",
        statement="人类通常用肺呼吸。",
        answer=True,
        explanation="是真的，肺是人体重要的呼吸器官。",
    ),
    TruthStatement(
        id="heart_pumps_blood",
        statement="心脏的主要功能之一是推动血液循环。",
        answer=True,
        explanation="是真的，心脏像泵一样推动血液流动。",
    ),
    TruthStatement(
        id="taste_buds",
        statement="舌头上有帮助感受味道的味蕾。",
        answer=True,
        explanation="是真的，味蕾能帮助我们感知味道。",
    ),
    TruthStatement(
        id="owls_night",
        statement="很多猫头鹰在夜间活动较多。",
        answer=True,
        explanation="是真的，许多猫头鹰属于夜行性动物。",
    ),
    TruthStatement(
        id="kangaroo_pouch",
        statement="袋鼠妈妈有育儿袋。",
        answer=True,
        explanation="是真的，幼崽会在育儿袋里继续发育。",
    ),
    TruthStatement(
        id="tree_rings",
        statement="树木年轮常能反映树的生长情况。",
        answer=True,
        explanation="是真的，年轮可以帮助了解树木年龄和生长环境。",
    ),
    TruthStatement(
        id="silk_worm",
        statement="蚕可以吐丝结茧。",
        answer=True,
        explanation="是真的，蚕丝就是由蚕吐出的丝形成的。",
    ),
    TruthStatement(
        id="magnet_attracts_plastic",
        statement="普通磁铁能吸住大多数塑料。",
        answer=False,
        explanation="是假的，普通塑料通常不会被磁铁吸住。",
    ),
    TruthStatement(
        id="fish_breathe_lungs",
        statement="大多数鱼主要靠肺在水里呼吸。",
        answer=False,
        explanation="是假的，大多数鱼主要靠鳃呼吸。",
    ),
    TruthStatement(
        id="plants_need_no_light",
        statement="绿色植物完全不需要光也能一直正常生长。",
        answer=False,
        explanation="是假的，很多绿色植物需要光进行光合作用。",
    ),
    TruthStatement(
        id="human_three_eyes",
        statement="人类通常有三只眼睛。",
        answer=False,
        explanation="是假的，人类通常有两只眼睛。",
    ),
    TruthStatement(
        id="winter_hotter",
        statement="在中国北方，冬天通常比夏天更炎热。",
        answer=False,
        explanation="是假的，北方冬天通常比夏天冷。",
    ),
    TruthStatement(
        id="square_three_sides",
        statement="正方形有三条边。",
        answer=False,
        explanation="是假的，正方形有四条边。",
    ),
    TruthStatement(
        id="one_hour_100_minutes",
        statement="一小时等于一百分钟。",
        answer=False,
        explanation="是假的，一小时等于六十分钟。",
    ),
    TruthStatement(
        id="week_eight_days",
        statement="一周通常有八天。",
        answer=False,
        explanation="是假的，一周通常有七天。",
    ),
    TruthStatement(
        id="sun_rises_west",
        statement="太阳通常从西边升起。",
        answer=False,
        explanation="是假的，太阳通常从东方升起。",
    ),
    TruthStatement(
        id="boiled_water_ice",
        statement="水烧开后会立刻变成冰。",
        answer=False,
        explanation="是假的，水烧开会变成热水和水蒸气，不会立刻结冰。",
    ),
    TruthStatement(
        id="all_birds_fly",
        statement="所有鸟都会飞。",
        answer=False,
        explanation="是假的，企鹅、鸵鸟等鸟类不会飞。",
    ),
    TruthStatement(
        id="elephant_tiny",
        statement="成年大象通常比家猫小。",
        answer=False,
        explanation="是假的，成年大象通常远比家猫大。",
    ),
    TruthStatement(
        id="rain_upwards",
        statement="雨通常从地面往天空落。",
        answer=False,
        explanation="是假的，雨通常从云层落向地面。",
    ),
]


class TruthQuizAgent(BaseAgent):
    id = TRUTH_QUIZ_AGENT_ID
    name = "真假判断"
    description = "语音真假判断小游戏，随机给出常识陈述并判断用户回答。"
    trigger_examples = ("玩真假判断", "来个真假游戏", "判断一下真假")

    def can_enter(self, text: str, context: AgentContext) -> bool:
        normalized = _normalize_text(text)
        return any(re.search(pattern, normalized) for pattern in ENTRY_PATTERNS)

    async def start(self, text: str, context: AgentContext) -> AgentResult:
        state, response = _next_statement([], score=0, round_count=0)
        return _truth_result(text=response, state=state, finished=False)

    async def handle(
        self,
        text: str,
        state: dict,
        context: AgentContext,
    ) -> AgentResult:
        if _is_exit(text):
            score = int(state.get("score") or 0)
            rounds = int(state.get("round_count") or 0)
            return _truth_result(
                text=f"好的，退出真假判断。你答对 {score} 题，共玩了 {rounds} 题。",
                state=None,
                finished=True,
            )

        if state.get("awaiting_next"):
            if _is_next(text):
                next_state, response = _next_statement(
                    list(state.get("used_ids") or []),
                    score=int(state.get("score") or 0),
                    round_count=int(state.get("round_count") or 0),
                )
                return _truth_result(text=response, state=next_state, finished=False)
            return _truth_result(
                text="想继续就说再来一题；不玩了可以说退出真假。",
                state=state,
                finished=False,
            )

        answer = _normalize_answer(text)
        if answer is None:
            return _truth_result(
                text="请回答真的或假的。",
                state=state,
                finished=False,
            )

        current = _find_statement(str(state.get("current_id") or ""))
        if current is None:
            next_state, response = _next_statement(
                list(state.get("used_ids") or []),
                score=int(state.get("score") or 0),
                round_count=int(state.get("round_count") or 0),
            )
            return _truth_result(text=response, state=next_state, finished=False)

        correct = answer == current.answer
        score = int(state.get("score") or 0) + (1 if correct else 0)
        next_state = {
            **state,
            "score": score,
            "awaiting_next": True,
        }
        prefix = "答对啦。" if correct else "这题答错啦。"
        return _truth_result(
            text=f"{prefix}{current.explanation}还要再来一题吗？",
            state=next_state,
            finished=False,
        )


def _next_statement(
    used_ids: list[str],
    *,
    score: int,
    round_count: int,
) -> tuple[dict, str]:
    candidates = [item for item in STATEMENTS if item.id not in used_ids]
    if not candidates:
        used_ids = []
        candidates = STATEMENTS[:]
    statement = random.choice(candidates)
    next_used = [*used_ids, statement.id]
    state = {
        "agent_id": TRUTH_QUIZ_AGENT_ID,
        "current_id": statement.id,
        "used_ids": next_used,
        "score": score,
        "round_count": round_count + 1,
        "awaiting_next": False,
    }
    return state, f"真假判断：{statement.statement}请回答真的或假的。"


def _truth_result(
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


def _find_statement(statement_id: str) -> TruthStatement | None:
    for item in STATEMENTS:
        if item.id == statement_id:
            return item
    return None


def _normalize_answer(text: str) -> bool | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    if any(word in normalized for word in FALSE_WORDS):
        return False
    if any(word in normalized for word in TRUE_WORDS):
        return True
    return None


def _is_exit(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in EXIT_WORDS)


def _is_next(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(word in normalized for word in NEXT_WORDS)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def create_agent() -> BaseAgent:
    return TruthQuizAgent()
