#!/usr/bin/env python3
"""Benchmark an OpenAI-compatible multimodal LLM endpoint.

The benchmark is intentionally independent from the production Gateway/LLM
chain. It measures text, image, mixed text+image, text+tools, and
image+tools requests without changing runtime routing or session history.
"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import sys
import time
import uuid
from typing import Any

from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]

CASE_TEXT_ONLY = "text_only"
CASE_TEXT_TOOLS = "text_tools"
CASE_IMAGE_ONLY = "image_only"
CASE_TEXT_IMAGE = "text_image"
CASE_TEXT_IMAGE_TOOLS = "text_image_tools"
ALL_CASES = (
    CASE_TEXT_ONLY,
    CASE_TEXT_TOOLS,
    CASE_IMAGE_ONLY,
    CASE_TEXT_IMAGE,
    CASE_TEXT_IMAGE_TOOLS,
)
IMAGE_CASES = {
    CASE_IMAGE_ONLY,
    CASE_TEXT_IMAGE,
    CASE_TEXT_IMAGE_TOOLS,
}
TOOL_CASES = {
    CASE_TEXT_TOOLS,
    CASE_TEXT_IMAGE_TOOLS,
}
EXPECTED_TOOL_NAME = "report_visible_scene"

SYSTEM_PROMPT = (
    "你是多模态模型能力和性能测试助手。只根据本次请求提供的文字和图片回答，"
    "不要引用其他轮次，不要解释测试过程。如果只收到图片，请用一句中文描述"
    "图片中最主要的可见内容。回答保持简短。\n/no_think"
)
VISUAL_PROMPT = "请用一句中文描述图片中最主要的可见内容。"
TEXT_TOOL_PROMPT = (
    "请调用 report_visible_scene 工具，把 summary 设置为“无图片基线”，"
    "把 has_person 设置为 false，不要直接输出文字。"
)
IMAGE_TOOL_PROMPT = (
    "请观察图片并调用 report_visible_scene 工具报告最主要的可见内容，"
    "不要直接输出文字。"
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": EXPECTED_TOOL_NAME,
            "description": "报告当前图片中最主要的可见内容，用于验证多模态工具调用能力。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "一句中文场景摘要。",
                    },
                    "has_person": {
                        "type": "boolean",
                        "description": "图片中是否能看到人。",
                    },
                },
                "required": ["summary", "has_person"],
                "additionalProperties": False,
            },
        },
    }
]
NO_THINKING_EXTRA_BODY = {
    "chat_template_kwargs": {
        "enable_thinking": False,
    }
}


@dataclass(frozen=True)
class LoadedImage:
    label: str
    path: Path
    data: bytes
    mime_type: str
    width: int
    height: int
    sha256: str


@dataclass(frozen=True)
class Scenario:
    case: str
    image: LoadedImage | None = None

    @property
    def image_label(self) -> str:
        return self.image.label if self.image else "none"

    @property
    def key(self) -> str:
        return f"{self.case}:{self.image_label}"


@dataclass
class BenchResult:
    scenario: str
    case: str
    image_label: str
    request_id: int
    ok: bool
    status: str
    image_bytes: int
    image_width: int | None
    image_height: int | None
    payload_prepare_ms: float | None
    first_raw_ms: float | None
    first_text_ms: float | None
    first_tool_call_ms: float | None
    first_meaningful_ms: float | None
    total_ms: float | None
    decode_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    decode_tokens_per_s: float | None
    output_chars: int
    output_text: str
    tool_call_names: str
    tool_arguments: str
    tool_arguments_valid: bool | None
    recognition_ok: bool | None
    expected_keywords: str
    raw_chunks: int
    text_chunks: int
    tool_chunks: int
    reasoning_chunks: int
    error: str | None = None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    rank = (len(ordered) - 1) * pct / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(
        ordered[lower] * (1.0 - weight) + ordered[upper] * weight,
        1,
    )


def summarize_numeric(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "avg": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "avg": round(statistics.fmean(values), 1),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "max": round(max(values), 1),
    }


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def summarize_results(results: list[BenchResult]) -> dict[str, Any]:
    grouped: dict[str, list[BenchResult]] = {}
    for result in results:
        grouped.setdefault(result.scenario, []).append(result)

    metrics = (
        "payload_prepare_ms",
        "first_raw_ms",
        "first_text_ms",
        "first_tool_call_ms",
        "first_meaningful_ms",
        "total_ms",
        "decode_ms",
        "prompt_tokens",
        "completion_tokens",
        "decode_tokens_per_s",
    )
    summary: dict[str, Any] = {}
    for scenario, items in grouped.items():
        ok_items = [item for item in items if item.ok]

        def values(name: str) -> list[float]:
            collected: list[float] = []
            for item in ok_items:
                value = _numeric(getattr(item, name))
                if value is not None:
                    collected.append(value)
            return collected

        recognition_items = [
            item for item in items if item.recognition_ok is not None
        ]
        tool_argument_items = [
            item for item in items if item.tool_arguments_valid is not None
        ]
        summary[scenario] = {
            "case": items[0].case,
            "image_label": items[0].image_label,
            "total": len(items),
            "ok": len(ok_items),
            "failed": len(items) - len(ok_items),
            "success_rate": (
                round(len(ok_items) / len(items), 4) if items else 0.0
            ),
            "recognition_checked": len(recognition_items),
            "recognition_passed": sum(
                1 for item in recognition_items if item.recognition_ok
            ),
            "tool_arguments_checked": len(tool_argument_items),
            "tool_arguments_passed": sum(
                1 for item in tool_argument_items if item.tool_arguments_valid
            ),
            "metrics": {
                name: summarize_numeric(values(name)) for name in metrics
            },
            "errors": [
                item.error for item in items if item.error
            ][:5],
        }
    return summary


def _metric(
    summary: dict[str, Any],
    scenario: str,
    name: str,
    field: str,
) -> float | None:
    value = (
        summary.get(scenario, {})
        .get("metrics", {})
        .get(name, {})
        .get(field)
    )
    return float(value) if isinstance(value, (int, float)) else None


def build_comparisons(summary: dict[str, Any]) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for scenario, row in summary.items():
        case = row["case"]
        if case in {CASE_TEXT_ONLY, CASE_TEXT_TOOLS, CASE_IMAGE_ONLY}:
            continue
        baseline = (
            f"{CASE_TEXT_TOOLS}:none"
            if case == CASE_TEXT_IMAGE_TOOLS
            else f"{CASE_TEXT_ONLY}:none"
        )
        metric_name = (
            "first_tool_call_ms"
            if case == CASE_TEXT_IMAGE_TOOLS
            else "first_text_ms"
        )
        item: dict[str, Any] = {
            "scenario": scenario,
            "baseline": baseline,
            "metric": metric_name,
        }
        for field in ("p50", "p95"):
            current = _metric(summary, scenario, metric_name, field)
            base = _metric(summary, baseline, metric_name, field)
            delta = current - base if current is not None and base is not None else None
            ratio = (
                delta / base * 100.0
                if delta is not None and base is not None and base > 0
                else None
            )
            item[f"{field}_ms"] = round(current, 1) if current is not None else None
            item[f"baseline_{field}_ms"] = (
                round(base, 1) if base is not None else None
            )
            item[f"delta_{field}_ms"] = (
                round(delta, 1) if delta is not None else None
            )
            item[f"delta_{field}_pct"] = (
                round(ratio, 1) if ratio is not None else None
            )
        comparisons.append(item)
    return comparisons


def parse_labeled_path(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("--image 必须使用 label=/path/pic.jpeg")
    label, path_text = raw.split("=", 1)
    label = label.strip()
    path_text = path_text.strip()
    if not label or not path_text:
        raise argparse.ArgumentTypeError("--image 的 label 和路径不能为空")
    return label, Path(path_text).expanduser()


def parse_expected_keywords(raw: str) -> tuple[str, list[str]]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("--expect 必须使用 label=关键词1,关键词2")
    label, keywords_text = raw.split("=", 1)
    keywords = [
        keyword.strip().lower()
        for keyword in keywords_text.split(",")
        if keyword.strip()
    ]
    if not label.strip() or not keywords:
        raise argparse.ArgumentTypeError("--expect 的 label 和关键词不能为空")
    return label.strip(), keywords


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        raise ValueError("文件不是 JPEG")
    offset = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset + 3 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xDA:
            break
        if offset + 2 > len(data):
            break
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(data):
            raise ValueError("JPEG 段长度无效")
        if marker in sof_markers:
            if segment_length < 7:
                raise ValueError("JPEG SOF 段过短")
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if width <= 0 or height <= 0:
                raise ValueError("JPEG 尺寸无效")
            return width, height
        offset += segment_length
    raise ValueError("JPEG 中未找到尺寸信息")


def load_image(label: str, path: Path, *, max_bytes: int) -> LoadedImage:
    resolved = path.resolve()
    data = resolved.read_bytes()
    if not data:
        raise ValueError(f"{resolved}: 图片为空")
    if len(data) > max_bytes:
        raise ValueError(
            f"{resolved}: 图片 {len(data)} bytes 超过限制 {max_bytes} bytes"
        )
    width, height = jpeg_dimensions(data)
    return LoadedImage(
        label=label,
        path=resolved,
        data=data,
        mime_type="image/jpeg",
        width=width,
        height=height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def jpeg_with_nonce(data: bytes, nonce: str) -> bytes:
    """Add a legal JPEG comment so repeated rounds do not reuse image hashes."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("文件不是 JPEG")
    payload = f"benchmark:{nonce}".encode("ascii")
    if len(payload) > 65_533:
        raise ValueError("JPEG comment 太长")
    segment = b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload
    return data[:2] + segment + data[2:]


def image_data_url(data: bytes, mime_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_messages(
    case: str,
    *,
    image_url: str | None,
    nonce: str,
) -> list[dict[str, Any]]:
    system = {
        "role": "system",
        "content": f"{SYSTEM_PROMPT}\n测试编号：{nonce}",
    }
    if case == CASE_IMAGE_ONLY:
        if not image_url:
            raise ValueError("image_only 缺少图片")
        user_content: Any = [
            {
                "type": "image_url",
                "image_url": {"url": image_url},
            }
        ]
    elif case in {CASE_TEXT_IMAGE, CASE_TEXT_IMAGE_TOOLS}:
        if not image_url:
            raise ValueError(f"{case} 缺少图片")
        prompt = (
            IMAGE_TOOL_PROMPT
            if case == CASE_TEXT_IMAGE_TOOLS
            else VISUAL_PROMPT
        )
        user_content = [
            {"type": "text", "text": f"{prompt}\n测试编号：{nonce}"},
            {
                "type": "image_url",
                "image_url": {"url": image_url},
            },
        ]
    elif case == CASE_TEXT_TOOLS:
        user_content = f"{TEXT_TOOL_PROMPT}\n测试编号：{nonce}"
    elif case == CASE_TEXT_ONLY:
        user_content = f"{VISUAL_PROMPT}\n测试编号：{nonce}"
    else:
        raise ValueError(f"未知测试场景: {case}")
    return [system, {"role": "user", "content": user_content}]


def _usage_value(usage: Any, name: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value) if isinstance(value, (int, float)) else None


def _round(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 1)
    return None


def validate_tool_arguments(raw: str) -> bool:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and isinstance(value.get("summary"), str)
        and bool(value["summary"].strip())
        and isinstance(value.get("has_person"), bool)
    )


class MultimodalBenchmarkRunner:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        temperature: float,
        max_tokens: int,
        tool_max_tokens: int,
        vary_image_payload: bool,
        expected_keywords: dict[str, list[str]],
    ) -> None:
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,
        )
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.tool_max_tokens = tool_max_tokens
        self.vary_image_payload = vary_image_payload
        self.expected_keywords = expected_keywords
        self.include_stream_usage = True

    def close(self) -> None:
        self.client.close()

    def run(
        self,
        scenario: Scenario,
        *,
        request_id: int,
        warmup: bool = False,
    ) -> BenchResult:
        nonce = uuid.uuid4().hex[:12]
        prepare_started = time.perf_counter()
        image_url = None
        request_image_bytes = 0
        if scenario.image is not None:
            image_data = scenario.image.data
            if self.vary_image_payload:
                image_data = jpeg_with_nonce(image_data, nonce)
            request_image_bytes = len(image_data)
            image_url = image_data_url(image_data, scenario.image.mime_type)
        messages = build_messages(
            scenario.case,
            image_url=image_url,
            nonce=nonce,
        )
        payload_prepare_ms = (time.perf_counter() - prepare_started) * 1000.0

        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": (
                self.tool_max_tokens
                if scenario.case in TOOL_CASES
                else self.max_tokens
            ),
            "stream": True,
            "extra_body": NO_THINKING_EXTRA_BODY,
        }
        if scenario.case in TOOL_CASES:
            params["tools"] = TOOLS
            params["tool_choice"] = "auto"
        if self.include_stream_usage:
            params["stream_options"] = {"include_usage": True}

        data = self._stream(params)
        if (
            not data["ok"]
            and self.include_stream_usage
            and data["raw_chunks"] == 0
        ):
            self.include_stream_usage = False
            params.pop("stream_options", None)
            data = self._stream(params)

        output_text = data["output_text"]
        tool_call_names = data["tool_call_names"]
        tool_arguments = data["tool_arguments"]
        expected = self.expected_keywords.get(scenario.image_label, [])
        recognition_ok: bool | None = None
        if expected and scenario.case in IMAGE_CASES:
            searchable = f"{output_text}\n{tool_arguments}".lower()
            recognition_ok = any(keyword in searchable for keyword in expected)

        tool_arguments_valid: bool | None = None
        if scenario.case in TOOL_CASES:
            tool_arguments_valid = validate_tool_arguments(tool_arguments)
            expected_tool_called = EXPECTED_TOOL_NAME in tool_call_names
            capability_ok = expected_tool_called and tool_arguments_valid
            if not expected_tool_called:
                status = "expected_tool_not_called"
            elif not tool_arguments_valid:
                status = "invalid_tool_arguments"
            else:
                status = "ok"
        else:
            capability_ok = bool(output_text)
            status = "ok" if capability_ok else "no_text"
        ok = bool(data["ok"] and capability_ok)
        if recognition_ok is False:
            status = "recognition_miss"

        result = BenchResult(
            scenario=scenario.key,
            case=scenario.case,
            image_label=scenario.image_label,
            request_id=request_id,
            ok=ok,
            status=status if data["ok"] else "request_error",
            image_bytes=request_image_bytes,
            image_width=scenario.image.width if scenario.image else None,
            image_height=scenario.image.height if scenario.image else None,
            payload_prepare_ms=_round(payload_prepare_ms),
            first_raw_ms=_round(data["first_raw_ms"]),
            first_text_ms=_round(data["first_text_ms"]),
            first_tool_call_ms=_round(data["first_tool_call_ms"]),
            first_meaningful_ms=_round(data["first_meaningful_ms"]),
            total_ms=_round(data["total_ms"]),
            decode_ms=_round(data["decode_ms"]),
            prompt_tokens=data["prompt_tokens"],
            completion_tokens=data["completion_tokens"],
            total_tokens=data["total_tokens"],
            decode_tokens_per_s=_round(data["decode_tokens_per_s"]),
            output_chars=len(output_text),
            output_text=output_text,
            tool_call_names=",".join(tool_call_names),
            tool_arguments=tool_arguments,
            tool_arguments_valid=tool_arguments_valid,
            recognition_ok=recognition_ok,
            expected_keywords=",".join(expected),
            raw_chunks=data["raw_chunks"],
            text_chunks=data["text_chunks"],
            tool_chunks=data["tool_chunks"],
            reasoning_chunks=data["reasoning_chunks"],
            error=data["error"],
        )
        if not warmup:
            print(
                json.dumps(
                    {
                        "scenario": result.scenario,
                        "request_id": result.request_id,
                        "ok": result.ok,
                        "first_text_ms": result.first_text_ms,
                        "first_tool_call_ms": result.first_tool_call_ms,
                        "decode_tokens_per_s": result.decode_tokens_per_s,
                        "status": result.status,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return result

    def _stream(self, params: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        first_raw_ms = None
        first_text_ms = None
        first_tool_call_ms = None
        raw_chunks = 0
        text_chunks = 0
        tool_chunks = 0
        reasoning_chunks = 0
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}
        usage = None
        error = None

        try:
            stream = self.client.chat.completions.create(**params)
            for chunk in stream:
                raw_chunks += 1
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if first_raw_ms is None:
                    first_raw_ms = elapsed_ms
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                if delta is None:
                    continue
                reasoning = (
                    getattr(delta, "reasoning", None)
                    or getattr(delta, "reasoning_content", None)
                    or ""
                )
                if reasoning:
                    reasoning_chunks += 1
                content = getattr(delta, "content", None) or ""
                if content:
                    if first_text_ms is None:
                        first_text_ms = elapsed_ms
                    text_chunks += 1
                    content_parts.append(content)
                for tool_call in getattr(delta, "tool_calls", None) or []:
                    if first_tool_call_ms is None:
                        first_tool_call_ms = elapsed_ms
                    tool_chunks += 1
                    index = int(getattr(tool_call, "index", 0) or 0)
                    accumulated = tool_calls.setdefault(
                        index,
                        {"name": "", "arguments": ""},
                    )
                    function = getattr(tool_call, "function", None)
                    if function is None:
                        continue
                    name = getattr(function, "name", None)
                    arguments = getattr(function, "arguments", None)
                    if name:
                        accumulated["name"] += str(name)
                    if arguments:
                        accumulated["arguments"] += str(arguments)
        except Exception as exc:
            error = repr(exc)

        total_ms = (time.perf_counter() - started) * 1000.0
        output_text = "".join(content_parts)
        prompt_tokens = _usage_value(usage, "prompt_tokens")
        completion_tokens = _usage_value(usage, "completion_tokens")
        total_tokens = _usage_value(usage, "total_tokens")
        if completion_tokens is None and output_text:
            completion_tokens = max(1, round(len(output_text) / 1.6))
        decode_ms = (
            max(0.0, total_ms - first_text_ms)
            if first_text_ms is not None
            else None
        )
        decode_tokens_per_s = (
            max(0, completion_tokens - 1) / (decode_ms / 1000.0)
            if completion_tokens and decode_ms and decode_ms > 0
            else None
        )
        first_meaningful_candidates = [
            value
            for value in (first_text_ms, first_tool_call_ms)
            if value is not None
        ]
        return {
            "ok": error is None and bool(output_text or tool_calls),
            "error": error,
            "first_raw_ms": first_raw_ms,
            "first_text_ms": first_text_ms,
            "first_tool_call_ms": first_tool_call_ms,
            "first_meaningful_ms": (
                min(first_meaningful_candidates)
                if first_meaningful_candidates
                else None
            ),
            "total_ms": total_ms,
            "decode_ms": decode_ms,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "decode_tokens_per_s": decode_tokens_per_s,
            "output_text": output_text,
            "tool_call_names": [
                item["name"] for _, item in sorted(tool_calls.items())
            ],
            "tool_arguments": "\n".join(
                item["arguments"] for _, item in sorted(tool_calls.items())
            ),
            "raw_chunks": raw_chunks,
            "text_chunks": text_chunks,
            "tool_chunks": tool_chunks,
            "reasoning_chunks": reasoning_chunks,
        }


def build_scenarios(
    images: list[LoadedImage],
    cases: list[str],
) -> list[Scenario]:
    scenarios: list[Scenario] = []
    if CASE_TEXT_ONLY in cases:
        scenarios.append(Scenario(CASE_TEXT_ONLY))
    if CASE_TEXT_TOOLS in cases:
        scenarios.append(Scenario(CASE_TEXT_TOOLS))
    for image in images:
        for case in cases:
            if case in IMAGE_CASES:
                scenarios.append(Scenario(case, image))
    return scenarios


def write_csv(path: Path, results: list[BenchResult]) -> None:
    rows = [asdict(result) for result in results]
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def render_markdown_report(manifest: dict[str, Any]) -> str:
    lines = [
        f"# Multimodal LLM Benchmark - {manifest['label']}",
        "",
        f"- Generated: `{manifest['generated_at']}`",
        f"- Endpoint: `{manifest['base_url']}`",
        f"- Model: `{manifest['model']}`",
        f"- Rounds: `{manifest['rounds']}`, warmups per scenario: `{manifest['warmups']}`",
        f"- Temperature: `{manifest['temperature']}`, max tokens: `{manifest['max_tokens']}`, "
        f"tool max tokens: `{manifest['tool_max_tokens']}`",
        "- Requests are sequential and reuse one HTTP client connection.",
        "",
        "## Images",
        "",
        "| label | file | bytes | resolution | sha256 | expected keywords |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ]
    for image in manifest["images"]:
        lines.append(
            f"| `{image['label']}` | `{image['path']}` | {image['bytes']} | "
            f"{image['width']}x{image['height']} | `{image['sha256'][:12]}` | "
            f"{', '.join(image['expected_keywords']) or '-'} |"
        )

    lines.extend(
        [
            "",
            "## Summary",
            "",
            "| scenario | ok | first raw p50/p95 ms | first text p50/p95 ms | "
            "first tool p50/p95 ms | decode token/s p50 | recognition | tool args |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for scenario, row in manifest["summary"].items():
        metrics = row["metrics"]

        def pair(name: str) -> str:
            return (
                f"{_fmt(metrics[name]['p50'])}/"
                f"{_fmt(metrics[name]['p95'])}"
            )

        recognition = (
            f"{row['recognition_passed']}/{row['recognition_checked']}"
            if row["recognition_checked"]
            else "-"
        )
        tool_arguments = (
            f"{row['tool_arguments_passed']}/{row['tool_arguments_checked']}"
            if row["tool_arguments_checked"]
            else "-"
        )
        lines.append(
            f"| `{scenario}` | {row['ok']}/{row['total']} | "
            f"{pair('first_raw_ms')} | {pair('first_text_ms')} | "
            f"{pair('first_tool_call_ms')} | "
            f"{_fmt(metrics['decode_tokens_per_s']['p50'])} | "
            f"{recognition} | {tool_arguments} |"
        )

    lines.extend(
        [
            "",
            "## Image Overhead",
            "",
            "| scenario | baseline | metric | p50 delta | p95 delta |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    for comparison in manifest["comparisons"]:
        p50 = (
            f"{_fmt(comparison['delta_p50_ms'])} ms "
            f"({_fmt(comparison['delta_p50_pct'])}%)"
        )
        p95 = (
            f"{_fmt(comparison['delta_p95_ms'])} ms "
            f"({_fmt(comparison['delta_p95_pct'])}%)"
        )
        lines.append(
            f"| `{comparison['scenario']}` | `{comparison['baseline']}` | "
            f"`{comparison['metric']}` | {p50} | {p95} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `first_text_ms` is the user-visible first-text latency.",
            "- `decode_tokens_per_s` measures generation speed after the first text chunk.",
            "- Tool scenarios use `first_tool_call_ms`; they do not require a text response.",
            "- Repeated JPEG requests receive a harmless unique JPEG comment by default to reduce image-cache effects.",
            "- Images are embedded only in the current request. The script does not modify Gateway routing, Robot MCP, or conversation history.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark an OpenAI-compatible multimodal LLM endpoint."
    )
    parser.add_argument(
        "--image",
        action="append",
        type=parse_labeled_path,
        required=True,
        help="JPEG input as label=/path/pic.jpeg; repeat for variants.",
    )
    parser.add_argument(
        "--expect",
        action="append",
        type=parse_expected_keywords,
        default=[],
        help="Optional recognition keywords as label=关键词1,关键词2.",
    )
    parser.add_argument(
        "--cases",
        default=",".join(ALL_CASES),
        help=f"Comma-separated cases: {','.join(ALL_CASES)}",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("LLM_BASE_URL", "http://127.0.0.1:15101/v1"),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL", "qwen3-5-9b"),
    )
    parser.add_argument("--api-key-env", default="LLM_API_KEY")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--tool-max-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-image-bytes", type=int, default=262_144)
    parser.add_argument("--seed", type=int, default=7232026)
    parser.add_argument("--label", default="visual-intent-evaluation")
    parser.add_argument(
        "--out-dir",
        default="tmp/multimodal_llm_benchmark",
    )
    parser.add_argument(
        "--keep-image-payload",
        action="store_true",
        help="Do not add a unique JPEG comment per request.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.rounds <= 0:
        raise SystemExit("--rounds 必须大于 0")
    if args.warmups < 0:
        raise SystemExit("--warmups 不能小于 0")
    if args.max_image_bytes <= 0:
        raise SystemExit("--max-image-bytes 必须大于 0")
    if args.tool_max_tokens <= 0:
        raise SystemExit("--tool-max-tokens 必须大于 0")

    cases = [case.strip() for case in args.cases.split(",") if case.strip()]
    unknown_cases = sorted(set(cases) - set(ALL_CASES))
    if unknown_cases:
        raise SystemExit(f"未知测试场景: {','.join(unknown_cases)}")
    if not cases:
        raise SystemExit("--cases 不能为空")

    expected_keywords = dict(args.expect)
    image_pairs = list(args.image)
    labels = [label for label, _ in image_pairs]
    if len(labels) != len(set(labels)):
        raise SystemExit("--image label 不能重复")
    unknown_expected = sorted(set(expected_keywords) - set(labels))
    if unknown_expected:
        raise SystemExit(
            "--expect 引用了不存在的 image label: "
            + ",".join(unknown_expected)
        )
    images = [
        load_image(label, path, max_bytes=args.max_image_bytes)
        for label, path in image_pairs
    ]
    scenarios = build_scenarios(images, cases)
    if not scenarios:
        raise SystemExit("没有可运行的测试场景")

    api_key = os.getenv(args.api_key_env) or "EMPTY"
    runner = MultimodalBenchmarkRunner(
        base_url=args.base_url.rstrip("/"),
        api_key=api_key,
        model=args.model,
        timeout=args.timeout,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        tool_max_tokens=args.tool_max_tokens,
        vary_image_payload=not args.keep_image_payload,
        expected_keywords=expected_keywords,
    )
    results: list[BenchResult] = []
    request_id = 0
    try:
        for scenario in scenarios:
            for warmup_index in range(args.warmups):
                runner.run(
                    scenario,
                    request_id=-(warmup_index + 1),
                    warmup=True,
                )

        rng = random.Random(args.seed)
        for _round_index in range(args.rounds):
            round_scenarios = list(scenarios)
            rng.shuffle(round_scenarios)
            for scenario in round_scenarios:
                request_id += 1
                results.append(
                    runner.run(
                        scenario,
                        request_id=request_id,
                    )
                )
    finally:
        runner.close()

    summary = summarize_results(results)
    comparisons = build_comparisons(summary)
    label = "".join(
        char if char.isalnum() or char in "-_" else "-"
        for char in args.label
    ).strip("-")
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / args.out_dir / f"{timestamp}_{label or 'benchmark'}"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "multimodal-llm-benchmark/v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "label": label or "benchmark",
        "base_url": args.base_url.rstrip("/"),
        "model": args.model,
        "api_key_env": args.api_key_env,
        "api_key_set": bool(os.getenv(args.api_key_env)),
        "rounds": args.rounds,
        "warmups": args.warmups,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "tool_max_tokens": args.tool_max_tokens,
        "timeout": args.timeout,
        "max_image_bytes": args.max_image_bytes,
        "vary_image_payload": not args.keep_image_payload,
        "cases": cases,
        "images": [
            {
                "label": image.label,
                "path": str(image.path),
                "bytes": len(image.data),
                "mime_type": image.mime_type,
                "width": image.width,
                "height": image.height,
                "sha256": image.sha256,
                "expected_keywords": expected_keywords.get(image.label, []),
            }
            for image in images
        ],
        "summary": summary,
        "comparisons": comparisons,
        "results": [asdict(result) for result in results],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(out_dir / "results.csv", results)
    (out_dir / "README.md").write_text(
        render_markdown_report(manifest),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "out_dir": str(out_dir),
                "summary": summary,
                "comparisons": comparisons,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
