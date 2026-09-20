from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_tts_providers.py"
SPEC = importlib.util.spec_from_file_location("benchmark_tts_providers", SCRIPT_PATH)
bench = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


class BenchmarkTtsProvidersTest(unittest.TestCase):
    def test_generate_texts_is_deterministic_and_bounded(self) -> None:
        first = bench.generate_texts(5, seed=123, min_chars=20, max_chars=40)
        second = bench.generate_texts(5, seed=123, min_chars=20, max_chars=40)

        self.assertEqual(first, second)
        self.assertEqual(5, len(first))
        self.assertTrue(all(1 <= len(text) <= 40 for text in first))

    def test_split_text_chunks_by_character_count(self) -> None:
        self.assertEqual(["abc", "def", "g"], bench.split_text("abcdefg", 3))

    def test_pcm_duration_ms_uses_sample_rate_width_and_channels(self) -> None:
        self.assertEqual(1000.0, bench.pcm_duration_ms(48_000, 24_000))

    def test_endpoint_builders_accept_base_or_full_urls(self) -> None:
        self.assertEqual(
            "http://127.0.0.1:15120/v1/audio/speech",
            bench.build_http_speech_url("http://127.0.0.1:15120"),
        )
        self.assertEqual(
            "http://127.0.0.1:15120/v1/audio/speech",
            bench.build_http_speech_url("http://127.0.0.1:15120/v1/audio/speech"),
        )
        self.assertEqual(
            "ws://127.0.0.1:15120/v1/audio/speech/stream",
            bench.build_ws_speech_stream_url("ws://127.0.0.1:15120"),
        )
        self.assertEqual(
            "ws://127.0.0.1:15120/v1/audio/speech/stream",
            bench.build_ws_speech_stream_url("ws://127.0.0.1:15120/v1/audio/speech/stream"),
        )

    def test_summarize_results_only_uses_successful_metrics(self) -> None:
        results = [
            bench.BenchResult(
                provider="p",
                request_id=1,
                ok=True,
                text="你好",
                text_chars=2,
                ttfb_ms=100.0,
                total_ms=500.0,
                audio_bytes=48_000,
                audio_duration_ms=1000.0,
                rtf=0.5,
                chunks=4,
                sample_rate=24_000,
                first_audio_before_input_done=True,
            ),
            bench.BenchResult(
                provider="p",
                request_id=2,
                ok=True,
                text="你好呀",
                text_chars=3,
                ttfb_ms=300.0,
                total_ms=900.0,
                audio_bytes=96_000,
                audio_duration_ms=2000.0,
                rtf=0.45,
                chunks=8,
                sample_rate=24_000,
            ),
            bench.BenchResult(
                provider="p",
                request_id=3,
                ok=False,
                text="失败",
                text_chars=2,
                ttfb_ms=None,
                total_ms=1000.0,
                audio_bytes=0,
                audio_duration_ms=None,
                rtf=None,
                chunks=0,
                sample_rate=None,
                error="timeout",
            ),
        ]

        summary = bench.summarize_results(results)

        self.assertEqual(3, summary["total"])
        self.assertEqual(2, summary["ok"])
        self.assertEqual(1, summary["failed"])
        self.assertEqual(0.6667, summary["success_rate"])
        self.assertEqual(200.0, summary["metrics"]["ttfb_ms"]["p50"])
        self.assertEqual(290.0, summary["metrics"]["ttfb_ms"]["p95"])
        self.assertEqual(1, summary["boolean_counts"]["first_audio_before_input_done"])
        self.assertEqual(["timeout"], summary["errors"])

    def test_render_markdown_report_contains_key_metrics(self) -> None:
        report = {
            "provider": "current-grpc",
            "generated_at": "2026-06-22T00:00:00+08:00",
            "concurrency": 1,
            "summary": {
                "total": 1,
                "ok": 1,
                "success_rate": 1.0,
                "errors": [],
                "metrics": {
                    "ttfb_ms": {
                        "count": 1,
                        "p50": 100.0,
                        "p95": 100.0,
                        "avg": 100.0,
                        "min": 100.0,
                        "max": 100.0,
                    }
                },
                "boolean_counts": {},
            },
        }

        markdown = bench.render_markdown_report(report)

        self.assertIn("TTS Provider Benchmark: current-grpc", markdown)
        self.assertIn("`ttfb_ms`", markdown)


if __name__ == "__main__":
    unittest.main()
