from __future__ import annotations

import unittest
from unittest.mock import patch

from gateway.trace_recorder import TraceRecorder


class TraceRecorderTest(unittest.TestCase):
    def test_list_rounds_paginates_newest_first(self) -> None:
        recorder = TraceRecorder(max_events=20, max_rounds=10)
        for index in range(4):
            recorder.emit(
                f"trace-{index}",
                session_id="session-1",
                round_seq=index,
                stage=f"stage-{index}",
            )

        payload = recorder.list_rounds(limit=2, offset=1)

        self.assertTrue(payload["success"])
        self.assertEqual([item["trace_id"] for item in payload["items"]], ["trace-2", "trace-1"])
        self.assertEqual(
            payload["pagination"],
            {
                "limit": 2,
                "offset": 1,
                "total": 4,
                "has_more": True,
            },
        )

    def test_trim_removes_oldest_rounds_when_event_capacity_is_exceeded(self) -> None:
        recorder = TraceRecorder(max_events=3, max_rounds=10)
        for index in range(4):
            recorder.emit(
                f"trace-{index}",
                session_id="session-1",
                round_seq=index,
                stage=f"stage-{index}",
            )

        payload = recorder.list_rounds(limit=10)

        self.assertEqual(payload["stats"]["event_count"], 3)
        self.assertEqual([item["trace_id"] for item in payload["items"]], ["trace-3", "trace-2", "trace-1"])

    def test_round_detail_includes_event_timing_deltas(self) -> None:
        recorder = TraceRecorder(max_events=20, max_rounds=10)

        with patch("gateway.trace_recorder.time.time", side_effect=[100.0, 100.25, 101.0]):
            recorder.emit("trace-1", session_id="session-1", round_seq=1, stage="audio_received")
            recorder.emit("trace-1", session_id="session-1", round_seq=1, stage="audio_decoded")
            recorder.emit("trace-1", session_id="session-1", round_seq=1, stage="asr_done")

        events = recorder.get_round("trace-1")["trace"]["events"]

        self.assertEqual(0.0, events[0]["since_round_start_ms"])
        self.assertIsNone(events[0]["since_previous_event_ms"])
        self.assertEqual(250.0, events[1]["since_round_start_ms"])
        self.assertEqual(250.0, events[1]["since_previous_event_ms"])
        self.assertEqual(1000.0, events[2]["since_round_start_ms"])
        self.assertEqual(750.0, events[2]["since_previous_event_ms"])

    def test_round_summary_aggregates_observability_metrics(self) -> None:
        recorder = TraceRecorder(max_events=20, max_rounds=10)

        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="request_dequeued",
            summary={"queue_size": 1},
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="audio_decoded",
            summary={"audio_bytes": 3200, "audio_duration_ms": 100.0},
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="asr_done",
            summary={
                "metadata": {
                    "metadata": {
                        "asr_queue_wait_ms": 12.34,
                        "asr_inference_ms": 45.67,
                        "audio_bytes": 1600,
                    }
                }
            },
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="llm_first_token",
            duration_ms=123.45,
            summary={
                "llm_selected_tool_name": "utils_remote__get_now_context",
                "llm_tool_choice_mode": "none",
                "llm_tool_call_count": 1,
            },
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="tts_first_audio",
            duration_ms=234.56,
            summary={"ws_send_ms": 7.89},
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="llm_tts_interrupted",
            summary={"timeout": False},
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="client_playback_completed",
            summary={
                "client_playback_completed": True,
                "client_playback_chunks": 3,
                "client_playback_samples": 3200,
                "client_playback_underruns": 1,
                "client_playback_zero_fill_samples": 160,
                "client_playback_max_buffered_samples": 6400,
            },
        )

        metrics = recorder.get_round("trace-1")["trace"]["metrics"]

        self.assertEqual(1.0, metrics["queue_size"])
        self.assertEqual(3200.0, metrics["audio_bytes"])
        self.assertEqual(100.0, metrics["audio_duration_ms"])
        self.assertEqual(12.3, metrics["asr_queue_wait_ms"])
        self.assertEqual(45.7, metrics["asr_inference_ms"])
        self.assertEqual(123.5, metrics["llm_first_token_ms"])
        self.assertEqual(
            "utils_remote__get_now_context", metrics["llm_selected_tool_name"]
        )
        self.assertEqual("none", metrics["llm_tool_choice_mode"])
        self.assertEqual(1.0, metrics["llm_tool_call_count"])
        self.assertEqual(234.6, metrics["tts_first_audio_ms"])
        self.assertEqual(7.9, metrics["ws_send_ms"])
        self.assertTrue(metrics["cancelled"])
        self.assertFalse(metrics["timeout"])
        self.assertTrue(metrics["client_playback_completed"])
        self.assertEqual(3.0, metrics["client_playback_chunks"])
        self.assertEqual(3200.0, metrics["client_playback_samples"])
        self.assertEqual(1.0, metrics["client_playback_underruns"])
        self.assertEqual(160.0, metrics["client_playback_zero_fill_samples"])
        self.assertEqual(6400.0, metrics["client_playback_max_buffered_samples"])

    def test_round_summary_includes_latency_diagnosis(self) -> None:
        recorder = TraceRecorder(max_events=20, max_rounds=10)

        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="asr_done",
            summary={"metadata": {"asr_inference_ms": 120.0}},
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="llm_first_token",
            duration_ms=4200.0,
        )
        recorder.emit(
            "trace-1",
            session_id="session-1",
            round_seq=1,
            stage="tts_first_audio",
            duration_ms=800.0,
            summary={"client_playback_underruns": 2},
        )

        diagnosis = recorder.get_round("trace-1")["trace"]["diagnosis"]

        self.assertEqual("slow", diagnosis["severity"])
        self.assertEqual("llm_first_token_ms", diagnosis["bottleneck_metric"])
        self.assertEqual("LLM first token", diagnosis["bottleneck_label"])
        self.assertEqual(4200.0, diagnosis["bottleneck_ms"])
        self.assertIn("client_playback_underruns=2.0", diagnosis["signals"])
        self.assertEqual("llm_first_token_ms", diagnosis["breakdown"][0]["metric"])

    def test_round_diagnosis_prefers_timeout_and_cancelled_severity(self) -> None:
        recorder = TraceRecorder(max_events=20, max_rounds=10)

        recorder.emit(
            "trace-timeout",
            session_id="session-1",
            round_seq=1,
            stage="llm_tts_timeout",
            status="timeout",
            summary={"llm_first_token_ms": 100.0},
        )
        recorder.emit(
            "trace-cancelled",
            session_id="session-1",
            round_seq=2,
            stage="llm_tts_interrupted",
            summary={"llm_first_token_ms": 100.0},
        )

        timeout_diagnosis = recorder.get_round("trace-timeout")["trace"]["diagnosis"]
        cancelled_diagnosis = recorder.get_round("trace-cancelled")["trace"]["diagnosis"]

        self.assertEqual("timeout", timeout_diagnosis["severity"])
        self.assertIn("timeout", timeout_diagnosis["signals"])
        self.assertEqual("cancelled", cancelled_diagnosis["severity"])
        self.assertIn("cancelled", cancelled_diagnosis["signals"])


if __name__ == "__main__":
    unittest.main()
