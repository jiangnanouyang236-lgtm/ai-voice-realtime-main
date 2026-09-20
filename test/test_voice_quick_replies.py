import random

from voice_quick_replies import (
    MIN_QUICK_REPLY_POOL_SIZE,
    QUICK_REPLY_POOLS,
    QuickReplySelector,
    robot_action_phrase_pool,
)


def test_every_quick_reply_pool_has_enough_unique_phrases():
    for pool_key, phrases in QUICK_REPLY_POOLS.items():
        assert len(phrases) >= MIN_QUICK_REPLY_POOL_SIZE, pool_key
        assert len(phrases) == len(set(phrases)), pool_key


def test_every_quick_reply_is_short_and_non_redundant():
    forbidden_redundancy = (
        "离开一会儿，我先休息",
        "退出，我先",
        "待机，我先待命",
    )
    for pool_key, phrases in QUICK_REPLY_POOLS.items():
        max_visible_chars = 9 if pool_key == "tool.complex" else 8
        for phrase in phrases:
            visible = phrase.replace("[EXIT]", "")
            assert len(visible) <= max_visible_chars, (pool_key, phrase)
            assert not any(token in visible for token in forbidden_redundancy), (
                pool_key,
                phrase,
            )


def test_every_robot_action_pool_has_at_least_200_unique_phrases():
    actions = (
        "generic",
        "move_forward",
        "move_backward",
        "turn_left",
        "turn_right",
        "stop",
        "greet",
        "handshake",
        "cheer",
        "recharge",
        "video_call",
    )
    for action in actions:
        phrases = robot_action_phrase_pool(action)
        assert len(phrases) >= 200, action
        assert len(phrases) == len(set(phrases)), action


def test_weather_progress_pool_never_mentions_a_location():
    location_tokens = ("青岛", "北京", "上海", "深圳", "广州", "杭州", "南京")
    for phrase in QUICK_REPLY_POOLS["tool.weather"]:
        assert not any(token in phrase for token in location_tokens)


def test_shuffle_bag_does_not_repeat_before_pool_is_exhausted():
    phrases = QUICK_REPLY_POOLS["tool.weather"]
    selector = QuickReplySelector(rng=random.Random(20260812))

    selected = [
        selector.pick("tool.weather", phrases, "session-1") for _ in range(len(phrases))
    ]

    assert len(selected) == len(set(selected)) == len(phrases)
    assert selector.pick("tool.weather", phrases, "session-1") != selected[-1]


def test_shuffle_bag_tracks_sessions_independently():
    phrases = QUICK_REPLY_POOLS["tool.weather"]
    selector = QuickReplySelector(rng=random.Random(7))

    session_a = [
        selector.pick("tool.weather", phrases, "session-a") for _ in range(len(phrases))
    ]
    session_b = [
        selector.pick("tool.weather", phrases, "session-b") for _ in range(len(phrases))
    ]

    assert len(session_a) == len(set(session_a))
    assert len(session_b) == len(set(session_b))
