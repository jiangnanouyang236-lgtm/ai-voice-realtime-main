from __future__ import annotations

import struct
import unittest

from tts.audio_resampler import resample_pcm16le


class TTSAudioResamplerTest(unittest.TestCase):
    def test_resamples_pcm16le_24k_to_16k_length(self) -> None:
        samples = list(range(240))
        audio = struct.pack("<240h", *samples)

        resampled = resample_pcm16le(audio, 24000, 16000)

        self.assertEqual(160 * 2, len(resampled))
        output = struct.unpack("<160h", resampled)
        self.assertEqual(0, output[0])
        self.assertGreater(output[-1], output[0])

    def test_same_rate_returns_original_bytes(self) -> None:
        audio = struct.pack("<4h", 1, -2, 3, -4)

        self.assertEqual(audio, resample_pcm16le(audio, 16000, 16000))


if __name__ == "__main__":
    unittest.main()
