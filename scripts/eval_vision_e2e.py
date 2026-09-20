#!/usr/bin/env python3
"""独立 Gold 验收使用的真实图片 Vision 校验核心。"""

from __future__ import annotations

import base64
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from llm.tool_router import parse_llm_router_decision
from llm.llm_grpc_server import _build_tool_router_classifier_messages
from scripts import eval_runner

SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


def load_image_data_url(path: Path) -> str:
    """读取真实 JPEG/PNG/WebP；拒绝文字描述或未知格式冒充图片。"""
    suffix_mime, _ = mimetypes.guess_type(path.name)
    mime = suffix_mime or ""
    if mime not in SUPPORTED_IMAGE_MIME_TYPES:
        raise ValueError("Vision fixture 必须是 JPEG、PNG 或 WebP 图片")
    payload = path.read_bytes()
    if not payload:
        raise ValueError("Vision fixture 图片不能为空")
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def run_vision_e2e(router_client, gen_client, case: dict, image_data_url: str) -> dict:
    text = case.get("prompt") or case.get("text", "")
    if not image_data_url.startswith("data:image/"):
        raise ValueError("Vision 评测必须传入真实图片 data URL")

    # Step 1: 4B 路由
    try:
        messages = _build_tool_router_classifier_messages(text, None)
        started = time.monotonic()
        resp = router_client.chat.completions.create(
            model=eval_runner.ROUTER_MODEL,
            messages=messages,
            max_tokens=4,
            temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        router_elapsed = (time.monotonic() - started) * 1000
        router_raw = (resp.choices[0].message.content or "").strip()
        decision = parse_llm_router_decision(router_raw)
        router_code = router_raw if router_raw else "?"
    except Exception as exc:
        return {
            "case": case,
            "router_ok": False,
            "router_code": "ERROR",
            "router_error": f"{type(exc).__name__}: {exc}",
            "gen_text": "",
            "gen_ok": False,
            "gen_error": str(exc),
            "elapsed_ms": 0,
            "ok": False,
        }

    # 验收只接受明确 Vision 路由；普通 Chat/Robot 不得算作等价成功。
    router_ok = decision == ("chat", "vision")

    # Step 2: 9B 描述画面
    gen_ok = False
    gen_text = ""
    gen_error = ""
    gen_elapsed = 0.0
    try:
        system_prompt = (
            "你是一个友好、专业的 AI 语音助手。当前摄像头画面已附在用户消息里。"
            "请基于画面用 1-2 句自然中文描述你看到了什么。"
            "不要输出 markdown / 表格 / 代码块 / 工具名 / 函数名。"
        )
        started = time.monotonic()
        resp = gen_client.chat.completions.create(
            model=eval_runner.GEN_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                },
            ],
            max_tokens=200,
            temperature=0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        gen_elapsed = (time.monotonic() - started) * 1000
        gen_text = (resp.choices[0].message.content or "").strip()
        required_groups = case.get("required_fact_groups", [])
        missing_groups = [
            group for group in required_groups if not any(term in gen_text for term in group)
        ]
        forbidden_hits = [term for term in case.get("forbidden_terms", []) if term in gen_text]
        gen_ok = (
            bool(gen_text)
            and "[EXIT]" not in gen_text
            and len(gen_text) > 5
            and not missing_groups
            and not forbidden_hits
        )
        if missing_groups:
            gen_error = f"missing_fact_groups={missing_groups}"
        if forbidden_hits:
            gen_error = f"forbidden_terms={forbidden_hits}"
    except Exception as exc:
        gen_error = f"{type(exc).__name__}: {exc}"

    return {
        "case": case,
        "router_ok": router_ok,
        "router_code": router_code,
        "gen_text": gen_text,
        "gen_ok": gen_ok,
        "gen_error": gen_error,
        "elapsed_ms": router_elapsed + gen_elapsed if router_ok else 0,
        "ok": router_ok and gen_ok,
    }
