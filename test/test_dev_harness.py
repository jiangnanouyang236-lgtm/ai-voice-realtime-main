from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_dev_harness
from scripts import probe_python_gateway_internal_voice as voice_probe
from scripts import probe_python_gateway_ws_audio as gateway_ws_probe
from gateway.audio_protocol import encode_audio_frame


def test_dry_run_is_safe_and_describes_fixed_robot(capsys) -> None:
    result = run_dev_harness.main(["--dry-run"])

    assert result == 0
    output = capsys.readouterr().out
    assert '"robot_id": "test_01"' in output
    assert '"greet"' in output
    assert '"cheer"' in output


def test_voice_client_event_dry_run_declares_llm_bypass(capsys) -> None:
    result = run_dev_harness.main(
        ["--scenario", "voice-client-event", "--dry-run"]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert '"voice-client-event"' in output
    assert '"tts_grpc"' in output
    assert '"bypassed_by_client_event_contract"' in output


def test_voice_m1_e2e_dry_run_declares_hybrid_native_rtp(capsys) -> None:
    result = run_dev_harness.main(
        ["--scenario", "voice-m1-e2e", "--dry-run"]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["robot_id"] == "test_01"
    assert payload["transport"] == "native_webrtc_rtp_only"
    assert payload["claimed_level"] == "HYBRID_VERIFIED"


def test_voice_agent_continuity_dry_run_declares_same_m1_session(capsys) -> None:
    result = run_dev_harness.main(
        ["--scenario", "voice-agent-continuity", "--dry-run"]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["inputs"] == [
        run_dev_harness.AGENT_ENTRY_PROMPT,
        run_dev_harness.AGENT_CONFIRM_PROMPT,
    ]
    assert payload["transport"] == "m1_input_text_commit_same_session"


def test_voice_vision_dry_run_declares_gold_and_programmatic_path(capsys) -> None:
    result = run_dev_harness.main(["--scenario", "voice-vision", "--dry-run"])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["robot_id"] == "test_01"
    assert payload["fixture_sha256"] == run_dev_harness.VISION_FIXTURE_SHA256
    assert payload["transport"] == "native_webrtc_vision_v1_data_channel"
    assert payload["claimed_level"] == "HYBRID_VERIFIED"


def test_m1_internal_voice_env_tracks_isolated_gateway_port() -> None:
    assert run_dev_harness._m1_internal_voice_env(57860) == {
        "GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE": "m1",
        "GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL": (
            "ws://127.0.0.1:57860/internal/voice/ws"
        ),
    }


def test_voice_direct_text_dry_run_declares_router_and_asr_boundary(capsys) -> None:
    result = run_dev_harness.main(
        ["--scenario", "voice-direct-text", "--dry-run"]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert '"router_model"' in output
    assert '"main_llm_model"' in output
    assert '"bypassed_by_direct_text_contract"' in output


def test_voice_utils_time_dry_run_declares_full_bot_matrix(capsys) -> None:
    result = run_dev_harness.main(
        ["--scenario", "voice-utils-time", "--dry-run"]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert '"utils_mcp"' in output
    assert '"all_enabled_bots_plus_test_01_audio"' in output
    assert '"external_write": false' in output


def test_voice_robot_actions_dry_run_declares_gateway_mqtt_chain(capsys) -> None:
    result = run_dev_harness.main(
        ["--scenario", "voice-robot-actions", "--dry-run"]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert '"python_gateway"' in output
    assert '"mqtt"' in output
    assert '"--authorize-external-write"' in output


def test_voice_audio_dry_run_declares_tracked_fixture(capsys) -> None:
    result = run_dev_harness.main(["--scenario", "voice-audio", "--dry-run"])

    assert result == 0
    output = capsys.readouterr().out
    assert '"stt_grpc"' in output
    assert '"tracked_reference_recording"' in output
    assert run_dev_harness.VOICE_AUDIO_FIXTURE_SHA256 in output


def test_non_allowlisted_robot_is_refused_before_connectivity(capsys) -> None:
    result = run_dev_harness.main(["--robot-id", "companion_01"])

    assert result == 2
    assert "不在固定测试白名单" in capsys.readouterr().out


def test_external_actions_require_explicit_authorization(capsys) -> None:
    result = run_dev_harness.main([])

    assert result == 2
    assert "必须显式授权" in capsys.readouterr().out


def test_voice_robot_actions_require_external_write(capsys) -> None:
    result = run_dev_harness.main(
        [
            "--scenario",
            "voice-robot-actions",
            "--authorize-connectivity",
            "--authorization-reference",
            "test-reference",
        ]
    )

    assert result == 2
    assert "external write" in capsys.readouterr().out


def test_voice_client_event_does_not_require_external_write(monkeypatch) -> None:
    monkeypatch.setattr(run_dev_harness, "_run_voice_client_event", lambda args: 0)

    result = run_dev_harness.main(
        [
            "--scenario",
            "voice-client-event",
            "--authorize-connectivity",
            "--authorization-reference",
            "test-authorization",
        ]
    )

    assert result == 0


def test_voice_secret_guard_uses_matching_private_env() -> None:
    secret = run_dev_harness._resolve_voice_robot_secret(
        {
            "GATEWAY_REQUIRE_ROBOT_SECRET": "true",
            "ROBOT_ID": "test_01",
            "ROBOT_SECRET": "private-secret",
        },
        "test_01",
    )

    assert secret == "private-secret"


def test_voice_secret_guard_rejects_mismatched_robot() -> None:
    with pytest.raises(run_dev_harness.HarnessRunError, match="ROBOT_ID"):
        run_dev_harness._resolve_voice_robot_secret(
            {
                "GATEWAY_REQUIRE_ROBOT_SECRET": "true",
                "ROBOT_ID": "companion_01",
                "ROBOT_SECRET": "private-secret",
            },
            "test_01",
        )


def test_voice_private_credentials_fill_only_empty_values(tmp_path: Path) -> None:
    private_env = tmp_path / ".env.local"
    private_env.write_text(
        'ROBOT_ID="test_01"\nROBOT_SECRET="private-secret"\nIGNORED_KEY="no"\n',
        encoding="utf-8",
    )

    loaded = run_dev_harness._load_voice_private_credentials(
        {"ROBOT_SECRET": ""}, private_env
    )

    assert loaded["ROBOT_ID"] == "test_01"
    assert loaded["ROBOT_SECRET"] == "private-secret"
    assert "IGNORED_KEY" not in loaded


def test_output_directory_cannot_escape_generated_roots(tmp_path: Path) -> None:
    with pytest.raises(run_dev_harness.HarnessRunError, match="reports/ 或 tmp/"):
        run_dev_harness._safe_output_dir(tmp_path / "outside")


def test_proxy_bypass_hosts_are_derived_without_exposing_credentials() -> None:
    hosts = run_dev_harness._proxy_bypass_hosts(
        {
            "CONFIG_DATABASE_URL": "mysql://user:secret@10.0.0.8:3306/config",
            "LLM_BASE_URL": "http://model.internal:15101/v1",
            "QWEN3_TTS_CUSTOM_VOICE_WS_URL": "ws://tts.internal:15120/v1/audio/speech/stream",
            "ROBOT_MQTT_HOST": "mqtt.example.net",
        }
    )

    assert hosts == {
        "127.0.0.1",
        "localhost",
        "10.0.0.8",
        "model.internal",
        "tts.internal",
        "mqtt.example.net",
    }


def test_health_check_refuses_non_localhost() -> None:
    with pytest.raises(run_dev_harness.HarnessRunError, match="只允许 localhost"):
        run_dev_harness._wait_http(
            "http://example.com/health",
            SimpleNamespace(poll=lambda: None),
            0.1,
        )


def test_health_check_accepts_ready_local_service(monkeypatch) -> None:
    class _Response:
        status = 200

        @staticmethod
        def read() -> bytes:
            return b"ok"

    class _Connection:
        def request(self, method: str, path: str) -> None:
            assert method == "GET"
            assert path == "/health"

        @staticmethod
        def getresponse() -> _Response:
            return _Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(
        run_dev_harness,
        "HTTPConnection",
        lambda host, port, timeout: _Connection(),
    )

    run_dev_harness._wait_http(
        "http://127.0.0.1:5003/health",
        SimpleNamespace(poll=lambda: None),
        0.1,
    )


def test_mqtt_capture_verification_checks_both_actions_and_trace() -> None:
    traces = {"greet": "trace-greet", "cheer": "trace-cheer"}
    topic = "windaka/test_01/mcp/task/manual_control_cmd"
    capture = SimpleNamespace(
        messages=[
            {
                "topic": topic,
                "retain": False,
                "payload": {
                    "method": "/task/manual_control_cmd",
                    "data": {"type": 6},
                    "meta": {"session_id": "session", "trace_id": "trace-greet"},
                },
            },
            {
                "topic": topic,
                "retain": False,
                "payload": {
                    "method": "/task/manual_control_cmd",
                    "data": {"type": 8},
                    "meta": {"session_id": "session", "trace_id": "trace-cheer"},
                },
            },
        ]
    )

    run_dev_harness._verify_capture(capture=capture, traces=traces, topic=topic)


def test_mqtt_capture_verification_rejects_retained_message() -> None:
    traces = {"greet": "trace-greet", "cheer": "trace-cheer"}
    topic = "windaka/test_01/mcp/task/manual_control_cmd"
    capture = SimpleNamespace(
        messages=[
            {
                "topic": topic,
                "retain": True,
                "payload": {
                    "method": "/task/manual_control_cmd",
                    "data": {"type": 6},
                    "meta": {"session_id": "session", "trace_id": "trace-greet"},
                },
            },
            {
                "topic": topic,
                "retain": False,
                "payload": {
                    "method": "/task/manual_control_cmd",
                    "data": {"type": 8},
                    "meta": {"session_id": "session", "trace_id": "trace-cheer"},
                },
            },
        ]
    )

    with pytest.raises(run_dev_harness.HarnessRunError, match="不应 retained"):
        run_dev_harness._verify_capture(capture=capture, traces=traces, topic=topic)


def _voice_probe_detail(trace_id: str = "trace-voice") -> dict:
    session_id = "session-voice"
    return {
        "session_id": session_id,
        "trace_id": trace_id,
        "messages": [
            {"phase": "session.open", "message": {"type": "session.opened"}},
            {
                "phase": "client_event",
                "message": {
                    "type": "orchestrator.status",
                    "trace_id": trace_id,
                    "payload": {
                        "mode": "client_event_active_accepted",
                        "event": "wake_idle",
                    },
                },
            },
            {
                "phase": "client_event",
                "message": {
                    "type": "playback_start",
                    "trace_id": trace_id,
                    "playback_id": "playback-voice",
                },
            },
            {
                "phase": "client_event",
                "message": {
                    "type": "<binary>",
                    "payload_bytes": 128,
                    "header": {
                        "event_type": "response.audio",
                        "direction": "downlink",
                        "trace_id": trace_id,
                        "session_id": session_id,
                    },
                },
            },
            {
                "phase": "client_event",
                "message": {
                    "type": "response.done",
                    "trace_id": trace_id,
                    "payload": {"reason": "completed", "event": "wake_idle"},
                },
            },
            {"phase": "session.close", "message": {"type": "session.closed"}},
        ],
    }


def test_voice_client_event_verification_requires_real_trace_bound_audio() -> None:
    result = run_dev_harness._verify_voice_client_event_probe(
        detail=_voice_probe_detail(),
        expected_event="wake_idle",
    )

    assert result["trace_id"] == "trace-voice"
    assert result["audio_frames"] == 1
    assert result["audio_payload_bytes"] == 128


def test_voice_client_event_verification_rejects_wrong_audio_trace() -> None:
    detail = _voice_probe_detail()
    detail["messages"][3]["message"]["header"]["trace_id"] = "stale-trace"

    with pytest.raises(run_dev_harness.HarnessRunError, match="未绑定目标 session/trace"):
        run_dev_harness._verify_voice_client_event_probe(
            detail=detail,
            expected_event="wake_idle",
        )


def test_internal_voice_probe_decodes_vaf1_without_exposing_audio() -> None:
    frame = encode_audio_frame(
        b"opus-payload",
        event_type="response.audio",
        direction="downlink",
        encoding="opus",
        session_id="session-voice",
        trace_id="trace-voice",
    )

    class _WebSocket:
        def settimeout(self, timeout: float) -> None:
            assert timeout == 1.0

        def recv(self) -> bytes:
            return frame

    message = voice_probe.recv_message(_WebSocket(), 1.0)

    assert message["type"] == "<binary>"
    assert message["payload_bytes"] == len(b"opus-payload")
    assert message["header"]["trace_id"] == "trace-voice"
    assert "opus-payload" not in str(message)


def test_harness_declares_voice_client_event_hybrid_policy() -> None:
    manifest = json.loads(
        (run_dev_harness.ROOT / ".ai" / "harness.json").read_text(encoding="utf-8")
    )
    policy = manifest["evidence_policy"]["capabilities"]["voice_client_event"]

    assert policy["HYBRID_VERIFIED"]["required_components"] == [
        "runtime_snapshot",
        "python_gateway",
        "tts",
    ]


def _direct_text_probe_and_trace() -> tuple[dict, dict, dict]:
    trace_id = "trace-direct-text"
    session_id = "session-direct-text"
    runtime = {"robot_id": "test_01", "bot_id": "wzk-test-bot"}
    probe = {
        "ok": True,
        "session_id": session_id,
        "robot_id": "test_01",
        "bot_id": "wzk-test-bot",
        "trace_id": trace_id,
        "input_text": run_dev_harness.DIRECT_TEXT_PROMPT,
        "messages": [
            {
                "type": "text",
                "message": {"type": "text", "content": run_dev_harness.DIRECT_TEXT_PROMPT},
            },
            {
                "type": "done",
                "message": {
                    "type": "done",
                    "trace_id": trace_id,
                    "round_id": "round-direct-text",
                    "playback_id": "playback-direct-text",
                },
            },
        ],
        "audio_frames": [
            {
                "type": "<binary>",
                "bytes": 256,
                "header": {
                    "type": "audio_frame",
                    "direction": "server_tts",
                    "encoding": "opus",
                    "trace_id": trace_id,
                    "round_id": "round-direct-text",
                    "playback_id": "playback-direct-text",
                },
            }
        ],
    }
    stages = [
        "text_received",
        "direct_text_ready",
        "llm_internal_metrics",
        "llm_first_token",
        "llm_done",
        "tts_first_audio",
        "llm_tts_done",
    ]
    events = [
        {
            "stage": stage,
            "summary": {"text": "秋天是温暖的金黄色。"} if stage == "llm_done" else {},
        }
        for stage in stages
    ]
    trace = {
        "success": True,
        "trace": {
            "trace_id": trace_id,
            "session_id": session_id,
            "robot_id": "test_01",
            "bot_id": "wzk-test-bot",
            "events": events,
            "metrics": {
                "llm_router_classifier_used": True,
                "llm_router_source": "llm_router_async",
                "llm_router_kind": "chat",
                "llm_response_chars": 12,
                "llm_stream_mode": "chat_bypass_tools",
                "llm_tool_call_count": 0,
            },
            "diagnosis": {"status": "ok"},
        },
    }
    return probe, trace, runtime


def test_direct_text_verification_requires_router_llm_and_audio() -> None:
    probe, trace, runtime = _direct_text_probe_and_trace()

    result = run_dev_harness._verify_direct_text_probe(
        probe=probe, trace_payload=trace, runtime=runtime
    )

    assert result["audio_payload_bytes"] == 256
    assert result["metrics"]["llm_router_classifier_used"] is True


def test_direct_text_verification_rejects_router_bypass() -> None:
    probe, trace, runtime = _direct_text_probe_and_trace()
    trace["trace"]["metrics"]["llm_router_classifier_used"] = False

    with pytest.raises(run_dev_harness.HarnessRunError, match="Router 分类器"):
        run_dev_harness._verify_direct_text_probe(
            probe=probe, trace_payload=trace, runtime=runtime
        )


def test_utils_time_verification_requires_exact_tool_and_facts(monkeypatch) -> None:
    probe, trace, runtime = _direct_text_probe_and_trace()
    probe["input_text"] = run_dev_harness.UTILS_TIME_PROMPT
    probe["messages"][0]["message"]["content"] = run_dev_harness.UTILS_TIME_PROMPT
    metrics = trace["trace"]["metrics"]
    metrics.update(
        {
            "llm_router_kind": "tool",
            "llm_router_category": "utils",
            "llm_selected_tool_name": "utils_remote__get_now_context",
            "llm_first_round_tools_count": 1,
            "llm_tool_call_count": 1,
            "llm_stream_mode": "tool_or_legacy",
        }
    )
    trace["trace"]["events"][4]["summary"]["text"] = "2026年8月10日，星期一，工作日。"
    monkeypatch.setattr(
        "scripts.eval_bot_utils_matrix.expected_time_facts",
        lambda: {
            "date_padded": "2026年08月10日",
            "date_unpadded": "2026年8月10日",
            "weekday": "星期一",
            "holiday": "否：工作日",
        },
    )

    result = run_dev_harness._verify_direct_text_probe(
        probe=probe,
        trace_payload=trace,
        runtime=runtime,
        expected_route="utils",
    )

    assert result["metrics"]["llm_tool_call_count"] == 1


def test_robot_action_verification_requires_exact_move_tool() -> None:
    probe, trace, runtime = _direct_text_probe_and_trace()
    prompt = "请控制机器人和我打个招呼。"
    probe["input_text"] = prompt
    probe["messages"][0]["message"]["content"] = prompt
    trace["trace"]["metrics"].update(
        {
            "llm_router_kind": "tool",
            "llm_router_category": "robot",
            "llm_selected_tool_name": "robot_remote__move_robot",
            "llm_first_round_tools_count": 1,
            "llm_tool_call_count": 1,
            "llm_stream_mode": "tool_or_legacy",
        }
    )

    result = run_dev_harness._verify_direct_text_probe(
        probe=probe,
        trace_payload=trace,
        runtime=runtime,
        expected_route="robot",
        expected_action="greet",
    )

    assert result["metrics"]["llm_selected_tool_name"] == "robot_remote__move_robot"


def test_active_audio_verification_requires_asr_and_trace_bound_audio() -> None:
    trace_id = "trace-audio"
    session_id = "session-audio"
    utterance_id = "utterance-audio"
    detail = {
        "trace_id": trace_id,
        "session_id": session_id,
        "utterance_id": utterance_id,
        "messages": [
            {"message": {"type": "session.opened"}},
            {
                "message": {
                    "type": "response.asr",
                    "trace_id": trace_id,
                    "utterance_id": utterance_id,
                    "payload": {
                        "text": "先别着急，我听清楚了，你说的重点我已经明白。"
                    },
                }
            },
            {"message": {"type": "playback_start", "trace_id": trace_id}},
            {
                "message": {
                    "type": "<binary>",
                    "payload_bytes": 128,
                    "header": {
                        "event_type": "response.audio",
                        "direction": "downlink",
                        "trace_id": trace_id,
                        "session_id": session_id,
                    },
                }
            },
            {
                "message": {
                    "type": "response.done",
                    "trace_id": trace_id,
                    "payload": {"reason": "completed"},
                }
            },
            {"message": {"type": "session.closed"}},
        ],
    }

    result = run_dev_harness._verify_active_audio_probe(detail)

    assert result["asr_text"].startswith("先别着急")
    assert result["audio_payload_bytes"] == 128


def test_agent_continuity_verification_requires_two_typed_audio_turns() -> None:
    session_id = "rtc_agent_1"
    turns = []
    for index in (1, 2):
        trace_id = f"trace-{index}"
        turns.append(
            {
                "trace_id": trace_id,
                "utterance_id": f"utt-{index}",
                "messages": [
                    {
                        "type": "orchestrator.status",
                        "session_id": session_id,
                        "payload": {"mode": "input_text_active_accepted"},
                    },
                    {
                        "type": "response.asr",
                        "session_id": session_id,
                        "payload": {"text": f"turn-{index}"},
                    },
                    {
                        "type": "<binary>",
                        "header": {"session_id": session_id},
                    },
                    {"type": "response.done", "session_id": session_id},
                ],
            }
        )

    result = run_dev_harness._verify_agent_continuity_probe(
        {"session_id": session_id, "turns": turns}
    )

    assert result["session_id"] == session_id
    assert [turn["audio_frames"] for turn in result["turns"]] == [1, 1]


def test_gateway_ws_probe_decodes_vaf1_without_audio_leak() -> None:
    frame = encode_audio_frame(
        b"private-audio",
        event_type="response.audio",
        direction="downlink",
        encoding="opus",
        session_id="session-direct-text",
        trace_id="trace-direct-text",
    )

    class _WebSocket:
        def settimeout(self, timeout: float) -> None:
            assert timeout == 2.0

        def recv(self) -> bytes:
            return frame

    message = gateway_ws_probe.recv_message(_WebSocket(), 2.0)

    assert message["bytes"] == len(b"private-audio")
    assert message["header"]["trace_id"] == "trace-direct-text"
    assert "private-audio" not in str(message)


def test_harness_declares_voice_direct_text_hybrid_policy() -> None:
    manifest = json.loads(
        (run_dev_harness.ROOT / ".ai" / "harness.json").read_text(encoding="utf-8")
    )
    policy = manifest["evidence_policy"]["capabilities"]["voice_direct_text"]

    assert policy["HYBRID_VERIFIED"]["required_components"] == [
        "runtime_snapshot",
        "python_gateway",
        "router",
        "llm",
        "tts",
    ]


def test_harness_declares_utils_tool_route_hybrid_policy() -> None:
    manifest = json.loads(
        (run_dev_harness.ROOT / ".ai" / "harness.json").read_text(encoding="utf-8")
    )
    policy = manifest["evidence_policy"]["capabilities"]["utils_tool_route"]

    assert policy["HYBRID_VERIFIED"]["required_components"] == [
        "runtime_snapshot",
        "python_gateway",
        "router",
        "llm",
        "utils_mcp",
        "tts",
    ]


def test_harness_declares_robot_voice_route_hybrid_policy() -> None:
    manifest = json.loads(
        (run_dev_harness.ROOT / ".ai" / "harness.json").read_text(encoding="utf-8")
    )
    policy = manifest["evidence_policy"]["capabilities"]["robot_voice_route"]

    assert policy["HYBRID_VERIFIED"]["required_components"] == [
        "runtime_snapshot",
        "python_gateway",
        "router",
        "llm",
        "robot_mcp",
        "mqtt_broker",
        "tts",
    ]


def test_harness_declares_python_audio_hybrid_policy() -> None:
    manifest = json.loads(
        (run_dev_harness.ROOT / ".ai" / "harness.json").read_text(encoding="utf-8")
    )
    policy = manifest["evidence_policy"]["capabilities"]["voice_python_audio"]

    assert policy["HYBRID_VERIFIED"]["required_components"] == [
        "runtime_snapshot",
        "python_gateway",
        "stt",
        "router",
        "llm",
        "tts",
    ]
