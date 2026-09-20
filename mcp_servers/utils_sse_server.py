#!/usr/bin/env python3
"""
工具集 MCP Server (SSE 版本)

提供时间、日期、星期、农历、节假日等工具，支持远程连接
"""

from flask import Flask, request, Response
import json
import logging
import uuid
import queue
import threading
import os
import sys
from typing import Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from . import utils_api
except ImportError:
    import utils_api

from voice_logging import configure_logging

configure_logging("mcp-utils", force=True)
logger = logging.getLogger(__name__)

app = Flask(__name__)

sessions: Dict[str, queue.Queue] = {}
sessions_lock = threading.Lock()

TZ_PROP = {
    "timezone": {
        "type": "string",
        "description": "可选。时区，如 Asia/Shanghai，不传则默认东八区",
    }
}

TOOLS_LIST = [
    {
        "name": "get_now_context",
        "description": "一次性获取当前完整时间上下文：日期、时间、星期、农历、节假日信息",
        "inputSchema": {"type": "object", "properties": TZ_PROP, "required": []},
    },
    {
        "name": "format_timestamp",
        "description": "将 Unix 时间戳转换为可读的日期时间",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ts": {"type": "number", "description": "Unix 时间戳（秒）"},
                **TZ_PROP,
            },
            "required": ["ts"],
        },
    },
]


def _call_tool(name: str, arguments: dict) -> str:
    tz = (arguments or {}).get("timezone") or None
    if name == "get_now_context":
        return utils_api.get_now_context(tz)
    if name == "format_timestamp":
        ts = (arguments or {}).get("ts")
        if ts is None:
            return "缺少参数 ts（时间戳）"
        return utils_api.format_timestamp(float(ts), tz)
    raise ValueError(f"Unknown tool: {name}")


def create_sse_event(event_type: str, data: str) -> str:
    return f"event: {event_type}\ndata: {data}\n\n"


def handle_jsonrpc_request(data: dict) -> dict | None:
    method = data.get("method")
    request_id = data.get("id")
    params = data.get("params", {})

    logger.info(f"处理请求: method={method}, id={request_id}")

    if request_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "utils-sse-server", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": TOOLS_LIST},
        }

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            result_text = _call_tool(tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": result_text}]},
            }
        except ValueError as e:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": str(e)},
            }
        except Exception as e:
            logger.exception("工具调用失败: name=%s, arguments=%s", tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"Tool execution failed: {e}"},
            }

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


@app.route("/mcp", methods=["GET"])
def mcp_sse_endpoint():
    session_id = str(uuid.uuid4())
    msg_queue = queue.Queue()
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
        data = request.get_json()
        response = handle_jsonrpc_request(data)
        if response is not None:
            with sessions_lock:
                q = sessions.get(session_id)
                if q:
                    q.put(response)
                else:
                    return json.dumps({"error": "Session not found"}), 404
        return "", 202
    except Exception as e:
        logger.exception("处理消息失败")
        return json.dumps({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health_check():
    return json.dumps({"status": "ok", "service": "utils-sse-server"}), 200


if __name__ == "__main__":
    logger.info("启动工具集 MCP SSE 服务器...")
    logger.info("服务地址: http://127.0.0.1:5004")
    logger.info("MCP SSE 端点: http://127.0.0.1:5004/mcp (GET)")
    app.run(host="0.0.0.0", port=5004, debug=False, threaded=True)
