from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "benchmark_multimodal_llm.py"
)
SPEC = importlib.util.spec_from_file_location(
    "benchmark_multimodal_llm",
    SCRIPT_PATH,
)
bench = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = bench
SPEC.loader.exec_module(bench)


def tiny_jpeg(width: int = 3, height: int = 2) -> bytes:
    sof_payload = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03"
        + b"\x01\x11\x00"
        + b"\x02\x11\x00"
        + b"\x03\x11\x00"
    )
    sof = b"\xff\xc0" + (len(sof_payload) + 2).to_bytes(2, "big") + sof_payload
    return b"\xff\xd8" + sof + b"\xff\xd9"


def make_result(
    *,
    scenario: str,
    case: str,
    image_label: str,
    request_id: int,
    first_text_ms: float | None = None,
    first_tool_call_ms: float | None = None,
    recognition_ok: bool | None = None,
) -> bench.BenchResult:
    meaningful = (
        first_text_ms
        if first_text_ms is not None
        else first_tool_call_ms
    )
    return bench.BenchResult(
        scenario=scenario,
        case=case,
        image_label=image_label,
        request_id=request_id,
        ok=True,
        status="ok",
        image_bytes=100 if image_label != "none" else 0,
        image_width=3 if image_label != "none" else None,
        image_height=2 if image_label != "none" else None,
        payload_prepare_ms=1.0,
        first_raw_ms=50.0,
        first_text_ms=first_text_ms,
        first_tool_call_ms=first_tool_call_ms,
        first_meaningful_ms=meaningful,
        total_ms=300.0,
        decode_ms=200.0 if first_text_ms is not None else None,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        decode_tokens_per_s=20.0 if first_text_ms is not None else None,
        output_chars=5 if first_text_ms is not None else 0,
        output_text="测试图片" if first_text_ms is not None else "",
        tool_call_names=(
            bench.EXPECTED_TOOL_NAME
            if first_tool_call_ms is not None
            else ""
        ),
        tool_arguments=(
            '{"summary":"测试图片","has_person":false}'
            if first_tool_call_ms is not None
            else ""
        ),
        tool_arguments_valid=(
            True if first_tool_call_ms is not None else None
        ),
        recognition_ok=recognition_ok,
        expected_keywords="图片" if recognition_ok is not None else "",
        raw_chunks=3,
        text_chunks=2 if first_text_ms is not None else 0,
        tool_chunks=1 if first_tool_call_ms is not None else 0,
        reasoning_chunks=0,
    )


class BenchmarkMultimodalLLMTest(unittest.TestCase):
    def test_jpeg_dimensions_and_nonce_preserve_dimensions(self) -> None:
        data = tiny_jpeg(640, 480)

        varied = bench.jpeg_with_nonce(data, "abc123")

        self.assertEqual((640, 480), bench.jpeg_dimensions(data))
        self.assertEqual((640, 480), bench.jpeg_dimensions(varied))
        self.assertNotEqual(data, varied)
        self.assertIn(b"benchmark:abc123", varied)

    def test_build_messages_keeps_text_baseline_text_only(self) -> None:
        messages = bench.build_messages(
            bench.CASE_TEXT_ONLY,
            image_url=None,
            nonce="n1",
        )

        self.assertIsInstance(messages[-1]["content"], str)
        self.assertNotIn("image_url", str(messages[-1]["content"]))

    def test_build_messages_uses_ephemeral_image_content(self) -> None:
        messages = bench.build_messages(
            bench.CASE_TEXT_IMAGE,
            image_url="data:image/jpeg;base64,AAAA",
            nonce="n2",
        )

        user_content = messages[-1]["content"]
        self.assertEqual("text", user_content[0]["type"])
        self.assertEqual("image_url", user_content[1]["type"])
        self.assertEqual(
            "data:image/jpeg;base64,AAAA",
            user_content[1]["image_url"]["url"],
        )

    def test_summarize_and_compare_image_latency(self) -> None:
        results = [
            make_result(
                scenario="text_only:none",
                case=bench.CASE_TEXT_ONLY,
                image_label="none",
                request_id=1,
                first_text_ms=100.0,
            ),
            make_result(
                scenario="text_only:none",
                case=bench.CASE_TEXT_ONLY,
                image_label="none",
                request_id=2,
                first_text_ms=200.0,
            ),
            make_result(
                scenario="text_image:small",
                case=bench.CASE_TEXT_IMAGE,
                image_label="small",
                request_id=3,
                first_text_ms=300.0,
                recognition_ok=True,
            ),
            make_result(
                scenario="text_image:small",
                case=bench.CASE_TEXT_IMAGE,
                image_label="small",
                request_id=4,
                first_text_ms=400.0,
                recognition_ok=False,
            ),
        ]

        summary = bench.summarize_results(results)
        comparisons = bench.build_comparisons(summary)

        self.assertEqual(
            150.0,
            summary["text_only:none"]["metrics"]["first_text_ms"]["p50"],
        )
        self.assertEqual(
            350.0,
            summary["text_image:small"]["metrics"]["first_text_ms"]["p50"],
        )
        self.assertEqual(
            1,
            summary["text_image:small"]["recognition_passed"],
        )
        self.assertEqual(200.0, comparisons[0]["delta_p50_ms"])

    def test_build_scenarios_never_attaches_image_to_text_tools(self) -> None:
        image = bench.LoadedImage(
            label="small",
            path=Path("/tmp/pic.jpeg"),
            data=tiny_jpeg(),
            mime_type="image/jpeg",
            width=3,
            height=2,
            sha256="abc",
        )

        scenarios = bench.build_scenarios(
            [image],
            [
                bench.CASE_TEXT_ONLY,
                bench.CASE_TEXT_TOOLS,
                bench.CASE_TEXT_IMAGE_TOOLS,
            ],
        )

        by_case = {scenario.case: scenario for scenario in scenarios}
        self.assertIsNone(by_case[bench.CASE_TEXT_ONLY].image)
        self.assertIsNone(by_case[bench.CASE_TEXT_TOOLS].image)
        self.assertIs(
            image,
            by_case[bench.CASE_TEXT_IMAGE_TOOLS].image,
        )

    def test_validate_tool_arguments_requires_complete_schema(self) -> None:
        self.assertTrue(
            bench.validate_tool_arguments(
                '{"summary":"教室里有学生","has_person":true}'
            )
        )
        self.assertFalse(
            bench.validate_tool_arguments(
                '{"summary":"被截断","has_person":false'
            )
        )
        self.assertFalse(
            bench.validate_tool_arguments(
                '{"summary":"","has_person":"false"}'
            )
        )


if __name__ == "__main__":
    unittest.main()
