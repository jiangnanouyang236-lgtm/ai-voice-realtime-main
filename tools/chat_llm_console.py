#!/usr/bin/env python3
"""
Interactive console client for the LLM gRPC service.

Start the LLM service first:
    python llm/llm_grpc_server.py

Then run this script:
    python tools/chat_llm_console.py --bot-id xiaowen
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

import grpc

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from llm import llm_service_pb2, llm_service_pb2_grpc  # noqa: E402


EXIT_COMMANDS = {"q", "quit", "exit", ":q", "退出脚本"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Console client for manually testing LLM StreamChat.",
    )
    parser.add_argument(
        "--target",
        default="127.0.0.1:50053",
        help="LLM gRPC target, default: 127.0.0.1:50053",
    )
    parser.add_argument(
        "--bot-id",
        default="xiaowen",
        help="Bot ID to send with each request, default: xiaowen",
    )
    parser.add_argument(
        "--session-id",
        default=f"console-{uuid.uuid4().hex[:8]}",
        help="Session ID. Reuse the same value to keep server-side context.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per request timeout in seconds, default: 120",
    )
    return parser.parse_args()


def stream_chat(
    stub: llm_service_pb2_grpc.LLMServiceStub,
    *,
    text: str,
    session_id: str,
    bot_id: str,
    timeout: float,
) -> None:
    request = llm_service_pb2.ChatRequest(
        text=text,
        session_id=session_id,
        bot_id=bot_id,
        trace_id=f"{session_id}:{uuid.uuid4().hex[:12]}",
    )

    print("小文> ", end="", flush=True)
    received_text = False
    try:
        for response in stub.StreamChat(request, timeout=timeout):
            if response.text:
                received_text = True
                print(response.text, end="", flush=True)
            if response.is_final:
                break
        if not received_text:
            print("(无文本响应)", end="", flush=True)
        print()
    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() else "UNKNOWN"
        detail = exc.details() or str(exc)
        print(f"\n[grpc error] {code}: {detail}")


def main() -> int:
    args = parse_args()
    print("LLM console test client")
    print(f"target={args.target}, bot_id={args.bot_id}, session_id={args.session_id}")
    print("输入 q / quit / exit / :q 退出脚本。要测试小文退出逻辑，请输入中文“退出吧”。")
    print()

    with grpc.insecure_channel(args.target) as channel:
        stub = llm_service_pb2_grpc.LLMServiceStub(channel)
        while True:
            try:
                text = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if not text:
                continue
            if text.lower() in EXIT_COMMANDS:
                return 0

            stream_chat(
                stub,
                text=text,
                session_id=args.session_id,
                bot_id=args.bot_id,
                timeout=args.timeout,
            )


if __name__ == "__main__":
    raise SystemExit(main())
