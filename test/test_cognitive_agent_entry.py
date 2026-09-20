from __future__ import annotations

import unittest

from llm.agents.cognitive import (
    COGNITIVE_AGENT_ID,
    SCORES,
    CognitiveScreeningAgent,
    _risk_level,
    _risk_level_text,
    is_cognitive_entry,
)
from llm.agent_runtime import AgentContext


TEST_CONTEXT = AgentContext(session_id="test-session", model_name="test-model")


class CognitiveAgentEntryTest(unittest.IsolatedAsyncioTestCase):
    async def test_direct_screening_request_starts_questions_immediately(self) -> None:
        agent = CognitiveScreeningAgent()

        result = await agent.start("帮我做认知检测", context=TEST_CONTEXT)

        self.assertFalse(result.finished)
        self.assertIsNotNone(result.state)
        self.assertEqual(result.state["agent_id"], COGNITIVE_AGENT_ID)
        self.assertEqual(result.state["current_index"], 0)
        self.assertIn("小游戏", result.text)
        self.assertIn("一共三题", result.text)
        self.assertIn("第 1 题", result.text)
        self.assertNotIn("筛查", result.text)
        self.assertNotIn("检测", result.text)

    async def test_memory_concern_asks_for_confirmation_before_screening(self) -> None:
        agent = CognitiveScreeningAgent()

        result = await agent.start("我感觉最近记忆有点跟不上了", context=TEST_CONTEXT)

        self.assertFalse(result.finished)
        self.assertEqual(
            result.state,
            {
                "agent_id": COGNITIVE_AGENT_ID,
                "awaiting_confirmation": True,
            },
        )
        self.assertIn("三题小游戏", result.text)
        self.assertIn("要不要", result.text)
        self.assertNotIn("筛查", result.text)
        self.assertNotIn("检测", result.text)
        self.assertNotIn("第 1 题", result.text)

    async def test_confirmation_yes_starts_screening_flow(self) -> None:
        agent = CognitiveScreeningAgent()
        state = {
            "agent_id": COGNITIVE_AGENT_ID,
            "awaiting_confirmation": True,
        }

        result = await agent.handle("好，开始吧", state, context=TEST_CONTEXT)

        self.assertFalse(result.finished)
        self.assertIsNotNone(result.state)
        self.assertEqual(result.state["current_index"], 0)
        self.assertIn("小游戏", result.text)
        self.assertIn("一共三题", result.text)
        self.assertIn("第 1 题", result.text)

    async def test_confirmation_no_exits_agent(self) -> None:
        agent = CognitiveScreeningAgent()
        state = {
            "agent_id": COGNITIVE_AGENT_ID,
            "awaiting_confirmation": True,
        }

        result = await agent.handle("先不用了", state, context=TEST_CONTEXT)

        self.assertTrue(result.finished)
        self.assertIsNone(result.state)
        self.assertIn("不做小游戏", result.text)
        self.assertNotIn("筛查", result.text)
        self.assertNotIn("检测", result.text)

    def test_info_query_does_not_enter_agent(self) -> None:
        self.assertFalse(is_cognitive_entry("认知筛查是什么"))

    def test_memory_concern_is_entry_candidate(self) -> None:
        self.assertTrue(is_cognitive_entry("我感觉最近记忆有点跟不上了"))

    def test_higher_score_means_lower_risk(self) -> None:
        self.assertEqual(SCORES, {"经常": 0, "偶尔": 1, "从不": 2})
        self.assertEqual(_risk_level(0), "attention")
        self.assertEqual(_risk_level(2), "attention")
        self.assertEqual(_risk_level(3), "observe")
        self.assertEqual(_risk_level(4), "observe")
        self.assertEqual(_risk_level(5), "normal")
        self.assertEqual(_risk_level(6), "normal")
        self.assertIn("专业人士", _risk_level_text(0))
        self.assertIn("继续观察", _risk_level_text(3))
        self.assertIn("没有明显", _risk_level_text(6))


if __name__ == "__main__":
    unittest.main()
