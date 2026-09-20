import importlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch


class SensitiveDefaultsTest(unittest.TestCase):
    def test_committed_env_templates_do_not_embed_secrets(self):
        root = Path(__file__).resolve().parents[1]
        sensitive_keys = {
            "ADMIN_PASSWORD",
            "ADMIN_SESSION_SECRET",
            "LLM_API_KEY",
            "LLM_ROUTER_API_KEY",
            "QWEN_ASR_API_KEY",
            "QWEN3_TTS_CUSTOM_VOICE_API_KEY",
            "QWEN3_TTS_BASE_API_KEY",
            "ROBOT_SECRET",
            "ROBOT_MQTT_PASSWORD",
            "COGNITIVE_REPORT_MQTT_PASSWORD",
        }
        for path in (root / ".env.example", root / "deploy/env.v3.compose.example"):
            values = {}
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
            for key in sensitive_keys:
                with self.subTest(path=path.name, key=key):
                    self.assertFalse(values.get(key, ""))

    def test_cognitive_report_mqtt_defaults_do_not_include_broker_or_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            from llm import agent_tools

            module = importlib.reload(agent_tools)
            self.addCleanup(importlib.reload, module)

            publisher = module.CognitiveReportMqttPublisher.from_env()

        self.assertFalse(publisher.enabled)
        self.assertEqual("", publisher.host)
        self.assertEqual("", publisher.username)
        self.assertEqual("", publisher.password)

    def test_cognitive_report_mqtt_enables_when_host_is_configured(self):
        with patch.dict(
            os.environ,
            {
                "COGNITIVE_REPORT_MQTT_HOST": "mqtt.internal",
                "COGNITIVE_REPORT_MQTT_USERNAME": "robot-user",
                "COGNITIVE_REPORT_MQTT_PASSWORD": "robot-pass",
            },
            clear=True,
        ):
            from llm import agent_tools

            module = importlib.reload(agent_tools)
            self.addCleanup(importlib.reload, module)

            publisher = module.CognitiveReportMqttPublisher.from_env()

        self.assertTrue(publisher.enabled)
        self.assertEqual("mqtt.internal", publisher.host)
        self.assertEqual("robot-user", publisher.username)
        self.assertEqual("robot-pass", publisher.password)

    def test_robot_mqtt_defaults_do_not_include_broker_or_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            from mcp_servers import robot_mqtt_api

            module = importlib.reload(robot_mqtt_api)
            self.addCleanup(importlib.reload, module)

            self.assertEqual("", module.MQTT_HOST)
            self.assertEqual("", module.MQTT_USERNAME)
            self.assertEqual("", module.MQTT_PASSWORD)
            with self.assertRaisesRegex(RuntimeError, "ROBOT_MQTT_HOST"):
                module._publish_payload("windaka/robot/mcp/task/manual_control_cmd", {})

    def test_robot_manual_control_action_type_mapping_includes_interactions(self):
        with patch.dict(os.environ, {}, clear=True):
            from mcp_servers import robot_mqtt_api

            module = importlib.reload(robot_mqtt_api)
            self.addCleanup(importlib.reload, module)

            cases = {
                "forward": 1,
                "backward": 2,
                "turn_left": 3,
                "turn_right": 4,
                "stop": 5,
                "greet": 6,
                "handshake": 7,
                "cheer": 8,
            }

            for action, expected_type in cases.items():
                with self.subTest(action=action):
                    payload = module.build_manual_control_payload(
                        action=action,
                        timestamp_ms=123456,
                    )
                    self.assertEqual(expected_type, payload["data"]["type"])


if __name__ == "__main__":
    unittest.main()
