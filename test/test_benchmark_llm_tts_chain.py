from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_llm_tts_chain.py"
SPEC = importlib.util.spec_from_file_location("benchmark_llm_tts_chain", SCRIPT_PATH)
bench = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


class BenchmarkLLMTTSChainTest(unittest.TestCase):
    def test_pcm_duration_ms(self):
        self.assertEqual(1000.0, bench.pcm_duration_ms(32_000, 16_000))

    def test_generate_random_prompts_returns_unique_prompts(self):
        prompts = bench.generate_random_prompts(5, seed=1234)

        self.assertEqual(5, len(prompts))
        self.assertEqual(5, len(set(prompts)))
        self.assertTrue(all("随机链路测试" in prompt for prompt in prompts))

    def test_summarize_results_includes_chain_metrics(self):
        results = [
            bench.ChainResult(
                request_id=1,
                ok=True,
                prompt="你好。",
                prompt_chars=3,
                response_text="你好呀。",
                response_chars=4,
                first_tts_text="你好",
                first_tts_text_chars=2,
                tts_setup_ms=20.0,
                llm_first_text_ms=100.0,
                tts_first_commit_ms=110.0,
                tts_first_segment_sent_ms=140.0,
                tts_bridge_buffer_ms=30.0,
                tts_first_audio_ms=300.0,
                tts_first_audio_after_commit_ms=190.0,
                tts_first_audio_after_segment_send_ms=160.0,
                tts_internal_first_pcm_ms=150.0,
                tts_first_text_to_first_pcm_ms=120.0,
                tts_provider_first_pcm_after_send_ms=110.0,
                tts_gateway_after_server_pcm_ms=10.0,
                llm_done_ms=500.0,
                total_ms=900.0,
                audio_bytes=32000,
                audio_duration_ms=1000.0,
                rtf=0.9,
                audio_chunks=4,
                sample_rate=16000,
                status="ok",
            ),
            bench.ChainResult(
                request_id=2,
                ok=False,
                prompt="讲个故事。",
                prompt_chars=5,
                response_text="",
                response_chars=0,
                first_tts_text="",
                first_tts_text_chars=0,
                tts_setup_ms=None,
                llm_first_text_ms=None,
                tts_first_commit_ms=None,
                tts_first_segment_sent_ms=None,
                tts_bridge_buffer_ms=None,
                tts_first_audio_ms=None,
                tts_first_audio_after_commit_ms=None,
                tts_first_audio_after_segment_send_ms=None,
                tts_internal_first_pcm_ms=None,
                tts_first_text_to_first_pcm_ms=None,
                tts_provider_first_pcm_after_send_ms=None,
                tts_gateway_after_server_pcm_ms=None,
                llm_done_ms=None,
                total_ms=100.0,
                audio_bytes=0,
                audio_duration_ms=None,
                rtf=None,
                audio_chunks=0,
                sample_rate=None,
                status="error",
                error="boom",
            ),
        ]

        summary = bench.summarize_results(results)

        self.assertEqual(2, summary["total"])
        self.assertEqual(1, summary["ok"])
        self.assertEqual(1, summary["failed"])
        self.assertEqual(20.0, summary["metrics"]["tts_setup_ms"]["p50"])
        self.assertEqual(100.0, summary["metrics"]["llm_first_text_ms"]["p50"])
        self.assertEqual(300.0, summary["metrics"]["tts_first_audio_ms"]["p50"])
        self.assertEqual(["boom"], summary["errors"])

    def test_render_markdown_report_contains_key_metrics(self):
        args = type("Args", (), {
            "label": "demo",
            "llm_target": "127.0.0.1:50053",
            "tts_target": "127.0.0.1:50052",
            "bot_id": "xiaowen",
            "robot_id": "companion_01",
            "llm_model": "",
            "tts_provider": "local-grpc",
            "tts_profile_id": "default_tts_profile",
            "voice": "serena",
            "cloud_tts_model": "qwen3-tts-flash-realtime",
            "cloud_tts_voice": "Cherry",
            "qwen_mode": "server_commit",
            "speech_rate": 1.0,
            "concurrency": 1,
            "random_prompts": False,
            "random_seed": None,
            "timeout": 60.0,
        })()
        result = bench.ChainResult(
            request_id=1,
            ok=True,
            prompt="你好。",
            prompt_chars=3,
            response_text="你好呀。",
            response_chars=4,
            first_tts_text="你好",
            first_tts_text_chars=2,
            tts_setup_ms=20.0,
            llm_first_text_ms=100.0,
            tts_first_commit_ms=110.0,
            tts_first_segment_sent_ms=140.0,
            tts_bridge_buffer_ms=30.0,
            tts_first_audio_ms=300.0,
            tts_first_audio_after_commit_ms=190.0,
            tts_first_audio_after_segment_send_ms=160.0,
            tts_internal_first_pcm_ms=150.0,
            tts_first_text_to_first_pcm_ms=120.0,
            tts_provider_first_pcm_after_send_ms=110.0,
            tts_gateway_after_server_pcm_ms=10.0,
            llm_done_ms=500.0,
            total_ms=900.0,
            audio_bytes=32000,
            audio_duration_ms=1000.0,
            rtf=0.9,
            audio_chunks=4,
            sample_rate=16000,
            status="ok",
        )

        report = bench.build_report(args, [result])
        markdown = bench.render_markdown_report(report)

        self.assertIn("LLM -> TTS Chain Benchmark", markdown)
        self.assertIn("`llm_first_text_ms`", markdown)
        self.assertIn("`tts_first_audio_ms`", markdown)


if __name__ == "__main__":
    unittest.main()
