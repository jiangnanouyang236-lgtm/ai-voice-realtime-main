from gateway.playback_report import (
    build_client_playback_trace_event,
    build_client_playback_summary,
    build_playback_cancel_payload,
    float_from_client_report,
    int_from_client_report,
    request_playback_id,
    request_round_id,
    request_trace_id,
    round_seq_from_trace_id,
)


def test_request_trace_id_can_be_distinct_from_round_id():
    data = {
        "trace_id": "rust-trace-1",
        "playback_id": "session-1:7:playback",
    }

    round_id = request_round_id(data, "session-1", 7)
    trace_id = request_trace_id(data, round_id)
    playback_id = request_playback_id(data, round_id)

    assert round_id == "session-1:7"
    assert trace_id == "rust-trace-1"
    assert playback_id == "session-1:7:playback"


def test_build_playback_cancel_payload_requires_round_id():
    assert build_playback_cancel_payload(None, reason="interrupt") is None
    assert build_playback_cancel_payload({}, reason="interrupt") is None
    assert build_playback_cancel_payload({"round_id": ""}, reason="interrupt") is None

    payload = build_playback_cancel_payload(
        {"round_id": "session-1:3", "playback_id": "session-1:3:playback"},
        reason="new_input",
    )

    assert payload == {
        "round_id": "session-1:3",
        "playback_id": "session-1:3:playback",
        "reason": "new_input",
    }


def test_playback_report_numeric_helpers_ignore_negative_bool_and_invalid_values():
    data = {
        "good_int": "4",
        "negative_int": -3,
        "bool_int": True,
        "bad_int": "oops",
        "good_float": "1.25",
        "negative_float": -1.25,
        "bool_float": False,
        "bad_float": "oops",
    }

    assert int_from_client_report(data, "good_int") == 4
    assert int_from_client_report(data, "negative_int") == 0
    assert int_from_client_report(data, "bool_int") == 0
    assert int_from_client_report(data, "bad_int") == 0
    assert float_from_client_report(data, "good_float") == 1.25
    assert float_from_client_report(data, "negative_float") == 0.0
    assert float_from_client_report(data, "bool_float") is None
    assert float_from_client_report(data, "bad_float") is None


def test_round_seq_from_trace_id_requires_matching_session_prefix():
    assert round_seq_from_trace_id("session-1", "session-1:7") == 7
    assert round_seq_from_trace_id("session-1", "other:7") is None
    assert round_seq_from_trace_id("session-1", "session-1:bad") is None


def test_build_client_playback_summary_returns_none_without_round_id():
    assert build_client_playback_summary({}, interrupted=False) is None
    assert build_client_playback_summary({"round_id": ""}, interrupted=False) is None


def test_build_client_playback_summary_collects_metrics_and_optional_timings():
    summary = build_client_playback_summary(
        {
            "trace_id": "rust-trace-5",
            "round_id": "session-1:5",
            "playback_id": "session-1:5:playback",
            "reason": "wake_interrupt",
            "pushed_chunks": "4",
            "pushed_samples": 8000,
            "underrun_callbacks": -1,
            "zero_filled_samples": True,
            "max_buffered_samples": "12000",
            "first_audio_to_playback_start_ms": "88.5",
            "playback_start_to_complete_ms": 1020,
        },
        interrupted=True,
    )

    assert summary == {
        "trace_id": "rust-trace-5",
        "round_id": "session-1:5",
        "playback_id": "session-1:5:playback",
        "client_playback_completed": False,
        "client_playback_interrupted": True,
        "client_playback_chunks": 4,
        "client_playback_samples": 8000,
        "client_playback_underruns": 0,
        "client_playback_zero_fill_samples": 0,
        "client_playback_max_buffered_samples": 12000,
        "client_first_audio_to_playback_start_ms": 88.5,
        "client_playback_start_to_complete_ms": 1020.0,
        "reason": "wake_interrupt",
    }


def test_build_client_playback_trace_event_adds_context_and_stage():
    event = build_client_playback_trace_event(
        "session-1",
        {
            "trace_id": "session-1:5",
            "round_id": "session-1:5",
            "playback_id": "session-1:5:playback",
            "pushed_chunks": 3,
            "pushed_samples": 4800,
        },
        interrupted=False,
        trace_context={
            "robot_id": "robot-1",
            "bot_id": "xiaowen",
            "bot_name": "Xiao Wen",
        },
    )

    assert event is not None
    assert event["trace_id"] == "session-1:5"
    assert event["round_seq"] == 5
    assert event["robot_id"] == "robot-1"
    assert event["bot_id"] == "xiaowen"
    assert event["bot_name"] == "Xiao Wen"
    assert event["stage"] == "client_playback_completed"
    assert event["summary"]["client_playback_completed"] is True
    assert event["summary"]["client_playback_interrupted"] is False


def test_build_client_playback_trace_event_marks_interrupted_and_handles_missing_round():
    assert build_client_playback_trace_event("session-1", {}, interrupted=True) is None

    event = build_client_playback_trace_event(
        "session-1",
        {
            "trace_id": "rust-trace",
            "round_id": "session-1:6",
            "reason": "wake_interrupt",
        },
        interrupted=True,
    )

    assert event is not None
    assert event["trace_id"] == "rust-trace"
    assert event["round_seq"] == 6
    assert event["stage"] == "client_playback_interrupted"
    assert event["summary"]["client_playback_completed"] is False
    assert event["summary"]["client_playback_interrupted"] is True
