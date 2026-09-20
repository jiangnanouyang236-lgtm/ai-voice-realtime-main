import asyncio
import json
import unittest

from llm.workflow_planner import (
    ComplexWorkflowPlanner,
    PLANNER_MAX_TOKENS,
    WorkflowPlanningError,
    build_planner_messages,
    recent_complete_turns,
)


WEATHER_TOOL = "websearch__search"
CALL_TOOL = "robot_remote__call_video"


def _schemas():
    return {
        WEATHER_TOOL: {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        CALL_TOOL: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def _valid_plan():
    return {
        "version": 1,
        "goal": "查询天气后呼叫视频",
        "steps": [
            {
                "id": "step_1",
                "type": "tool",
                "intent": "查询青岛天气",
                "tool_name": WEATHER_TOOL,
                "arguments": {"query": "青岛今天天气"},
                "depends_on": [],
                "failure_policy": "partial_response",
                "terminal": False,
            },
            {
                "id": "step_2",
                "type": "respond",
                "intent": "播报天气并说明即将呼叫视频",
                "depends_on": ["step_1"],
                "failure_policy": "partial_response",
                "terminal": False,
                "vision": "none",
            },
            {
                "id": "step_3",
                "type": "tool",
                "intent": "呼叫视频",
                "tool_name": CALL_TOOL,
                "arguments": {},
                "depends_on": [],
                "failure_policy": "abort",
                "terminal": False,
            },
        ],
    }


class FakeGenerator:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, messages, *, temperature, max_tokens):
        self.calls.append(
            {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ComplexWorkflowPlannerTest(unittest.TestCase):
    def make_planner(self, generator):
        return ComplexWorkflowPlanner(
            generate=generator,
            tool_schemas=_schemas(),
            terminal_tool_names={CALL_TOOL},
        )

    def test_valid_plan_uses_temperature_zero_and_is_validated(self):
        generator = FakeGenerator([json.dumps(_valid_plan(), ensure_ascii=False)])
        result = asyncio.run(
            self.make_planner(generator).plan(user_text="查天气，然后拨打视频电话")
        )

        self.assertEqual(1, result.attempts)
        self.assertEqual(0.0, generator.calls[0]["temperature"])
        self.assertEqual(PLANNER_MAX_TOKENS, generator.calls[0]["max_tokens"])
        self.assertEqual(CALL_TOOL, result.plan.steps[-1].tool_name)
        self.assertTrue(result.plan.requires_playback_barrier)

    def test_invalid_json_retries_once_with_error_feedback(self):
        generator = FakeGenerator(
            [
                "```json\n{}\n```",
                json.dumps(_valid_plan(), ensure_ascii=False),
            ]
        )
        result = asyncio.run(self.make_planner(generator).plan(user_text="查天气再打视频"))

        self.assertEqual(2, result.attempts)
        self.assertEqual(2, len(generator.calls))
        retry_message = generator.calls[1]["messages"][-1]["content"]
        self.assertIn("invalid_json", retry_message)
        self.assertNotIn("```json", retry_message)

    def test_schema_failure_retries_with_safe_error_summary(self):
        invalid = _valid_plan()
        invalid["steps"][0]["arguments"] = {}
        generator = FakeGenerator(
            [json.dumps(invalid, ensure_ascii=False), json.dumps(_valid_plan(), ensure_ascii=False)]
        )

        result = asyncio.run(self.make_planner(generator).plan(user_text="查天气再打视频"))

        self.assertEqual(2, result.attempts)
        self.assertIn("tool_schema_mismatch", generator.calls[1]["messages"][-1]["content"])

    def test_two_invalid_attempts_fail_without_returning_a_plan(self):
        generator = FakeGenerator(["not-json", "still-not-json"])

        with self.assertRaises(WorkflowPlanningError) as caught:
            asyncio.run(self.make_planner(generator).plan(user_text="执行复杂任务"))

        self.assertEqual(2, caught.exception.attempts)
        self.assertEqual("invalid_json", caught.exception.last_error_code)
        self.assertEqual(2, len(generator.calls))

    def test_timeout_retries_once_without_exposing_exception_text(self):
        generator = FakeGenerator(
            [
                asyncio.TimeoutError("internal endpoint and secret"),
                json.dumps(_valid_plan(), ensure_ascii=False),
            ]
        )

        result = asyncio.run(self.make_planner(generator).plan(user_text="执行复杂任务"))

        self.assertEqual(2, result.attempts)
        feedback = generator.calls[1]["messages"][-1]["content"]
        self.assertIn("planner_timeout", feedback)
        self.assertNotIn("secret", feedback)

    def test_recent_history_keeps_only_five_complete_turns(self):
        messages = []
        for index in range(1, 8):
            messages.extend(
                [
                    {"role": "user", "content": f"用户{index}"},
                    {"role": "assistant", "content": f"助手{index}"},
                ]
            )
        messages.extend(
            [
                {"role": "tool", "content": "内部工具"},
                {"role": "user", "content": "[系统：工具 websearch 返回结果]\n内部结果"},
                {"role": "user", "content": "未完成的新问题"},
            ]
        )

        history = recent_complete_turns(messages, max_turns=5)

        self.assertEqual(10, len(history))
        self.assertEqual("用户3", history[0]["content"])
        self.assertEqual("助手7", history[-1]["content"])
        self.assertNotIn("未完成的新问题", [item["content"] for item in history])

    def test_new_user_replaces_unanswered_pending_user(self):
        history = recent_complete_turns(
            [
                {"role": "user", "content": "旧的半轮"},
                {"role": "user", "content": "新的问题"},
                {"role": "assistant", "content": "新的回答"},
            ],
            max_turns=5,
        )

        self.assertEqual(
            [
                {"role": "user", "content": "新的问题"},
                {"role": "assistant", "content": "新的回答"},
            ],
            history,
        )

    def test_prompt_explains_weather_plus_outfit_vision_plan(self):
        messages = build_planner_messages(
            user_text="查青岛天气，再看看我穿得合不合适",
            conversation_messages=[],
            tool_schemas=_schemas(),
            terminal_tool_names={CALL_TOOL},
        )

        prompt = messages[0]["content"]
        self.assertIn("查询天气，再评价我的穿搭", prompt)
        self.assertIn("vision=latest_required", prompt)
        self.assertIn("不要额外生成 vision_analyze", prompt)
        self.assertIn("id、type、intent、depends_on、failure_policy、terminal", prompt)
        self.assertIn("respond 步骤还必须包含 vision，且不能包含 arguments 或 text", prompt)
        self.assertIn('"id":"step_1"', prompt)
        self.assertIn('"vision":"latest_required"', prompt)

    def test_non_tool_terminal_hint_is_safely_normalized(self):
        plan = _valid_plan()
        plan["steps"][1]["terminal"] = True
        generator = FakeGenerator([json.dumps(plan, ensure_ascii=False)])

        result = asyncio.run(
            self.make_planner(generator).plan(user_text="查天气，然后拨打视频电话")
        )

        respond = next(step for step in result.plan.steps if step.type == "respond")
        self.assertFalse(respond.terminal)
        self.assertEqual(CALL_TOOL, result.plan.steps[-1].tool_name)

    def test_prompt_contains_capabilities_and_latest_request_without_secrets(self):
        messages = build_planner_messages(
            user_text="看看衣服是否适合今天的天气",
            conversation_messages=[],
            tool_schemas=_schemas(),
            terminal_tool_names={CALL_TOOL},
        )

        self.assertEqual(["system", "user"], [message["role"] for message in messages])
        self.assertIn(WEATHER_TOOL, messages[0]["content"])
        self.assertIn(CALL_TOOL, messages[0]["content"])
        self.assertIn("看看衣服是否适合今天的天气", messages[1]["content"])
        self.assertIn("不要生成 robot_id", messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
