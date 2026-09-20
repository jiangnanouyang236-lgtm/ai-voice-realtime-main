import unittest
from unittest import mock

from mcp_servers import robot_mqtt_api, robot_sse_server


class RobotVideoCallTest(unittest.TestCase):
    def test_payload_uses_fixed_protocol_fields(self):
        payload = robot_mqtt_api.build_video_call_payload(timestamp_ms=1742134800000)

        self.assertEqual(
            {
                "msg_id": 7,
                "msg_type": "request",
                "method": "/device/call",
                "timestamp": 1742134800000,
                "data": {"action": 1, "code": 1},
            },
            payload,
        )

    def test_publish_uses_current_robot_id_in_topic(self):
        with (
            mock.patch.object(robot_mqtt_api, "MQTT_TOPIC_PREFIX", "windaka"),
            mock.patch.object(robot_mqtt_api, "_publish_payload") as publish_payload,
        ):
            publish_payload.return_value = {"topic": "captured"}

            result = robot_mqtt_api.publish_video_call_cmd(
                timestamp_ms=1742134800000,
                robot_id="companion_01",
            )

        publish_payload.assert_called_once_with(
            "windaka/companion_01/mcp/device/call",
            {
                "msg_id": 7,
                "msg_type": "request",
                "method": "/device/call",
                "timestamp": 1742134800000,
                "data": {"action": 1, "code": 1},
            },
            meta=None,
        )
        self.assertEqual("companion_01", result["robot_id"])

    def test_publish_forwards_session_and_trace_meta(self):
        audit_meta = {"session_id": "session-1", "trace_id": "trace-1"}
        with mock.patch.object(robot_mqtt_api, "_publish_payload") as publish_payload:
            publish_payload.return_value = {"topic": "captured"}

            robot_mqtt_api.publish_video_call_cmd(
                timestamp_ms=1742134800000,
                robot_id="companion_01",
                meta=audit_meta,
            )

        self.assertEqual(audit_meta, publish_payload.call_args.kwargs["meta"])

    def test_mcp_tool_only_exposes_robot_id(self):
        tool = next(tool for tool in robot_sse_server.TOOLS_LIST if tool["name"] == "call_video")

        self.assertEqual({"robot_id"}, set(tool["inputSchema"]["properties"]))
        self.assertNotIn("action", tool["inputSchema"]["properties"])
        self.assertNotIn("code", tool["inputSchema"]["properties"])

    def test_mcp_dispatch_forwards_authoritative_robot_id(self):
        with mock.patch.object(
            robot_sse_server.robot_mqtt_api,
            "call_video_text",
            return_value="好的，我开始呼叫视频电话。",
        ) as call_video_text:
            result = robot_sse_server._call_tool("call_video", {"robot_id": "robot_42"})

        call_video_text.assert_called_once_with(robot_id="robot_42", meta=None)
        self.assertEqual("好的，我开始呼叫视频电话。", result)

    def test_mcp_dispatch_extracts_audit_meta_before_robot_call(self):
        arguments = {
            "robot_id": "robot_42",
            "_meta": {"session_id": "session-1", "trace_id": "trace-1"},
        }
        with mock.patch.object(
            robot_sse_server.robot_mqtt_api,
            "call_video_text",
            return_value="好的，我开始呼叫视频电话。",
        ) as call_video_text:
            robot_sse_server._call_tool("call_video", arguments)

        call_video_text.assert_called_once_with(
            robot_id="robot_42",
            meta={"session_id": "session-1", "trace_id": "trace-1"},
        )
        self.assertNotIn("_meta", arguments)


if __name__ == "__main__":
    unittest.main()
