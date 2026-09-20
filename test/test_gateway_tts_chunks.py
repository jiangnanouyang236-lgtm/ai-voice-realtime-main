from gateway.tts_chunks import build_tts_text_chunk


def test_build_tts_text_chunk_fills_trace_fields_and_timestamp():
    chunk = build_tts_text_chunk(
        "你好",
        is_final=False,
        session_id="session-1",
        trace_id="trace-1",
        round_id="round-1",
        playback_id="round-1:playback",
        gateway_send_epoch_ms=123.4,
    )

    assert chunk.text == "你好"
    assert chunk.is_final is False
    assert chunk.session_id == "session-1"
    assert chunk.trace_id == "trace-1"
    assert chunk.round_id == "round-1"
    assert chunk.playback_id == "round-1:playback"
    assert chunk.gateway_send_epoch_ms == 123.4
    assert not chunk.HasField("config")


def test_build_tts_text_chunk_includes_config_only_when_requested():
    chunk = build_tts_text_chunk(
        "你好",
        is_final=False,
        session_id="session-1",
        trace_id=None,
        round_id=None,
        playback_id=None,
        bot_tts_settings={"tts_profile_id": "default_tts_profile"},
        include_config=True,
        gateway_send_epoch_ms=123.4,
    )
    final_chunk = build_tts_text_chunk(
        "",
        is_final=True,
        session_id="session-1",
        trace_id=None,
        round_id=None,
        playback_id=None,
        bot_tts_settings={"tts_profile_id": "default_tts_profile"},
        include_config=False,
        gateway_send_epoch_ms=124.4,
    )

    assert chunk.HasField("config")
    assert chunk.config.tts_profile_id == "default_tts_profile"
    assert chunk.trace_id == ""
    assert chunk.round_id == ""
    assert chunk.playback_id == ""
    assert not final_chunk.HasField("config")
