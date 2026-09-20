import unittest

from llm.tool_router import (
    build_tool_latency_route,
    build_whole_session_exit_response,
    detect_required_tool_category,
    infer_deterministic_singing_tool_args,
    is_xiaowen_profile,
    parse_llm_router_decision,
    router_category_to_tool_prefix,
    router_progress_text,
)
from voice_quick_replies import quick_reply_pool, robot_action_phrase_pool


def _tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }


class ToolRouterV2Test(unittest.TestCase):
    def test_parse_router_decision_accepts_whitelist_outputs(self):
        self.assertEqual(("exit", None), parse_llm_router_decision("E"))
        self.assertEqual(("chat", None), parse_llm_router_decision("C"))
        self.assertEqual(("tool", "websearch"), parse_llm_router_decision("W"))
        self.assertEqual(("tool", "robot"), parse_llm_router_decision("R"))
        self.assertEqual(("tool", "singing"), parse_llm_router_decision("S"))
        self.assertEqual(("tool", "task"), parse_llm_router_decision("T"))
        self.assertEqual(("tool", "utils"), parse_llm_router_decision("U"))
        self.assertEqual(("tool", "complex"), parse_llm_router_decision("X"))
        self.assertEqual(("exit", None), parse_llm_router_decision("exit"))
        self.assertEqual(("chat", None), parse_llm_router_decision("1"))
        self.assertEqual(("tool", "websearch"), parse_llm_router_decision("2:websearch"))
        self.assertEqual(("tool", "robot"), parse_llm_router_decision("2:robot"))
        self.assertEqual(("tool", "singing"), parse_llm_router_decision("2:singing"))
        self.assertEqual(("tool", "task"), parse_llm_router_decision("2:task"))
        self.assertEqual(("tool", "utils"), parse_llm_router_decision("2:utils"))
        self.assertEqual(("tool", "complex"), parse_llm_router_decision("2:complex"))

    def test_parse_router_decision_rejects_non_whitelist_outputs(self):
        self.assertIsNone(parse_llm_router_decision(""))
        self.assertIsNone(parse_llm_router_decision("2:weather"))
        self.assertIsNone(parse_llm_router_decision("2:unknown"))
        self.assertIsNone(parse_llm_router_decision("需要工具"))
        self.assertIsNone(parse_llm_router_decision("W 因为需要搜索"))
        self.assertIsNone(parse_llm_router_decision("1 因为这是闲聊"))

    def test_router_category_metadata_maps_to_tool_prefix_and_progress_text(self):
        self.assertEqual("websearch.", router_category_to_tool_prefix("websearch"))
        self.assertEqual("robot_remote.", router_category_to_tool_prefix("robot"))
        self.assertEqual("singing_remote.", router_category_to_tool_prefix("singing"))
        self.assertEqual("robots_task_service.", router_category_to_tool_prefix("task"))
        self.assertEqual("utils_remote.", router_category_to_tool_prefix("utils"))
        self.assertIsNone(router_category_to_tool_prefix("complex"))
        self.assertIn(router_progress_text("websearch"), quick_reply_pool("tool.websearch"))
        self.assertIn(router_progress_text("robot"), robot_action_phrase_pool("generic"))
        self.assertEqual("好～我准备一下。", router_progress_text("singing"))
        self.assertIn(router_progress_text("task"), quick_reply_pool("tool.task"))
        self.assertIn(router_progress_text("utils"), quick_reply_pool("tool.utils"))
        self.assertIsNone(router_progress_text("complex"))

    def test_whole_session_exit_matches_direct_exit_only(self):
        response = build_whole_session_exit_response("退出吧")

        self.assertIsNotNone(response)
        self.assertTrue(response.endswith("[EXIT]"))
        self.assertIsNotNone(build_whole_session_exit_response("退下吧。"))
        self.assertIsNotNone(build_whole_session_exit_response("再见！"))
        self.assertIsNotNone(build_whole_session_exit_response("cya"))
        self.assertIsNone(build_whole_session_exit_response("退出这个话题"))
        self.assertIsNone(build_whole_session_exit_response("退出小游戏"))
        self.assertIsNone(build_whole_session_exit_response("换个话题"))

    def test_xiaowen_weather_without_city_defaults_to_qingdao_websearch(self):
        route = build_tool_latency_route(
            "帮我查一下天气。",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("tool", route.kind)
        self.assertIn(route.progress_text, quick_reply_pool("tool.weather"))
        self.assertNotIn("青岛", route.progress_text)
        self.assertEqual("websearch.", route.tool_prefix)
        self.assertEqual("websearch__bailian_web_search", route.selected_tool_name)
        self.assertEqual("websearch", route.category)
        self.assertTrue(route.require_tool_call)
        self.assertEqual("帮我查一下青岛今天的天气。", route.model_text)

    def test_xiaowen_weather_with_city_keeps_user_query(self):
        route = build_tool_latency_route(
            "今天深圳天气怎么样？",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("tool", route.kind)
        self.assertIn(route.progress_text, quick_reply_pool("tool.weather"))
        self.assertNotIn("深圳", route.progress_text)
        self.assertEqual("websearch.", route.tool_prefix)
        self.assertEqual("websearch__bailian_web_search", route.selected_tool_name)
        self.assertTrue(route.require_tool_call)
        self.assertIsNone(route.model_text)

    def test_weather_self_correction_never_announces_either_location(self):
        route = build_tool_latency_route(
            "帮我查一下，呃，青岛的，呃，或者帮我查一下北京的天气。",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen",
            bot_name="温妮",
            session_id="weather-self-correction",
        )

        self.assertEqual("tool", route.kind)
        self.assertIn(route.progress_text, quick_reply_pool("tool.weather"))
        self.assertNotIn("青岛", route.progress_text)
        self.assertNotIn("北京", route.progress_text)
        self.assertIsNone(route.model_text)

    def test_xiaowen_gold_price_selects_websearch(self):
        route = build_tool_latency_route(
            "我想知道今天黄金的价格。",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("websearch.", route.tool_prefix)
        self.assertEqual("websearch__bailian_web_search", route.selected_tool_name)
        self.assertIn(route.progress_text, quick_reply_pool("tool.websearch"))
        self.assertNotIn("黄金", route.progress_text)

    def test_xiaowen_time_query_selects_utils_tool(self):
        route = build_tool_latency_route(
            "今天星期几？",
            [_tool("utils_remote__get_current_time")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("utils_remote.", route.tool_prefix)
        self.assertEqual("utils_remote__get_current_time", route.selected_tool_name)
        self.assertEqual("utils", route.category)
        self.assertIn(route.progress_text, quick_reply_pool("tool.utils"))

    def test_xiaowen_reminder_query_selects_task_service(self):
        route = build_tool_latency_route(
            "明天早上八点提醒我开会。",
            [_tool("robots_task_service__create_task")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("robots_task_service.", route.tool_prefix)
        self.assertEqual("robots_task_service__create_task", route.selected_tool_name)
        self.assertIn(route.progress_text, quick_reply_pool("tool.task"))
        self.assertNotIn("明天早上八点", route.progress_text)

    def test_xiaowen_robot_motion_selects_robot_tool(self):
        route = build_tool_latency_route(
            "你往前走一下。",
            [_tool("robot_remote__move_robot")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("robot_remote.", route.tool_prefix)
        self.assertEqual("robot_remote__move_robot", route.selected_tool_name)
        self.assertEqual("robot", route.category)
        self.assertIn(route.progress_text, robot_action_phrase_pool("move_forward"))

    def test_xiaowen_robot_interaction_actions_select_robot_tool(self):
        cases = [
            ("和我打个招呼吧。", "greet"),
            ("来握个手吧。", "handshake"),
            ("庆祝一下，喝个彩。", "cheer"),
        ]

        for text, expected_progress in cases:
            with self.subTest(text=text):
                route = build_tool_latency_route(
                    text,
                    [_tool("robot_remote__move_robot")],
                    bot_id="xiaowen",
                    bot_name="温妮",
                )

                self.assertEqual("tool", route.kind)
                self.assertEqual("robot_remote.", route.tool_prefix)
                self.assertEqual("robot_remote__move_robot", route.selected_tool_name)
                self.assertIn(route.progress_text, robot_action_phrase_pool(expected_progress))

    def test_stop_music_selects_robot_cancel_tool(self):
        route = build_tool_latency_route(
            "把音乐关掉",
            [_tool("robot_remote__cancel_robot_task"), _tool("robot_remote__move_robot")],
            bot_id="default",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("robot_remote__cancel_robot_task", route.selected_tool_name)
        self.assertEqual("robot", route.category)

    def test_xiaowen_human_people_lookup_selects_environment_understanding(self):
        route = build_tool_latency_route(
            "你帮我看看周围有没有长头发的人。",
            [_tool("robot_remote__understand_environment")],
            bot_id="xiaowen-human",
            bot_name="小文-机器人",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("robot_remote.", route.tool_prefix)
        self.assertEqual("robot_remote__understand_environment", route.selected_tool_name)
        self.assertTrue(route.require_tool_call)

    def test_xiaowen_human_confirmation_after_environment_question_selects_environment_tool(self):
        route = build_tool_latency_route(
            "可以了。",
            [_tool("robot_remote__understand_environment")],
            bot_id="xiaowen-human",
            bot_name="小文-机器人",
            messages=[
                {"role": "user", "content": "长发"},
                {
                    "role": "assistant",
                    "content": "需要我帮您识别一下周围有没有长头发的人吗？如果是的话，我可以调用视觉识别功能来查看环境。",
                },
                {"role": "user", "content": "可以了。"},
            ],
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("robot_remote__understand_environment", route.selected_tool_name)
        self.assertTrue(route.require_tool_call)

    def test_xiaowen_human_confirmation_without_pending_environment_question_stays_chat(self):
        route = build_tool_latency_route(
            "好的。",
            [_tool("robot_remote__understand_environment")],
            bot_id="xiaowen-human",
            bot_name="小文-机器人",
            messages=[
                {"role": "user", "content": "讲个故事"},
                {"role": "assistant", "content": "好呀，我给你讲一个小故事。"},
                {"role": "user", "content": "好的。"},
            ],
        )

        self.assertEqual("legacy", route.kind)
        self.assertIsNone(route.selected_tool_name)

    def test_xiaowen_story_uses_chat_route(self):
        route = build_tool_latency_route(
            "讲个故事。",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.progress_text)
        self.assertFalse(route.require_tool_call)

    def test_xiaowen_weather_story_uses_main_story_intent(self):
        route = build_tool_latency_route(
            "讲一个今天青岛天气的故事。",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.progress_text)
        self.assertFalse(route.require_tool_call)

    def test_xiaowen_tool_keyword_explanation_uses_chat_route(self):
        route = build_tool_latency_route(
            "解释一下黄金价格为什么会波动。",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.progress_text)
        self.assertFalse(route.require_tool_call)

    def test_xiaowen_robot_capability_question_uses_chat_route(self):
        route = build_tool_latency_route(
            "你会跳舞吗？",
            [_tool("robot_remote__dance")],
            bot_id="xiaowen",
            bot_name="温妮",
        )

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.progress_text)
        self.assertFalse(route.require_tool_call)

    def test_default_uses_shared_route(self):
        route = build_tool_latency_route(
            "今天青岛天气怎么样？",
            [_tool("websearch__bailian_web_search")],
            bot_id="default",
            bot_name="默认助手",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("websearch__bailian_web_search", route.selected_tool_name)
        self.assertTrue(route.progress_text)
        self.assertTrue(route.require_tool_call)

    def test_xiaowen_human_profile_uses_fast_route(self):
        route = build_tool_latency_route(
            "今天青岛天气怎么样？",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen-human",
            bot_name="小文-机器人",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("websearch__bailian_web_search", route.selected_tool_name)

    def test_xiaowen_test_profile_is_enabled_for_router_classifier(self):
        self.assertTrue(is_xiaowen_profile("xiaowen-test", "小文-测试机"))

    def test_wzk_prefix_profile_is_enabled_for_router_classifier(self):
        self.assertTrue(is_xiaowen_profile("wzk-new-bot", "任意名称"))

    def test_default_profile_uses_same_router_policy(self):
        self.assertTrue(is_xiaowen_profile("default", "默认助手"))

    def test_conditional_two_robot_tools_use_complex_route(self):
        route = build_tool_latency_route(
            "先看看房间有没有宠物，有的话再播放轻音乐。",
            [
                _tool("robot_remote__detect_pet"),
                _tool("robot_remote__play_music"),
            ],
            bot_id="default",
            bot_name="默认助手",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("complex", route.category)
        self.assertIsNone(route.selected_tool_name)
        self.assertFalse(route.require_tool_call)

    def test_repeated_same_robot_tool_actions_use_complex_route(self):
        for text in ("先左转再右转", "先向前走，然后向后退"):
            with self.subTest(text=text):
                route = build_tool_latency_route(
                    text,
                    [_tool("robot_remote__move_robot")],
                    bot_id="default",
                    bot_name="默认助手",
                )

                self.assertEqual("tool", route.kind)
                self.assertEqual("complex", route.category)
                self.assertIsNone(route.selected_tool_name)

    def test_no_tools_preserves_explicit_complex_category(self):
        self.assertEqual(
            "complex",
            detect_required_tool_category("先查现在几点，然后提醒我睡觉"),
        )

    def test_unsupported_home_control_never_uses_another_robot_tool(self):
        for text in ("把空调打开", "把灯调亮一点", "关闭窗帘"):
            with self.subTest(text=text):
                route = build_tool_latency_route(
                    text,
                    [_tool("robot_remote__move_robot"), _tool("robot_remote__dance")],
                    bot_id="default",
                    bot_name="默认助手",
                )

                self.assertEqual("chat", route.kind)
                self.assertEqual("unavailable_robot_capability", route.category)
                self.assertEqual("deterministic_unavailable", route.source)

    def test_unsupported_robot_parameters_never_degrade_to_fixed_actions(self):
        tools = [
            _tool("robot_remote__move_robot"),
            _tool("robot_remote__dance"),
            _tool("robot_remote__call_video"),
            _tool("singing_remote__play_song"),
        ]
        for text in ("跳一段街舞", "往前走五米", "给妈妈打视频", "把音乐音量调大"):
            with self.subTest(text=text):
                route = build_tool_latency_route(text, tools, bot_id="default")
                self.assertEqual("chat", route.kind)
                self.assertEqual("deterministic_unavailable", route.source)

    def test_negated_robot_start_does_not_call_robot_tool(self):
        tools = [_tool("robot_remote__dance"), _tool("robot_remote__patrol")]
        for text in ("不用跳舞", "不要开始巡逻", "别给我跳", "先不移动"):
            with self.subTest(text=text):
                route = build_tool_latency_route(text, tools, bot_id="default")
                self.assertEqual("chat", route.kind)
                self.assertEqual("deterministic_safety", route.source)

    def test_colloquial_fixed_dance_selects_dance_tool(self):
        route = build_tool_latency_route(
            "给大伙整段舞",
            [_tool("robot_remote__dance")],
            bot_id="default",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("robot_remote__dance", route.selected_tool_name)

    def test_named_or_generic_song_selects_singing_playback(self):
        for text in ("唱首爱你我听听", "给我唱一首歌", "来首歌"):
            with self.subTest(text=text):
                route = build_tool_latency_route(
                    text,
                    [_tool("singing_remote__play_song")],
                    bot_id="default",
                )

                self.assertEqual("tool", route.kind)
                self.assertEqual("singing_remote__play_song", route.selected_tool_name)
                self.assertEqual("singing", route.category)
                self.assertEqual("好～我准备一下。", route.progress_text)

    def test_song_capability_queries_select_catalog_without_playback(self):
        for text, expected_query in (
            ("你会唱什么歌？", ""),
            ("你会唱王心凌的吗？", "王心凌"),
            ("你会唱爱你吗？", "爱你"),
        ):
            with self.subTest(text=text):
                route = build_tool_latency_route(
                    text,
                    [_tool("singing_remote__list_songs")],
                    bot_id="default",
                )

                self.assertEqual("tool", route.kind)
                self.assertEqual("singing", route.category)
                self.assertEqual("singing_remote__list_songs", route.selected_tool_name)
                self.assertIsNone(route.progress_text)
                self.assertEqual(
                    {"query": expected_query, "limit": 5},
                    infer_deterministic_singing_tool_args(text, route.selected_tool_name),
                )

    def test_singing_then_robot_action_stays_complex(self):
        route = build_tool_latency_route(
            "先唱首爱你，然后跳个舞",
            [_tool("singing_remote__play_song"), _tool("robot_remote__dance")],
            bot_id="default",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("complex", route.category)

    def test_singing_chat_and_negative_requests_do_not_start_song(self):
        tools = [_tool("singing_remote__play_song"), _tool("singing_remote__list_songs")]
        for text in ("你唱歌好听吗", "别唱了", "不要唱歌"):
            with self.subTest(text=text):
                route = build_tool_latency_route(text, tools, bot_id="default")
                self.assertEqual("chat", route.kind)

    def test_singing_request_without_singing_mcp_is_explicitly_unavailable(self):
        route = build_tool_latency_route(
            "唱首爱你",
            [_tool("robot_remote__dance")],
            bot_id="default",
        )

        self.assertEqual("chat", route.kind)
        self.assertEqual("unavailable_singing", route.category)

    def test_ambiguous_single_word_side_effect_stays_chat_without_context(self):
        tools = [
            _tool("robot_remote__move_robot"),
            _tool("robot_remote__return_to_charge"),
            _tool("robot_remote__cancel_robot_task"),
        ]
        for text in ("走", "回", "取消掉", "不要了", "继续", "再来一次"):
            with self.subTest(text=text):
                route = build_tool_latency_route(text, tools, bot_id="default")
                self.assertEqual("chat", route.kind)
                self.assertEqual("deterministic_safety", route.source)

    def test_incomplete_reminder_creation_asks_for_clarification(self):
        tools = [_tool("robots_task_service__create_reminder")]
        for text in ("提醒我", "明天提醒我", "提醒我喝水"):
            with self.subTest(text=text):
                route = build_tool_latency_route(text, tools, bot_id="default")
                self.assertEqual("chat", route.kind)
                self.assertEqual("deterministic_safety", route.source)

        complete = build_tool_latency_route("明早七点提醒我起床", tools, bot_id="default")
        self.assertEqual("tool", complete.kind)
        self.assertEqual("task", complete.category)

    def test_capability_and_explanation_questions_stay_chat(self):
        tools = [
            _tool("robot_remote__dance"),
            _tool("robot_remote__call_video"),
            _tool("websearch__bailian_web_search"),
        ]
        for text in (
            "你具备跳舞这个能力吗？",
            "你可以发起视频通话吗？",
            "什么是机器人巡检？",
            "金价通常受哪些因素影响？",
        ):
            with self.subTest(text=text):
                route = build_tool_latency_route(text, tools, bot_id="default")
                self.assertEqual("chat", route.kind)

    def test_current_visual_request_is_not_downgraded_to_capability_chat(self):
        route = build_tool_latency_route(
            "你能从当前画面描述一下我的表情吗？",
            [_tool("robot_remote__understand_environment")],
            bot_id="default",
        )

        self.assertEqual("legacy", route.kind)

    def test_english_and_mixed_language_multi_tool_requests_are_complex(self):
        tools = [
            _tool("websearch__bailian_web_search"),
            _tool("utils_remote__get_current_time"),
            _tool("robots_task_service__create_reminder"),
        ]
        for text in (
            "What are the current time and weather?",
            "Check the weather, then tell me the current time.",
            "先 check 当前时间，然后 remind me 去睡觉。",
        ):
            with self.subTest(text=text):
                route = build_tool_latency_route(text, tools, bot_id="default")
                self.assertEqual("tool", route.kind)
                self.assertEqual("complex", route.category)

    def test_stop_wording_is_not_mistaken_for_negated_future_action(self):
        route = build_tool_latency_route(
            "马上停住，不要继续移动。",
            [_tool("robot_remote__cancel_robot_task")],
            bot_id="default",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("robot", route.category)
        self.assertEqual("robot_remote__cancel_robot_task", route.selected_tool_name)

    def test_xiaowen_test_profile_uses_fast_chat_route_for_known_chat_intent(self):
        route = build_tool_latency_route(
            "讲个故事。",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen-test",
            bot_name="小文-测试机",
        )

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.selected_tool_name)
        self.assertFalse(route.require_tool_call)

    def test_xiaowen_test_profile_still_routes_tools(self):
        route = build_tool_latency_route(
            "今天青岛天气怎么样？",
            [_tool("websearch__bailian_web_search")],
            bot_id="xiaowen-test",
            bot_name="小文-测试机",
        )

        self.assertEqual("tool", route.kind)
        self.assertEqual("websearch__bailian_web_search", route.selected_tool_name)

    def test_wzk_prefix_profile_uses_fast_chat_route(self):
        route = build_tool_latency_route(
            "讲个故事。",
            [_tool("websearch__bailian_web_search")],
            bot_id="wzk-companion",
            bot_name="任意名称",
        )

        self.assertEqual("chat", route.kind)
        self.assertIsNone(route.selected_tool_name)


if __name__ == "__main__":
    unittest.main()
