from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_asr_streaming.py"
SPEC = importlib.util.spec_from_file_location("benchmark_asr_streaming", SCRIPT_PATH)
bench = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


class BenchmarkAsrStreamingTest(unittest.TestCase):
    def test_normalize_text_removes_punctuation_and_spaces(self) -> None:
        self.assertEqual("森林里的小路", bench.normalize_text(" 森林里的小路。"))

    def test_char_error_rate_tracks_homophone_like_substitution(self) -> None:
        self.assertEqual(0.0, bench.char_error_rate("森林里的小路。", "森林里的小路"))
        self.assertAlmostEqual(1 / 6, bench.char_error_rate("森林里的小路", "森林里的小鹿"))

    def test_parse_network_profile(self) -> None:
        self.assertEqual((20.0, 512.0), bench.parse_network_profile("office:20,512"))

    def test_simulated_network_metrics_uses_opus_upload_size(self) -> None:
        args = type("Args", (), {"network_audio_codec": "opus", "opus_bitrate_kbps": 16.0})()
        item = bench.AudioItem(
            request_id=1,
            text="你好。",
            wav_path="x.wav",
            pcm_bytes=16000 * 2 * 2,
            duration_ms=2000.0,
            sample_rate=16000,
        )

        batch_tail, stream_tail, upload_bytes = bench.simulated_network_metrics(args, item, 250.0, "weak:120,64")

        self.assertEqual(4000, upload_bytes)
        self.assertEqual(870.0, batch_tail)
        self.assertEqual(120.0, stream_tail)


if __name__ == "__main__":
    unittest.main()

