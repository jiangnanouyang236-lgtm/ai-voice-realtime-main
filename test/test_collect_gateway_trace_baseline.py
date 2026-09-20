from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "collect_gateway_trace_baseline.py"
SPEC = importlib.util.spec_from_file_location("collect_gateway_trace_baseline", SCRIPT_PATH)
baseline = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(baseline)


class CollectGatewayTraceBaselineTest(unittest.TestCase):
    def test_percentile_interpolates_values(self) -> None:
        self.assertEqual(1.0, baseline.percentile([1], 95))
        self.assertEqual(2.5, baseline.percentile([1, 2, 3, 4], 50))
        self.assertEqual(3.9, baseline.percentile([1, 2, 3, 4], 95))

    def test_summarize_rounds_collects_metrics(self) -> None:
        summary = baseline.summarize_rounds(
            [
                {
                    "status": "ok",
                    "metrics": {
                        "llm_first_token_ms": 100.0,
                        "llm_router_ms": 12.0,
                        "llm_mcp_enabled": True,
                        "tts_first_audio_ms": 200.0,
                        "client_playback_completed": True,
                        "client_playback_samples": 32000,
                    },
                },
                {
                    "status": "timeout",
                    "metrics": {
                        "llm_first_token_ms": 300.0,
                        "llm_router_ms": 28.0,
                        "timeout": True,
                        "client_playback_interrupted": True,
                    },
                },
            ]
        )

        self.assertEqual(2, summary["round_count"])
        self.assertEqual({"ok": 1, "timeout": 1}, summary["status_counts"])
        self.assertEqual(
            {"count": 2, "min": 100.0, "avg": 200.0, "p50": 200.0, "p95": 290.0, "max": 300.0},
            summary["numeric_metrics"]["llm_first_token_ms"],
        )
        self.assertEqual(20.0, summary["numeric_metrics"]["llm_router_ms"]["p50"])
        self.assertEqual(1, summary["boolean_metric_true_counts"]["llm_mcp_enabled"])
        self.assertEqual(1, summary["boolean_metric_true_counts"]["timeout"])
        self.assertEqual(1, summary["boolean_metric_true_counts"]["client_playback_completed"])
        self.assertEqual(1, summary["boolean_metric_true_counts"]["client_playback_interrupted"])

    def test_build_report_can_require_client_playback_report(self) -> None:
        payload = {
            "items": [
                {
                    "trace_id": "trace-1",
                    "status": "ok",
                    "metrics": {"client_playback_completed": True},
                },
                {
                    "trace_id": "trace-2",
                    "status": "ok",
                    "metrics": {"llm_total_ms": 1000.0},
                },
            ],
            "stats": {"enabled": True},
            "pagination": {"total": 2},
        }

        report = baseline.build_report(
            payload,
            base_url="http://gateway.local/",
            label="company-office-lan-binary-pcm",
            require_client_playback_report=True,
        )

        self.assertEqual("gateway-trace-baseline/v1", report["schema_version"])
        self.assertEqual("http://gateway.local", report["base_url"])
        self.assertEqual(1, report["summary"]["round_count"])
        self.assertEqual(["trace-1"], [item["trace_id"] for item in report["rounds"]])


if __name__ == "__main__":
    unittest.main()
