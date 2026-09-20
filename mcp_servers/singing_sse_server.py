#!/usr/bin/env python3
"""独立 AI 唱歌 MCP Server（SSE）。"""

from flask import Flask, Response, request
import json
import logging
import os
import queue
import sys
import threading
from typing import Any, Dict
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from singing.service import SingingService
from voice_logging import configure_logging


configure_logging("mcp-singing", force=True)
logger = logging.getLogger(__name__)

app = Flask(__name__)
sessions: Dict[str, queue.Queue] = {}
sessions_lock = threading.Lock()
SERVICE = SingingService()

TOOLS_LIST = [
    {
        "name": "play_song",
        "description": (
            "使用当前角色已经准备好的歌声音色播放约 30 秒歌曲片段。"
            "query 应尽量保留用户原始点歌内容，以便处理 ASR 错字和歌名别名；"
            "用户明确要求随便唱一首时 query 可以为空。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "用户原始点歌文本或歌曲名称；随便唱一首时可为空。",
                }
            },
            "required": [],
        },
    },
    {
        "name": "list_songs",
        "description": (
            "查询当前角色实际会唱的歌曲。适用于“你会唱什么歌”“会唱王心凌的吗”"
            "“会唱爱你吗”等能力查询，不会触发歌曲播放。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "可选。按歌名、歌手或可能存在 ASR 错字的名称过滤。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "本次最多返回多少首；语音场景建议不超过 5。",
                },
                "cursor": {
                    "type": "string",
                    "description": "可选。上一页返回的 next_cursor。",
                },
            },
            "required": [],
        },
    },
]


def _call_tool(name: str, arguments: dict[str, Any] | None) -> str:
    args = dict(arguments or {})
    meta = args.pop("_meta", None)
    voice_id = SERVICE.resolve_voice_id(meta if isinstance(meta, dict) else None)
    if not voice_id:
        result = SERVICE.unmapped_voice_result(list_request=name == "list_songs")
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if name == "play_song":
        result = SERVICE.play_song(str(args.get("query") or ""), voice_id=voice_id)
    elif name == "list_songs":
        result = SERVICE.list_songs(
            str(args.get("query") or ""),
            voice_id=voice_id,
            limit=int(args.get("limit") or 5),
            cursor=str(args.get("cursor") or ""),
        )
    else:
        raise ValueError(f"Unknown tool: {name}")
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def create_sse_event(event_type: str, data: str) -> str:
    return f"event: {event_type}\ndata: {data}\n\n"


def handle_jsonrpc_request(data: dict[str, Any]) -> dict[str, Any] | None:
    method = data.get("method")
    request_id = data.get("id")
    params = data.get("params", {})
    if request_id is None:
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "singing-sse-server", "version": "0.1.0"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOLS_LIST},
        }
    if method == "tools/call":
        try:
            result_text = _call_tool(
                str(params.get("name") or ""),
                params.get("arguments") if isinstance(params.get("arguments"), dict) else {},
            )
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": result_text}]},
            }
        except ValueError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(exc)},
            }
        except Exception as exc:
            logger.exception("唱歌工具调用失败: method=%s", params.get("name"))
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"Tool execution failed: {exc}"},
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


@app.route("/mcp", methods=["GET"])
def mcp_sse_endpoint():
    session_id = str(uuid.uuid4())
    msg_queue: queue.Queue = queue.Queue()
    with sessions_lock:
        sessions[session_id] = msg_queue

    def generate():
        try:
            yield create_sse_event("endpoint", f"/messages/{session_id}")
            while True:
                try:
                    message = msg_queue.get(timeout=30)
                    if message is None:
                        break
                    yield create_sse_event("message", json.dumps(message, ensure_ascii=False))
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with sessions_lock:
                sessions.pop(session_id, None)

    return Response(generate(), mimetype="text/event-stream")


@app.route("/messages/<session_id>", methods=["POST"])
def messages_endpoint(session_id: str):
    try:
        response = handle_jsonrpc_request(request.get_json() or {})
        if response is not None:
            with sessions_lock:
                target = sessions.get(session_id)
                if target is None:
                    return json.dumps({"error": "Session not found"}), 404
                target.put(response)
        return "", 202
    except Exception as exc:
        logger.exception("处理唱歌 MCP 消息失败")
        return json.dumps({"error": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health_check():
    return json.dumps({"status": "ok", "service": "singing-sse-server"}), 200


if __name__ == "__main__":
    port = int(os.getenv("SINGING_MCP_PORT", "5005"))
    logger.info("启动唱歌 MCP SSE 服务器: port=%s", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
