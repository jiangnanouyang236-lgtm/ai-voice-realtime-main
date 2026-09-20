import unittest

from llm.workflow_protocol import (
    WorkflowValidationError,
    tool_schemas_from_openai_tools,
    validate_workflow_plan,
)


WEATHER_TOOL = "websearch__search"
MOVE_TOOL = "robot_remote__move_robot"
CALL_TOOL = "robot_remote__call_video"


def _schemas():
    return {
        WEATHER_TOOL: {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        MOVE_TOOL: {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["forward", "right"]}},
            "required": ["action"],
            "additionalProperties": False,
        },
        CALL_TOOL: {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    }


def _tool_step(step_id, tool_name, arguments, *, depends_on=None, terminal=False, when=None):
    step = {
        "id": step_id,
        "type": "tool",
        "intent": f"执行 {tool_name}",
        "tool_name": tool_name,
        "arguments": arguments,
        "depends_on": list(depends_on or []),
        "failure_policy": "abort",
        "terminal": terminal,
    }
    if when is not None:
        step["when"] = when
    return step


def _respond(step_id="step_2", *, depends_on=None, vision="none"):
    return {
        "id": step_id,
        "type": "respond",
        "intent": "向用户汇总结果",
        "depends_on": list(depends_on or []),
        "failure_policy": "partial_response",
        "terminal": False,
        "vision": vision,
    }


def _plan(steps):
    return {"version": 1, "goal": "完成用户的复合请求", "steps": steps}


class ComplexWorkflowProtocolTest(unittest.TestCase):
    def validate(self, steps):
        return validate_workflow_plan(
            _plan(steps),
            tool_schemas=_schemas(),
            terminal_tool_names={CALL_TOOL},
        )

    def assert_error(self, code, steps):
        with self.assertRaises(WorkflowValidationError) as caught:
            self.validate(steps)
        self.assertEqual(code, caught.exception.code)

    def test_weather_and_vision_plan_is_valid(self):
        plan = self.validate(
            [
                _tool_step("step_1", WEATHER_TOOL, {"query": "青岛今天天气"}),
                _respond(depends_on=["step_1"], vision="latest_required"),
            ]
        )

        self.assertEqual(["step_1", "step_2"], [step.id for step in plan.steps])
        self.assertFalse(plan.requires_playback_barrier)
        self.assertEqual("latest_required", plan.steps[-1].vision)

    def test_terminal_tool_is_recomputed_and_moved_last(self):
        plan = self.validate(
            [
                _tool_step("step_1", CALL_TOOL, {}, terminal=False),
                _tool_step("step_2", WEATHER_TOOL, {"query": "青岛天气"}),
                _respond("step_3", depends_on=["step_2"]),
            ]
        )

        self.assertEqual(["step_2", "step_3", "step_1"], [step.id for step in plan.steps])
        self.assertEqual("step_1", plan.terminal_step_id)
        self.assertTrue(plan.steps[-1].terminal)
        self.assertTrue(plan.requires_playback_barrier)

    def test_model_cannot_mark_non_terminal_tool_as_terminal(self):
        plan = self.validate(
            [
                _tool_step("step_1", WEATHER_TOOL, {"query": "青岛天气"}, terminal=True),
                _respond(depends_on=["step_1"]),
            ]
        )

        self.assertFalse(plan.steps[0].terminal)
        self.assertIsNone(plan.terminal_step_id)

    def test_visual_condition_can_control_tool(self):
        visual = {
            "id": "step_1",
            "type": "vision_analyze",
            "intent": "判断用户是否举手",
            "output_key": "hand_raised",
            "allowed_values": ["yes", "no", "uncertain"],
            "depends_on": [],
            "failure_policy": "abort",
            "terminal": False,
        }
        move = _tool_step(
            "step_2",
            MOVE_TOOL,
            {"action": "forward"},
            depends_on=["step_1"],
            when={"source": "step_1.hand_raised", "operator": "equals", "value": "yes"},
        )

        plan = self.validate([visual, move, _respond("step_3", depends_on=["step_1", "step_2"])])

        self.assertEqual("step_1", plan.steps[1].condition.source_step_id)

    def test_cycle_is_rejected(self):
        self.assert_error(
            "dependency_cycle",
            [
                _tool_step("step_1", WEATHER_TOOL, {"query": "天气"}, depends_on=["step_2"]),
                _respond(depends_on=["step_1"]),
            ],
        )

    def test_unknown_tool_is_rejected(self):
        self.assert_error(
            "unknown_tool",
            [_tool_step("step_1", "unknown__tool", {}), _respond(depends_on=["step_1"])],
        )

    def test_robot_identity_from_model_is_rejected_recursively(self):
        schemas = _schemas()
        schemas[MOVE_TOOL] = {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "context": {"type": "object", "properties": {"robot_id": {"type": "string"}}},
            },
            "required": ["action"],
        }
        with self.assertRaises(WorkflowValidationError) as caught:
            validate_workflow_plan(
                _plan(
                    [
                        _tool_step(
                            "step_1",
                            MOVE_TOOL,
                            {"action": "forward", "context": {"robot_id": "wrong"}},
                        ),
                        _respond(depends_on=["step_1"]),
                    ]
                ),
                tool_schemas=schemas,
                terminal_tool_names={CALL_TOOL},
            )
        self.assertEqual("forbidden_tool_arguments", caught.exception.code)

    def test_tool_schema_is_enforced(self):
        self.assert_error(
            "tool_schema_mismatch",
            [_tool_step("step_1", WEATHER_TOOL, {}), _respond(depends_on=["step_1"])],
        )

    def test_exactly_one_response_is_required(self):
        self.assert_error(
            "invalid_response_count",
            [_tool_step("step_1", WEATHER_TOOL, {"query": "天气"})],
        )
        self.assert_error(
            "invalid_response_count",
            [_respond("step_1"), _respond("step_2")],
        )

    def test_response_must_depend_on_all_result_steps(self):
        self.assert_error(
            "response_missing_dependencies",
            [
                _tool_step("step_1", WEATHER_TOOL, {"query": "青岛天气"}),
                _tool_step("step_2", MOVE_TOOL, {"action": "forward"}),
                _respond("step_3", depends_on=["step_1"]),
            ],
        )

    def test_multiple_terminal_tools_are_rejected(self):
        self.assert_error(
            "multiple_terminal_steps",
            [
                _tool_step("step_1", CALL_TOOL, {}),
                _tool_step("step_2", CALL_TOOL, {}),
                _respond("step_3"),
            ],
        )

    def test_terminal_tool_cannot_have_dependents(self):
        self.assert_error(
            "terminal_has_dependents",
            [
                _tool_step("step_1", CALL_TOOL, {}),
                _respond(depends_on=["step_1"]),
            ],
        )

    def test_condition_must_reference_declared_visual_output(self):
        self.assert_error(
            "invalid_condition_source",
            [
                _tool_step("step_1", WEATHER_TOOL, {"query": "天气"}),
                _tool_step(
                    "step_2",
                    MOVE_TOOL,
                    {"action": "forward"},
                    depends_on=["step_1"],
                    when={"source": "step_1.hand_raised", "operator": "equals", "value": "yes"},
                ),
                _respond("step_3", depends_on=["step_1", "step_2"]),
            ],
        )

    def test_more_than_eight_steps_are_rejected(self):
        steps = [
            _tool_step(f"step_{index}", WEATHER_TOOL, {"query": str(index)})
            for index in range(1, 9)
        ]
        steps.append(_respond("step_9", depends_on=[step["id"] for step in steps]))
        self.assert_error("invalid_step_count", steps)

    def test_openai_tool_schema_extraction_ignores_invalid_entries(self):
        schemas = tool_schemas_from_openai_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": WEATHER_TOOL,
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                {"type": "function", "function": {"name": "missing_schema", "parameters": None}},
                {"type": "not_function"},
            ]
        )

        self.assertEqual([WEATHER_TOOL], list(schemas))


if __name__ == "__main__":
    unittest.main()
