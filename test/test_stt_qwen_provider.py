from concurrent.futures import ThreadPoolExecutor
import grpc
import io
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, sentinel
import wave

from stt import stt_service_pb2


class _FakeTranscriptions:
    def __init__(self, content="今天天气不错。"):
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.content)


class _FakeOpenAIClient:
    def __init__(self, content="今天天气不错。"):
        self.audio = SimpleNamespace(transcriptions=_FakeTranscriptions(content))


class QwenASRProviderTest(unittest.TestCase):
    def test_qwen_provider_default_client_disables_trusting_proxy_environment(self):
        from stt.asr_providers import QwenASRProvider

        with (
            patch("stt.asr_providers.DefaultHttpxClient") as http_client_factory,
            patch("stt.asr_providers.OpenAI") as openai_factory,
        ):
            http_client_factory.return_value = sentinel.http_client

            provider = QwenASRProvider(
                base_url="http://asr.local/v1",
                api_key="test-key",
                model="qwen-asr-local",
                timeout=12,
            )

        http_client_factory.assert_called_once_with(timeout=12, trust_env=False)
        openai_factory.assert_called_once_with(
            api_key="test-key",
            base_url="http://asr.local/v1",
            http_client=sentinel.http_client,
        )
        self.assertIs(provider.client, openai_factory.return_value)

    def test_qwen_provider_uploads_pcm_as_wav_transcription_file(self):
        from stt.asr_providers import QwenASRProvider

        pcm = b"\x01\x00\x02\x00" * 80
        client = _FakeOpenAIClient("青岛今天有雨。")
        provider = QwenASRProvider(
            base_url="http://asr.local/v1",
            api_key="test-key",
            model="qwen-asr-local",
            client=client,
        )

        result = provider.recognize_from_pcm(pcm, sample_rate=16000, language="zh")

        call = client.audio.transcriptions.calls[0]
        self.assertEqual(call["model"], "qwen-asr-local")
        self.assertEqual(call["temperature"], 0)
        self.assertEqual(call["response_format"], "json")
        self.assertEqual(call["language"], "zh")
        filename, wav_bytes, mime_type = call["file"]
        self.assertEqual(filename, "audio.wav")
        self.assertEqual(mime_type, "audio/wav")

        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 16000)
            self.assertEqual(wav_file.readframes(wav_file.getnframes()), pcm)

        self.assertEqual(result["text"], "青岛今天有雨。")
        self.assertEqual(result["raw_text"], "青岛今天有雨。")
        self.assertEqual(result["language"], "zh")
        self.assertEqual(result["event_type"], "speech")
        self.assertEqual(result["confidence_source"], "unavailable")
        self.assertEqual(result["metadata"]["provider"], "qwen")

    def test_create_stt_provider_uses_qwen_when_configured(self):
        from stt.asr_providers import create_stt_provider

        with patch.dict(
            os.environ,
            {
                "STT_PROVIDER": "qwen",
                "QWEN_ASR_BASE_URL": "http://asr.local/v1",
                "QWEN_ASR_API_KEY": "asr-key",
                "QWEN_ASR_MODEL": "qwen-asr-local",
            },
        ):
            with patch("stt.asr_providers.QwenASRProvider") as provider_cls:
                provider = create_stt_provider()

        self.assertIs(provider, provider_cls.return_value)
        provider_cls.assert_called_once()
        _, kwargs = provider_cls.call_args
        self.assertEqual(kwargs["base_url"], "http://asr.local/v1")
        self.assertEqual(kwargs["api_key"], "asr-key")
        self.assertEqual(kwargs["model"], "qwen-asr-local")

    def test_asr_providers_import_loads_project_dotenv_side_effect(self):
        import config
        import stt.asr_providers

        self.assertIs(config, stt.asr_providers.config)

    def test_qwen_provider_normalizes_root_base_url_for_openai_sdk(self):
        from stt.asr_providers import QwenASRProvider

        client = _FakeOpenAIClient()

        root_provider = QwenASRProvider(
            base_url="http://asr.local:15100",
            api_key="test-key",
            model="qwen-asr-local",
            client=client,
        )
        v1_provider = QwenASRProvider(
            base_url="http://asr.local:15100/v1",
            api_key="test-key",
            model="qwen-asr-local",
            client=client,
        )
        endpoint_provider = QwenASRProvider(
            base_url="http://asr.local:15100/v1/audio/transcriptions",
            api_key="test-key",
            model="qwen-asr-local",
            client=client,
        )

        self.assertEqual(root_provider.base_url, "http://asr.local:15100/v1")
        self.assertEqual(v1_provider.base_url, "http://asr.local:15100/v1")
        self.assertEqual(endpoint_provider.base_url, "http://asr.local:15100/v1")


class STTGrpcServerProviderTest(unittest.TestCase):
    def test_default_concurrency_is_eight(self):
        from stt.stt_grpc_server import _default_max_concurrent_inferences

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_default_max_concurrent_inferences(), 8)

    def test_unsupported_provider_fails_fast(self):
        from stt.asr_providers import create_stt_provider

        with patch.dict(os.environ, {"STT_PROVIDER": "sensevoice"}, clear=True):
            with self.assertRaisesRegex(ValueError, "only 'qwen' is supported"):
                create_stt_provider()

    def test_stt_max_concurrency_env_overrides_provider_default(self):
        from stt.stt_grpc_server import _default_max_concurrent_inferences

        with patch.dict(
            os.environ,
            {"STT_PROVIDER": "qwen", "STT_MAX_CONCURRENT_INFERENCES": "16"},
            clear=False,
        ):
            self.assertEqual(_default_max_concurrent_inferences(), 16)

    def test_recognize_speech_uses_injected_provider_for_pcm(self):
        from stt.stt_grpc_server import STTServiceServicer

        class FakeProvider:
            def __init__(self):
                self.calls = []

            def recognize_from_pcm(self, pcm_data, sample_rate=16000, language=None):
                self.calls.append((pcm_data, sample_rate, language))
                return {
                    "text": "你好。",
                    "raw_text": "你好。",
                    "language": "zh",
                    "event_type": "speech",
                    "confidence_source": "unavailable",
                    "metadata": {"provider": "fake"},
                }

        provider = FakeProvider()
        service = STTServiceServicer(stt_client=provider)
        request = stt_service_pb2.AudioRequest(
            audio_data=b"\x00\x00",
            format="pcm",
            sample_rate=16000,
            language="zh",
        )

        response = service.RecognizeSpeech(request, context=None)

        self.assertEqual(provider.calls, [(b"\x00\x00", 16000, "zh")])
        self.assertEqual(response.text, "你好。")
        self.assertEqual(response.language, "zh")
        self.assertEqual(response.event_type, "speech")
        metadata = json.loads(response.metadata_json)
        self.assertEqual(metadata["provider"], "fake")
        self.assertEqual(metadata["stt_provider"], "FakeProvider")
        self.assertEqual(metadata["stt_request_kind"], "unary")
        self.assertEqual(metadata["audio_bytes"], 2)
        self.assertEqual(metadata["sample_rate"], 16000)
        self.assertGreaterEqual(metadata["asr_queue_wait_ms"], 0)
        self.assertGreaterEqual(metadata["asr_inference_ms"], 0)

    def test_stream_recognize_adds_queue_and_inference_metrics(self):
        from stt.stt_grpc_server import STTServiceServicer

        class FakeProvider:
            def recognize_from_pcm(self, pcm_data, sample_rate=16000, language=None):
                return {
                    "text": "流式结果。",
                    "raw_text": "流式结果。",
                    "confidence_source": "unavailable",
                    "metadata": {"provider": "fake"},
                }

        service = STTServiceServicer(stt_client=FakeProvider())
        chunks = [
            stt_service_pb2.AudioChunk(data=b"\x00\x00", is_final=False),
            stt_service_pb2.AudioChunk(data=b"\x01\x00", is_final=True),
        ]

        responses = list(service.StreamRecognize(iter(chunks), context=None))

        self.assertEqual(1, len(responses))
        self.assertEqual("流式结果。", responses[0].text)
        metadata = json.loads(responses[0].metadata_json)
        self.assertEqual(metadata["stt_request_kind"], "stream_collect")
        self.assertEqual(metadata["audio_bytes"], 4)
        self.assertGreaterEqual(metadata["asr_queue_wait_ms"], 0)
        self.assertGreaterEqual(metadata["asr_inference_ms"], 0)

    def test_queue_wait_metric_increases_when_concurrency_is_saturated(self):
        from stt.stt_grpc_server import STTServiceServicer

        class SlowProvider:
            def recognize_from_pcm(self, pcm_data, sample_rate=16000, language=None):
                time.sleep(0.08)
                return {
                    "text": "慢请求。",
                    "raw_text": "慢请求。",
                    "confidence_source": "unavailable",
                    "metadata": {"provider": "slow-fake"},
                }

        service = STTServiceServicer(
            max_concurrent_inferences=1,
            stt_client=SlowProvider(),
        )

        def call_recognize():
            request = stt_service_pb2.AudioRequest(
                audio_data=b"\x00\x00",
                format="pcm",
                sample_rate=16000,
            )
            return service.RecognizeSpeech(request, context=None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(call_recognize) for _ in range(2)]
            responses = [future.result(timeout=1.0) for future in futures]

        queue_waits = sorted(
            json.loads(response.metadata_json)["asr_queue_wait_ms"]
            for response in responses
        )
        self.assertLess(queue_waits[0], queue_waits[1])
        self.assertGreaterEqual(queue_waits[1], 40)

    def test_queue_wait_times_out_when_concurrency_stays_saturated(self):
        from stt.stt_grpc_server import STTServiceServicer

        started = threading.Event()
        release = threading.Event()

        class BlockingProvider:
            def recognize_from_pcm(self, pcm_data, sample_rate=16000, language=None):
                started.set()
                release.wait(timeout=1.0)
                return {
                    "text": "完成。",
                    "raw_text": "完成。",
                    "confidence_source": "unavailable",
                    "metadata": {"provider": "blocking-fake"},
                }

        service = STTServiceServicer(
            max_concurrent_inferences=1,
            inference_queue_timeout_sec=0.05,
            stt_client=BlockingProvider(),
        )
        request = stt_service_pb2.AudioRequest(
            audio_data=b"\x00\x00",
            format="pcm",
            sample_rate=16000,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(service.RecognizeSpeech, request, None)
            self.assertTrue(started.wait(timeout=1.0))

            context = _FakeGrpcContext()
            response = service.RecognizeSpeech(request, context)

            release.set()
            first.result(timeout=1.0)

        self.assertEqual("", response.text)
        self.assertEqual(grpc.StatusCode.RESOURCE_EXHAUSTED, context.code)
        self.assertIn("queue wait exceeded", context.details)

    def test_cancelled_request_does_not_wait_for_inference_slot(self):
        from stt.stt_grpc_server import STTServiceServicer

        class NeverCalledProvider:
            def recognize_from_pcm(self, pcm_data, sample_rate=16000, language=None):
                raise AssertionError("provider should not be called for cancelled request")

        service = STTServiceServicer(
            max_concurrent_inferences=1,
            inference_queue_timeout_sec=1.0,
            stt_client=NeverCalledProvider(),
        )
        self.assertTrue(service.inference_semaphore.acquire(timeout=0.1))
        try:
            context = _FakeGrpcContext(active=False)
            request = stt_service_pb2.AudioRequest(
                audio_data=b"\x00\x00",
                format="pcm",
                sample_rate=16000,
            )

            response = service.RecognizeSpeech(request, context)
        finally:
            service.inference_semaphore.release()

        self.assertEqual("", response.text)
        self.assertEqual(grpc.StatusCode.CANCELLED, context.code)


class _FakeGrpcContext:
    def __init__(self, active=True):
        self.active = active
        self.code = None
        self.details = ""

    def is_active(self):
        return self.active

    def set_code(self, code):
        self.code = code

    def set_details(self, details):
        self.details = details


if __name__ == "__main__":
    unittest.main()
