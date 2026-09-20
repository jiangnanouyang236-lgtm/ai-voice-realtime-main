import asyncio
from types import SimpleNamespace

from gateway.asr_pipeline import process_asr_audio


def test_process_asr_audio_returns_text_metadata_and_timeout():
    stub = _FakeSTTStub()
    logger = _FakeLogger()

    text, duration_ms, metadata = asyncio.run(
        process_asr_audio(
            b"fake-wav",
            "session-1",
            get_stt_stub=lambda: stub,
            wav_to_pcm_fn=lambda audio: (b"\x00\x00", 16000),
            stt_metadata_fn=_fake_stt_metadata,
            stt_rpc_timeout_sec=2.5,
            audio_format="pcm",
            logger=logger,
        )
    )

    assert text == "你好。"
    assert duration_ms >= 0
    assert metadata["raw_text"] == "你好。"
    assert stub.timeout == 2.5
    assert stub.request.audio_data == b"\x00\x00"
    assert stub.request.format == "pcm"
    assert stub.request.sample_rate == 16000


def test_process_asr_audio_returns_empty_result_on_failure():
    logger = _FakeLogger()

    text, duration_ms, metadata = asyncio.run(
        process_asr_audio(
            b"fake-wav",
            "session-1",
            get_stt_stub=lambda: _DeadlineSTTStub(),
            wav_to_pcm_fn=lambda audio: (b"\x00\x00", 16000),
            stt_metadata_fn=_fake_stt_metadata,
            stt_rpc_timeout_sec=0.1,
            audio_format="pcm",
            logger=logger,
        )
    )

    assert text is None
    assert duration_ms == 0
    assert metadata == {}
    assert logger.error_messages


def _fake_stt_metadata(response):
    return {
        "confidence": 0.9,
        "confidence_source": "provider",
        "language": "zh",
        "emotion": "neutral",
        "event_type": "speech",
        "raw_text": response.raw_text,
        "tags": [],
        "metadata": {},
    }


class _FakeSTTStub:
    def __init__(self):
        self.timeout = None
        self.request = None

    def RecognizeSpeech(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        return SimpleNamespace(text="你好。", raw_text="你好。")


class _DeadlineSTTStub:
    def RecognizeSpeech(self, request, timeout=None):
        raise TimeoutError("deadline")


class _FakeLogger:
    def __init__(self):
        self.info_messages = []
        self.error_messages = []

    def info(self, message, *args):
        self.info_messages.append((message, args))

    def error(self, message, *args):
        self.error_messages.append((message, args))
