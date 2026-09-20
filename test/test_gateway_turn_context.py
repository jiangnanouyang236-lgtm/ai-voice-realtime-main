from gateway.turn_context import build_turn_ids, build_turn_trace


def test_build_turn_ids_defaults_round_trace_and_playback_ids():
    ids = build_turn_ids({}, "session-1", 3)

    assert ids == {
        "trace_id": "session-1:3",
        "round_id": "session-1:3",
        "playback_id": "session-1:3:playback",
    }


def test_build_turn_ids_preserves_client_supplied_ids():
    ids = build_turn_ids(
        {
            "round_id": "round-from-client",
            "trace_id": "trace-from-client",
            "playback_id": "playback-from-client",
        },
        "session-1",
        3,
    )

    assert ids == {
        "trace_id": "trace-from-client",
        "round_id": "round-from-client",
        "playback_id": "playback-from-client",
    }


def test_build_turn_trace_uses_trace_id_as_round_id_and_defaults_playback_id():
    trace = build_turn_trace(
        {},
        {"robot_id": "robot-1", "bot_id": "xiaowen", "bot_name": "Xiao Wen"},
        trace_id="session-1:3",
        round_id=None,
        playback_id=None,
        round_seq=3,
    )

    assert trace == {
        "trace_id": "session-1:3",
        "round_seq": 3,
        "round_id": "session-1:3",
        "playback_id": "session-1:3:playback",
        "robot_id": "robot-1",
        "bot_id": "xiaowen",
        "bot_name": "Xiao Wen",
    }


def test_build_turn_trace_preserves_explicit_round_and_playback_ids():
    trace = build_turn_trace(
        {"playback_id": "from-payload"},
        {"robot_id": "robot-1"},
        trace_id="trace-external",
        round_id="round-explicit",
        playback_id="playback-explicit",
        round_seq=7,
    )

    assert trace["trace_id"] == "trace-external"
    assert trace["round_seq"] == 7
    assert trace["round_id"] == "round-explicit"
    assert trace["playback_id"] == "playback-explicit"
    assert trace["robot_id"] == "robot-1"


def test_build_turn_trace_uses_payload_playback_when_no_explicit_playback_id():
    trace = build_turn_trace(
        {"playback_id": "payload-playback"},
        None,
        trace_id="session-1:4",
        round_id=None,
        playback_id=None,
        round_seq=4,
    )

    assert trace["round_id"] == "session-1:4"
    assert trace["playback_id"] == "payload-playback"
