from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch, sentinel

from llm.llm_client import ImprovedQwenLLMClient


class _FakeCompletions:
    def __init__(self, chunks=None) -> None:
        self.last_params: dict | None = None
        self.chunks = chunks or [
            SimpleNamespace(content="ok", tool_calls=None),
        ]

    def create(self, **params):
        self.last_params = params
        return iter(
            [
                SimpleNamespace(choices=[SimpleNamespace(delta=chunk)])
                for chunk in self.chunks
            ]
        )


class _FakeOpenAI:
    def __init__(self, chunks=None) -> None:
        self.completions = _FakeCompletions(chunks)
        self.chat = SimpleNamespace(completions=self.completions)


class LLMClientExtraBodyTest(unittest.TestCase):
    def test_default_openai_client_disables_trusting_proxy_environment(self) -> None:
        with (
            patch("llm.llm_client.DefaultHttpxClient") as http_client_factory,
            patch("llm.llm_client.OpenAI") as openai_factory,
        ):
            http_client_factory.return_value = sentinel.http_client

            client = ImprovedQwenLLMClient(
                api_key="test-key",
                base_url="http://example.invalid/v1",
            )

        http_client_factory.assert_called_once_with(timeout=60.0, trust_env=False)
        openai_factory.assert_called_once_with(
            api_key="test-key",
            base_url="http://example.invalid/v1",
            http_client=sentinel.http_client,
        )
        self.assertIs(client.client, openai_factory.return_value)

    def test_qwen3_models_disable_thinking_by_default(self) -> None:
        fake = _FakeOpenAI()
        client = ImprovedQwenLLMClient(api_key="test-key", base_url="http://example.invalid/v1")
        client.client = fake
        messages = [{"role": "user", "content": "你好"}]

        chunks = list(client._call_llm(
            messages=messages,
            model_name="qwen3-5-9b",
        ))

        self.assertEqual(chunks, [{"type": "text", "content": "ok"}])
        self.assertEqual(
            fake.completions.last_params["extra_body"],
            {"chat_template_kwargs": {"enable_thinking": False}},
        )
        self.assertEqual([{"role": "user", "content": "你好"}], messages)
        self.assertEqual(
            [{"role": "user", "content": "你好/no_think"}],
            fake.completions.last_params["messages"],
        )

    def test_qwen3_no_think_hint_prefers_system_message(self) -> None:
        fake = _FakeOpenAI()
        client = ImprovedQwenLLMClient(api_key="test-key", base_url="http://example.invalid/v1")
        client.client = fake

        list(client._call_llm(
            messages=[
                {"role": "system", "content": "你是语音助手。"},
                {"role": "user", "content": "你好"},
            ],
            model_name="qwen3-5-9b",
        ))

        self.assertEqual(
            [
                {"role": "system", "content": "你是语音助手。\n/no_think"},
                {"role": "user", "content": "你好"},
            ],
            fake.completions.last_params["messages"],
        )

    def test_qwen3_no_think_hint_preserves_multimodal_user_content(self) -> None:
        fake = _FakeOpenAI()
        client = ImprovedQwenLLMClient(api_key="test-key", base_url="http://example.invalid/v1")
        client.client = fake
        user_content = [
            {"type": "text", "text": "这是什么"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,/9j/2Q=="},
            },
        ]

        list(client._call_llm(
            messages=[
                {"role": "system", "content": "你是视觉助手。"},
                {"role": "user", "content": user_content},
            ],
            model_name="qwen3-5-9b",
        ))

        self.assertEqual(
            "你是视觉助手。\n/no_think",
            fake.completions.last_params["messages"][0]["content"],
        )
        self.assertEqual(
            user_content,
            fake.completions.last_params["messages"][1]["content"],
        )

    def test_qwen3_reasoning_chunks_are_observed_but_not_yielded(self) -> None:
        fake = _FakeOpenAI(
            chunks=[
                SimpleNamespace(content=None, reasoning_content="先想一下", tool_calls=None),
                SimpleNamespace(content="直接回答", reasoning_content=None, tool_calls=None),
            ]
        )
        client = ImprovedQwenLLMClient(api_key="test-key", base_url="http://example.invalid/v1")
        client.client = fake

        with self.assertLogs("llm.llm_client", level="WARNING") as logs:
            chunks = list(client._call_llm(
                messages=[{"role": "user", "content": "你好"}],
                model_name="qwen3-5-9b",
            ))

        self.assertEqual(chunks, [{"type": "text", "content": "直接回答"}])
        self.assertTrue(any("reasoning_content" in line for line in logs.output))

    def test_non_qwen3_models_keep_original_request_shape(self) -> None:
        fake = _FakeOpenAI()
        client = ImprovedQwenLLMClient(api_key="test-key", base_url="http://example.invalid/v1")
        client.client = fake

        list(client._call_llm(
            messages=[{"role": "user", "content": "你好"}],
            model_name="qwen-plus",
        ))

        self.assertNotIn("extra_body", fake.completions.last_params)


if __name__ == "__main__":
    unittest.main()
