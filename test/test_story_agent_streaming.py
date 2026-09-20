from __future__ import annotations

import unittest

from llm.agent_runtime import AgentContext
from llm.agents.story import InteractiveStoryAgent, STORY_AGENT_ID


class StoryAgentStreamingTest(unittest.IsolatedAsyncioTestCase):
    def test_entry_requires_explicit_story_game_intent(self) -> None:
        agent = InteractiveStoryAgent()
        context = AgentContext(session_id="story-session", model_name="qwen3-5-9b")

        self.assertFalse(agent.can_enter("讲个故事", context))
        self.assertFalse(agent.can_enter("讲一个修仙故事", context))
        self.assertTrue(agent.can_enter("我想玩故事接龙", context))
        self.assertTrue(agent.can_enter("我想玩故事续写", context))
        self.assertTrue(agent.can_enter("来个修仙故事接龙", context))

    async def test_start_stream_yields_llm_chunks_and_final_state(self) -> None:
        async def streamer(messages, temperature, max_tokens):
            yield "你走进森林，"
            yield "树后有一束光。你要靠近吗？"

        agent = InteractiveStoryAgent()
        context = AgentContext(
            session_id="story-session",
            model_name="qwen3-5-9b",
            llm_streamer=streamer,
        )

        events = [
            event
            async for event in agent.start_stream("玩一个冒险故事", context)
        ]

        texts = [event.text for event in events if event.text]
        final_results = [event.result for event in events if event.result is not None]
        self.assertEqual(
            [
                "那我们来玩冒险故事接龙吧。你可以自由地说想做什么，想退出就说退出故事。",
                "你走进森林，",
                "树后有一束光。你要靠近吗？",
            ],
            texts,
        )
        self.assertEqual(1, len(final_results))
        result = final_results[0]
        self.assertFalse(result.finished)
        self.assertEqual("你走进森林，树后有一束光。你要靠近吗？", result.text)
        self.assertEqual(STORY_AGENT_ID, result.state["agent_id"])
        self.assertEqual(result.text, result.state["summary"])

    async def test_help_request_explains_flow_without_advancing_story(self) -> None:
        agent = InteractiveStoryAgent()
        context = AgentContext(session_id="story-session", model_name="qwen3-5-9b")
        state = {
            "agent_id": STORY_AGENT_ID,
            "style": "冒险",
            "summary": "你站在森林入口。",
            "turn_count": 1,
            "recent": [{"assistant": "你站在森林入口。"}],
        }

        events = [
            event
            async for event in agent.handle_stream("我该说什么", state, context)
        ]

        texts = [event.text for event in events if event.text]
        final_results = [event.result for event in events if event.result is not None]
        self.assertEqual(1, len(texts))
        self.assertIn("想结束就说退出故事", texts[0])
        self.assertEqual(state, final_results[0].state)
        self.assertFalse(final_results[0].finished)


if __name__ == "__main__":
    unittest.main()
