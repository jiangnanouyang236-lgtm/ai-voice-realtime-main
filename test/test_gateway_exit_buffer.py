import unittest
import os
import grpc

os.environ["CONFIG_DATABASE_URL"] = ""
from gateway.grpc_control import (
    cancel_grpc_call_holder,
    is_cancelled_grpc_error,
    is_locally_cancelled_grpc_error,
)
from gateway.text_streaming import (
    build_direct_text_asr_metadata,
    build_direct_text_summary,
    split_control_safe_buffer,
    split_exit_safe_buffer,
    strip_standalone_tool_tags,
)


class GatewayExitBufferTest(unittest.TestCase):
    def test_complete_sentence_without_exit_prefix_flushes_immediately(self):
        safe_text, pending, should_exit = split_exit_safe_buffer("我帮你查一下天气，稍等一下。")

        self.assertEqual("我帮你查一下天气，稍等一下。", safe_text)
        self.assertEqual("", pending)
        self.assertFalse(should_exit)

    def test_partial_exit_prefix_stays_pending(self):
        safe_text, pending, should_exit = split_exit_safe_buffer("好的，我这就退出[EX")

        self.assertEqual("好的，我这就退出", safe_text)
        self.assertEqual("[EX", pending)
        self.assertFalse(should_exit)

    def test_complete_exit_tag_is_filtered(self):
        safe_text, pending, should_exit = split_exit_safe_buffer("好的，我这就退出。[EXIT]")

        self.assertEqual("好的，我这就退出。", safe_text)
        self.assertEqual("", pending)
        self.assertTrue(should_exit)

    def test_complete_singing_marker_is_filtered(self):
        safe_text, pending, should_exit, asset_id = split_control_safe_buffer(
            "好～我准备一下。[SINGING_PLAYBACK:serena-v1:003]"
        )

        self.assertEqual("好～我准备一下。", safe_text)
        self.assertEqual("", pending)
        self.assertFalse(should_exit)
        self.assertEqual("serena-v1:003", asset_id)

    def test_partial_singing_marker_stays_pending(self):
        safe_text, pending, should_exit, asset_id = split_control_safe_buffer(
            "好～[SINGING_PLAY"
        )

        self.assertEqual("好～", safe_text)
        self.assertEqual("[SINGING_PLAY", pending)
        self.assertFalse(should_exit)
        self.assertIsNone(asset_id)

    def test_multiple_singing_markers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "多个歌曲"):
            split_control_safe_buffer(
                "[SINGING_PLAYBACK:serena-v1:001][SINGING_PLAYBACK:serena-v1:002]"
            )

    def test_standalone_tool_tag_is_filtered(self):
        text = "我现在看一下周围。\n[robot_remote]\n稍等哦。"

        self.assertEqual("我现在看一下周围。\n稍等哦。", strip_standalone_tool_tags(text))

    def test_inline_bracketed_text_is_not_filtered(self):
        text = "这是普通文本里的[robot_remote]标记，不应该被整段删除。"

        self.assertEqual(text, strip_standalone_tool_tags(text))

    def test_build_direct_text_summary_defaults_source(self):
        self.assertEqual(
            {"content": "你好", "source": "client_text"},
            build_direct_text_summary("你好", {}),
        )

    def test_build_direct_text_asr_metadata_keeps_source(self):
        self.assertEqual(
            {"source": "vision_context"},
            build_direct_text_asr_metadata({"source": "vision_context"}),
        )

    def test_cancel_grpc_call_holder_cancels_call_once(self):
        call = _FakeGrpcCall()
        holder = {"call": call}

        self.assertTrue(cancel_grpc_call_holder(holder, "TTS", "session-1"))
        self.assertEqual(1, call.cancel_count)

    def test_cancel_grpc_call_holder_ignores_empty_holder(self):
        self.assertFalse(cancel_grpc_call_holder({"call": None}, "TTS", "session-1"))

    def test_locally_cancelled_grpc_error_is_expected_interrupt(self):
        error = _FakeGrpcCancelledError()

        self.assertTrue(is_cancelled_grpc_error(error))
        self.assertTrue(is_locally_cancelled_grpc_error(error))
        self.assertFalse(is_cancelled_grpc_error(RuntimeError("boom")))
        self.assertFalse(is_locally_cancelled_grpc_error(RuntimeError("boom")))


class _FakeGrpcCall:
    def __init__(self):
        self.cancel_count = 0

    def cancel(self):
        self.cancel_count += 1


class _FakeGrpcCancelledError(Exception):
    def code(self):
        return grpc.StatusCode.CANCELLED

    def details(self):
        return "Locally cancelled by application!"


if __name__ == "__main__":
    unittest.main()
