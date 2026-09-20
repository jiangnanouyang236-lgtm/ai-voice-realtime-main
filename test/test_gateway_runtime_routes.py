import os
import json
import unittest
from unittest.mock import Mock, patch

_original_env_config_database_url = os.environ.get("CONFIG_DATABASE_URL")
os.environ["CONFIG_DATABASE_URL"] = ""

from fastapi.testclient import TestClient

from gateway import config as gateway_config

_original_config_database_url = gateway_config.CONFIG_DATABASE_URL
try:
    gateway_config.CONFIG_DATABASE_URL = ""
    from gateway import gateway_server as gateway
finally:
    gateway_config.CONFIG_DATABASE_URL = _original_config_database_url
    if _original_env_config_database_url is None:
        os.environ.pop("CONFIG_DATABASE_URL", None)
    else:
        os.environ["CONFIG_DATABASE_URL"] = _original_env_config_database_url
from gateway.runtime_api import (
    SENSITIVE_NOTE_REDACTION,
    build_runtime_robots_payload,
    build_runtime_session_payload,
    build_stats_payload,
    sanitize_runtime_robot,
)
from gateway.rtc_config import parse_rtc_ice_servers, summarize_rtc_ice_servers


class GatewayRuntimeRoutesTest(unittest.TestCase):
    def test_healthz_alias_returns_gateway_status(self):
        client = TestClient(gateway.app)

        response = client.get("/healthz")

        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "service": "Voice Gateway"}, response.json())

    def test_runtime_robot_notes_keep_plain_text(self):
        robot = sanitize_runtime_robot(
            {
                "robot_id": "companion_01",
                "notes": "部署在展厅一号位",
            }
        )

        self.assertEqual("部署在展厅一号位", robot["notes"])
        self.assertFalse(robot["notes_redacted"])

    def test_runtime_robot_notes_redact_keyed_secret(self):
        robot = sanitize_runtime_robot(
            {
                "robot_id": "companion_01",
                "notes": "ROBOT_SECRET=abc1234567890SECRET",
            }
        )

        self.assertEqual(SENSITIVE_NOTE_REDACTION, robot["notes"])
        self.assertTrue(robot["notes_redacted"])

    def test_runtime_robot_notes_redact_bare_secret_like_token(self):
        robot = sanitize_runtime_robot(
            {
                "robot_id": "companion_02",
                "notes": "mG5w88vOaZSL1nvWy83drZ5K1cwhi5eEj1G3jCBVKEY",
            }
        )

        self.assertEqual(SENSITIVE_NOTE_REDACTION, robot["notes"])
        self.assertTrue(robot["notes_redacted"])

    def test_runtime_stats_payload_keeps_active_connection_shape(self):
        payload = build_stats_payload(3, {"total_sessions": 5, "active_sessions": 2})

        self.assertEqual(
            {"active_connections": 3, "total_sessions": 5, "active_sessions": 2},
            payload,
        )

    def test_internal_status_returns_process_snapshot_without_runtime_db(self):
        client = TestClient(gateway.app)

        with (
            patch.object(gateway, "gateway_runtime_state", None),
            patch.object(gateway, "active_connections", 0),
        ):
            response = client.get("/internal/status?session_limit=3")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual("ok", payload["status"])
        self.assertEqual("Voice Gateway", payload["service"])
        self.assertIn("generated_at", payload)
        self.assertEqual(0, payload["connections"]["active"])
        self.assertEqual(gateway.MAX_CONNECTIONS, payload["connections"]["max"])
        self.assertEqual(3, payload["sessions"]["limit"])
        self.assertIn("total_sessions", payload["sessions"])
        self.assertIn("items", payload["sessions"])
        self.assertIn("round_count", payload["traces"])
        self.assertEqual({"stt", "llm", "tts"}, {item["service"] for item in payload["upstreams"]})
        self.assertFalse(payload["runtime"]["success"])
        self.assertEqual("env", payload["runtime"]["source"])
        self.assertIn("max_connections", payload["settings"])
        self.assertIn("ready_for_offer", payload["rtc"])
        self.assertFalse(payload["cleanup"]["task_created"])

    def test_internal_status_uses_runtime_max_connections(self):
        client = TestClient(gateway.app)
        runtime_state = Mock()
        runtime_state.get_gateway_settings.return_value = {"max_connections": 20}
        runtime_state.get_status.return_value = {
            "success": True,
            "source": "db",
            "config_version": 160,
        }

        with (
            patch.object(gateway, "gateway_runtime_state", runtime_state),
            patch.object(gateway, "active_connections", 0),
        ):
            response = client.get("/internal/status")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual(20, payload["connections"]["max"])
        self.assertEqual(20, payload["settings"]["max_connections"])
        self.assertEqual("db", payload["runtime"]["source"])

    def test_runtime_robots_payload_redacts_notes_and_preserves_sections(self):
        payload = build_runtime_robots_payload(
            active_connections=1,
            session_stats={"total_sessions": 1},
            sessions=[{"session_id": "s1", "robot_id": "companion_01"}],
            robots=[
                {
                    "robot_id": "companion_01",
                    "notes": "robot_secret=abc1234567890SECRET",
                }
            ],
            settings={"max_connections": 8},
            rtc={"ready_for_offer": True},
            upstreams=[{"service": "stt", "status": "ready"}],
        )

        self.assertTrue(payload["success"])
        self.assertEqual(1, payload["stats"]["active_connections"])
        self.assertEqual("s1", payload["sessions"][0]["session_id"])
        self.assertEqual(SENSITIVE_NOTE_REDACTION, payload["robots"][0]["notes"])
        self.assertTrue(payload["robots"][0]["notes_redacted"])
        self.assertEqual({"max_connections": 8}, payload["settings"])
        self.assertEqual({"ready_for_offer": True}, payload["rtc"])
        self.assertEqual("stt", payload["upstreams"][0]["service"])

    def test_runtime_session_payload_returns_not_found_shape(self):
        payload = build_runtime_session_payload(None)

        self.assertFalse(payload["success"])
        self.assertIsNone(payload["session"])
        self.assertEqual("会话不存在或已清理", payload["message"])

    def test_runtime_session_payload_returns_session_shape(self):
        payload = build_runtime_session_payload({"session_id": "s1", "history": []})

        self.assertTrue(payload["success"])
        self.assertEqual({"session_id": "s1", "history": []}, payload["session"])

    def test_rtc_config_uses_gateway_settings(self):
        config = gateway.build_rtc_config("session-1")
        settings = gateway.build_gateway_rtc_settings()

        self.assertEqual("session-1", config["session_id"])
        self.assertEqual(gateway.GATEWAY_RTC_ICE_SERVERS, config["ice_servers"])
        self.assertEqual(gateway.GATEWAY_RTC_ICE_SERVERS, settings.ice_servers)
        self.assertIsNot(gateway.GATEWAY_RTC_ICE_SERVERS, settings.ice_servers)
        self.assertEqual(gateway.GATEWAY_RTC_SIGNALING_ENABLED, settings.signaling_enabled)
        self.assertEqual(
            gateway.GATEWAY_RTC_AUDIO_DIRECTION,
            config["media"]["audio"]["direction"],
        )
        self.assertEqual(gateway.GATEWAY_RTC_AUDIO_DIRECTION, settings.audio_direction)
        self.assertEqual(gateway.GATEWAY_RTC_AUDIO_CODEC, config["media"]["audio"]["codec"])
        self.assertEqual(gateway.GATEWAY_RTC_AUDIO_CODEC, settings.audio_codec)
        self.assertEqual(
            gateway.GATEWAY_RTC_AUDIO_SAMPLE_RATE,
            config["media"]["audio"]["sample_rate"],
        )
        self.assertEqual(gateway.GATEWAY_RTC_AUDIO_SAMPLE_RATE, settings.audio_sample_rate)
        self.assertEqual(
            gateway.GATEWAY_RTC_AUDIO_CHANNELS,
            config["media"]["audio"]["channels"],
        )
        self.assertEqual(gateway.GATEWAY_RTC_AUDIO_CHANNELS, settings.audio_channels)
        self.assertEqual(
            gateway.GATEWAY_RTC_AUDIO_PTIME_MS,
            config["media"]["audio"]["ptime_ms"],
        )
        self.assertEqual(gateway.GATEWAY_RTC_AUDIO_PTIME_MS, settings.audio_ptime_ms)

    def test_parse_rtc_ice_servers_accepts_csv_urls(self):
        servers = parse_rtc_ice_servers("stun:one.example, turn:two.example")

        self.assertEqual(
            [{"urls": ["stun:one.example"]}, {"urls": ["turn:two.example"]}],
            servers,
        )

    def test_parse_rtc_ice_servers_accepts_json_objects(self):
        servers = parse_rtc_ice_servers(
            '[{"urls":["stun:one.example","turn:two.example"],'
            '"username":"u","credential":"p"}]'
        )

        self.assertEqual(
            [
                {
                    "urls": ["stun:one.example", "turn:two.example"],
                    "username": "u",
                    "credential": "p",
                }
            ],
            servers,
        )

    def test_rtc_ice_summary_redacts_credentials_and_counts_urls(self):
        summary = summarize_rtc_ice_servers(
            [
                {"urls": ["stun:one.example:3478"]},
                {
                    "urls": [
                        "turn:turn.example:3478?transport=udp",
                        "turns:turn.example:5349?transport=tcp",
                    ],
                    "username": "wzkicu",
                    "credential": "secret-value",
                },
            ]
        )

        self.assertEqual(2, summary["ice_server_count"])
        self.assertEqual(1, summary["stun_url_count"])
        self.assertEqual(2, summary["turn_url_count"])
        self.assertEqual(0, summary["turn_servers_missing_credentials"])
        self.assertIn("<redacted>", json.dumps(summary, ensure_ascii=False))
        self.assertNotIn("secret-value", json.dumps(summary, ensure_ascii=False))

    def test_rtc_status_warns_about_unusable_config(self):
        original = {
            "GATEWAY_RTC_SIGNALING_ENABLED": gateway.GATEWAY_RTC_SIGNALING_ENABLED,
            "GATEWAY_RTC_ICE_SERVERS": gateway.GATEWAY_RTC_ICE_SERVERS,
            "GATEWAY_RTC_AUDIO_CODEC": gateway.GATEWAY_RTC_AUDIO_CODEC,
            "GATEWAY_RTC_AUDIO_SAMPLE_RATE": gateway.GATEWAY_RTC_AUDIO_SAMPLE_RATE,
            "GATEWAY_RTC_AUDIO_CHANNELS": gateway.GATEWAY_RTC_AUDIO_CHANNELS,
            "GATEWAY_RTC_AUDIO_PTIME_MS": gateway.GATEWAY_RTC_AUDIO_PTIME_MS,
        }
        try:
            gateway.GATEWAY_RTC_SIGNALING_ENABLED = True
            gateway.GATEWAY_RTC_ICE_SERVERS = [
                {"urls": ["", "https://bad.example"]},
                {"urls": ["turn:turn.example:3478"], "username": "wzkicu"},
            ]
            gateway.GATEWAY_RTC_AUDIO_CODEC = "pcm16"
            gateway.GATEWAY_RTC_AUDIO_SAMPLE_RATE = 16000
            gateway.GATEWAY_RTC_AUDIO_CHANNELS = 1
            gateway.GATEWAY_RTC_AUDIO_PTIME_MS = 30

            status = gateway.build_rtc_status()
        finally:
            for name, value in original.items():
                setattr(gateway, name, value)

        self.assertFalse(status["ready_for_offer"])
        self.assertIn("empty_ice_url", status["warnings"])
        self.assertIn("unknown_ice_url_scheme", status["warnings"])
        self.assertIn("turn_missing_username_or_credential", status["warnings"])
        self.assertIn("audio_codec_not_opus", status["warnings"])
        self.assertIn("audio_sample_rate_not_webrtc_opus_clock", status["warnings"])
        self.assertIn("audio_ptime_unusual", status["warnings"])

    def test_rtc_status_marks_clean_config_ready_for_offer(self):
        original = {
            "GATEWAY_RTC_SIGNALING_ENABLED": gateway.GATEWAY_RTC_SIGNALING_ENABLED,
            "GATEWAY_RTC_ICE_SERVERS": gateway.GATEWAY_RTC_ICE_SERVERS,
            "GATEWAY_RTC_AUDIO_CODEC": gateway.GATEWAY_RTC_AUDIO_CODEC,
            "GATEWAY_RTC_AUDIO_SAMPLE_RATE": gateway.GATEWAY_RTC_AUDIO_SAMPLE_RATE,
            "GATEWAY_RTC_AUDIO_CHANNELS": gateway.GATEWAY_RTC_AUDIO_CHANNELS,
            "GATEWAY_RTC_AUDIO_PTIME_MS": gateway.GATEWAY_RTC_AUDIO_PTIME_MS,
        }
        try:
            gateway.GATEWAY_RTC_SIGNALING_ENABLED = True
            gateway.GATEWAY_RTC_ICE_SERVERS = [
                {"urls": ["stun:frp.wzk.icu:3478"]},
                {
                    "urls": ["turn:frp.wzk.icu:3478?transport=udp"],
                    "username": "wzkicu",
                    "credential": "secret-value",
                },
            ]
            gateway.GATEWAY_RTC_AUDIO_CODEC = "opus"
            gateway.GATEWAY_RTC_AUDIO_SAMPLE_RATE = 48000
            gateway.GATEWAY_RTC_AUDIO_CHANNELS = 1
            gateway.GATEWAY_RTC_AUDIO_PTIME_MS = 20

            status = gateway.build_rtc_status()
        finally:
            for name, value in original.items():
                setattr(gateway, name, value)

        self.assertTrue(status["ready_for_offer"])
        self.assertEqual([], status["warnings"])
        self.assertEqual(1, status["ice"]["stun_url_count"])
        self.assertEqual(1, status["ice"]["turn_url_count"])
        self.assertNotIn("secret-value", json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
