from __future__ import annotations

import unittest

import httpx

from llm.vision_context import (
    VISION_PROMPT,
    VisionSnapshotClient,
    build_visual_user_content,
    is_visual_context_intent,
)


class VisionIntentTest(unittest.TestCase):
    def test_visual_prompt_requires_short_coarse_answer(self) -> None:
        self.assertIn("一到两句", VISION_PROMPT)
        self.assertIn("不要列点", VISION_PROMPT)
        self.assertIn("不要尝试辨认图片文字", VISION_PROMPT)

    def test_visual_deictic_phrases_are_detected(self) -> None:
        for text in (
            "这是什么？",
            "帮我看看这个",
            "前面有什么",
            "你看到了什么？",
            "这是谁",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_visual_context_intent(text))

    def test_conversation_references_do_not_fetch_an_image(self) -> None:
        for text in (
            "前面说了什么",
            "你怎么看这个方案",
            "帮我看看天气",
            "这个功能是什么",
            "解释一下这个问题",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_visual_context_intent(text))

    def test_natural_visual_questions_are_detected(self) -> None:
        for text in (
            "你觉得我今天穿的衣服怎么样？",
            "我这身衣服好看吗？",
            "今天穿这个合适吗？",
            "你觉得这个颜色适合我吗？",
            "我手里拿的是什么？",
            "桌上有什么？",
            "这里光线怎么样？",
            "你能认出我穿的颜色吗？",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_visual_context_intent(text))

    def test_compound_tool_and_visual_request_is_left_to_workflow(self) -> None:
        self.assertFalse(
            is_visual_context_intent("帮我查一下今天青岛的天气，再看看我穿得合不合适")
        )


class VisionSnapshotClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_latest_builds_authenticated_request(self) -> None:
        jpeg = b"\xff\xd8snapshot\xff\xd9"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.headers["authorization"],
                "Bearer internal-secret",
            )
            self.assertEqual(request.url.params["session_id"], "session/with space")
            return httpx.Response(
                200,
                content=jpeg,
                headers={
                    "content-type": "image/jpeg",
                    "x-vision-frame-id": "7",
                    "x-vision-captured-at-ms": "1900000000000",
                    "x-vision-age-ms": "320",
                },
            )

        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = VisionSnapshotClient(
            base_url="http://gateway:8282",
            token="internal-secret",
            timeout_sec=1.0,
            async_client=async_client,
        )
        self.addAsyncCleanup(async_client.aclose)

        result = await client.fetch_latest("session/with space")

        self.assertEqual("ok", result.status)
        self.assertIsNotNone(result.snapshot)
        self.assertEqual(jpeg, result.snapshot.jpeg)
        self.assertEqual("7", result.snapshot.frame_id)
        self.assertEqual(320, result.snapshot.age_ms)
        content = build_visual_user_content("这是什么", result.snapshot)
        self.assertEqual("text", content[0]["type"])
        self.assertTrue(
            content[1]["image_url"]["url"].startswith(
                "data:image/jpeg;base64,"
            )
        )

    async def test_fetch_latest_treats_404_as_unavailable(self) -> None:
        async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(404, json={"error": "snapshot_unavailable"})
            )
        )
        client = VisionSnapshotClient(
            base_url="http://gateway:8282",
            token="internal-secret",
            timeout_sec=1.0,
            async_client=async_client,
        )
        self.addAsyncCleanup(async_client.aclose)

        result = await client.fetch_latest("session-1")

        self.assertEqual("unavailable", result.status)
        self.assertIsNone(result.snapshot)
        self.assertEqual(404, result.http_status)

    async def test_fetch_latest_rejects_missing_configuration_and_session(self) -> None:
        client = VisionSnapshotClient(
            base_url="",
            token="",
            timeout_sec=1.0,
        )
        self.addAsyncCleanup(client._client.aclose)

        self.assertEqual("invalid_session", (await client.fetch_latest(" ")).status)
        self.assertEqual("not_configured", (await client.fetch_latest("session-1")).status)

    async def test_fetch_latest_classifies_transport_failures(self) -> None:
        cases = (
            ("timeout", httpx.ReadTimeout("slow gateway")),
            ("network_error", httpx.ConnectError("gateway unavailable")),
        )
        for expected, error in cases:
            with self.subTest(expected=expected):

                async def handler(_request: httpx.Request, error=error) -> httpx.Response:
                    raise error

                async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                client = VisionSnapshotClient(
                    base_url="http://gateway:8282",
                    token="internal-secret",
                    timeout_sec=1.0,
                    async_client=async_client,
                )
                result = await client.fetch_latest("session-1")
                await async_client.aclose()
                self.assertEqual(expected, result.status)
                self.assertIsNone(result.snapshot)

    async def test_fetch_latest_classifies_invalid_gateway_responses(self) -> None:
        cases = (
            ("gateway_error", httpx.Response(401)),
            (
                "invalid_content_type",
                httpx.Response(
                    200,
                    content=b"\xff\xd8ok\xff\xd9",
                    headers={"content-type": "application/json"},
                ),
            ),
            (
                "invalid_image_size",
                httpx.Response(200, content=b"", headers={"content-type": "image/jpeg"}),
            ),
            (
                "invalid_image_size",
                httpx.Response(
                    200,
                    content=b"\xff\xd8" + (b"x" * (5 * 1024 * 1024)) + b"\xff\xd9",
                    headers={"content-type": "image/jpeg"},
                ),
            ),
            (
                "invalid_jpeg",
                httpx.Response(
                    200,
                    content=b"not-a-jpeg",
                    headers={"content-type": "image/jpeg"},
                ),
            ),
        )
        for expected, response in cases:
            with self.subTest(
                expected=expected,
                status=response.status_code,
                size=len(response.content),
            ):
                async_client = httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _request, response=response: response
                    )
                )
                client = VisionSnapshotClient(
                    base_url="http://gateway:8282",
                    token="internal-secret",
                    timeout_sec=1.0,
                    async_client=async_client,
                )
                result = await client.fetch_latest("session-1")
                await async_client.aclose()
                self.assertEqual(expected, result.status)
                self.assertIsNone(result.snapshot)
