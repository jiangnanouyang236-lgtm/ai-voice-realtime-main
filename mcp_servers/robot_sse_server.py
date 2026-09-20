#!/usr/bin/env python3
"""
机器人 MCP Server (SSE 版本)

通过 MQTT 发送机器人手动控制消息，支持远程 SSE 连接。
"""

from flask import Flask, request, Response
import json
import logging
import queue
import threading
import uuid
import os
import sys
from typing import Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from . import robot_mqtt_api
except ImportError:
    import robot_mqtt_api

from voice_logging import configure_logging

configure_logging("mcp-robot", force=True)
logger = logging.getLogger(__name__)

app = Flask(__name__)

sessions: Dict[str, queue.Queue] = {}
sessions_lock = threading.Lock()

TOOLS_LIST = [
    {
        "name": "move_robot",
        "description": (
            "控制真实机器人执行移动动作。"
            "支持前进、后退、左转、右转、停止、打招呼、握手、喝彩。"
            "该操作会影响物理设备位置，可能存在碰撞风险，仅在用户明确要求移动机器人时调用。"
            "在机器人控制语境下，用户说“你”默认指机器人本体（例如“你往前走两步”）。"
            "用户说“和我打个招呼吧”“打个招呼”“挥挥手”时 action=greet；"
            "用户说“和我握个手”“来握个手吧”“握手”时 action=handshake；"
            "用户说“喝个彩”“庆祝一下”“欢呼一下”时 action=cheer。"
            "如果未提供 action，默认执行 forward。"
            "底层 MQTT 所需字段由系统自动补齐。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "forward",
                        "backward",
                        "turn_left",
                        "turn_right",
                        "stop",
                        "greet",
                        "handshake",
                        "cheer",
                    ],
                    "description": "可选。机器人动作类型。不传时默认 forward。",
                }
            },
            "required": [],
        },
    },
    {
        "name": "create_map",
        "description": (
            "启动创建地图任务。适用于“创建地图”“开始建图”“去建个图”"
            "“地图建一下”“生成地图”等表达。会按 robot_id 动态 MQTT topic 发送建图任务请求。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "patrol",
        "description": (
            "启动巡检任务。适用于“巡检”“巡逻”“巡视”“看看周围”"
            "“检查一下环境”“四处看看”等表达。会按 robot_id 动态 MQTT topic 发送巡检任务请求。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "follow",
        "description": "启动追随任务。会按 robot_id 动态 MQTT topic 发送追随任务请求。",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "recharge_robot",
        "description": (
            "让机器人回桩充电。适用于“去充电”“回去充电”“回充”“回充电桩”"
            "“回桩充电”等表达。会按 robot_id 动态 MQTT topic 发送回桩充电任务请求。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "return_to_charge",
        "description": (
            "让机器人返回充电桩并开始充电，等同于 recharge_robot。"
            "适用于“回充电桩”“返回充电桩”“回桩充电”“去充电”等表达。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "dance",
        "description": (
            "让机器人跳舞。适用于“跳舞”“跳个舞”“给我跳支舞”“来段舞蹈”“就是现在”"
            "等表达。会按 robot_id 动态 MQTT topic 发送跳舞设备请求，payload data 为空对象。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "call_video",
        "description": (
            "让当前机器人呼叫视频电话。适用于用户明确要求“我要拨打视频电话”"
            "“呼叫视频电话”“帮我打个视频电话”“发起视频通话”“开始视频通话”等表达。"
            "能力询问、使用说明、否定或取消表达不应调用。"
            "会按当前会话 robot_id 动态 MQTT topic 发送视频呼叫请求，"
            "底层 action 和 code 由系统固定补齐。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "detect_gesture",
        "description": (
            "启动视觉手势检测。适用于“看一下我的手势”“识别手势”“检测手势”"
            "“我比个手势你看看”等表达。会按 robot_id 动态 MQTT topic 发送视觉识别请求，"
            "payload data.type=1。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "detect_pet",
        "description": (
            "启动视觉宠物检测。适用于“看看有没有宠物”“检测宠物”“识别猫狗”"
            "“看看旁边有没有小猫小狗”等表达。会按 robot_id 动态 MQTT topic 发送视觉识别请求，"
            "payload data.type=2。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "understand_environment",
        "description": (
            "启动视觉环境理解/环境检测。适用于“看一下周围环境”“检测环境”“理解一下环境”“看看周围有什么”"
            "“识别一下当前场景”等表达。会按 robot_id 动态 MQTT topic 发送视觉识别请求，"
            "payload data.type=3。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cancel_robot_task",
        "description": (
            "取消机器人当前任务。适用于“取消当前任务”“停止当前任务”“别执行了”"
            "“取消巡检”“停止跟随”“终止任务”等表达。"
            "task_uuid 可选，不传时默认取消当前任务。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_uuid": {
                    "type": "string",
                    "description": "可选。指定要取消的任务 UUID，不传时为空字符串，表示当前任务。",
                }
            },
            "required": [],
        },
    }
]


ROBOT_ID_SCHEMA = {
    "type": "string",
    "description": (
        "可选。目标机器人 robot_id，不传时使用 MCP 服务环境变量 "
        "ROBOT_MQTT_DEFAULT_ROBOT_ID 或 ROBOT_ID。"
    ),
}


for tool in TOOLS_LIST:
    tool["inputSchema"].setdefault("properties", {}).setdefault("robot_id", ROBOT_ID_SCHEMA)


def _call_tool(name: str, arguments: dict) -> str:
    args = arguments or {}
    # Extract and pop _meta so it doesn't leak into tool schema or robot_id override logic.
    # LLM gRPC server injects this to propagate session_id/trace_id into MQTT payload.
    meta = args.pop("_meta", None)
    robot_id = args.get("robot_id")
    if name == "move_robot":
        return robot_mqtt_api.move_robot_text(
            action=args.get("action", robot_mqtt_api.DEFAULT_ACTION),
            robot_id=robot_id,
            meta=meta,
        )
    if name == "create_map":
        return robot_mqtt_api.create_map_text(robot_id=robot_id, meta=meta)
    if name == "patrol":
        return robot_mqtt_api.patrol_text(robot_id=robot_id, meta=meta)
    if name == "follow":
        return robot_mqtt_api.follow_text(robot_id=robot_id, meta=meta)
    if name in ("recharge_robot", "return_to_charge"):
        return robot_mqtt_api.recharge_text(robot_id=robot_id, meta=meta)
    if name == "dance":
        return robot_mqtt_api.dance_text(robot_id=robot_id, meta=meta)
    if name == "call_video":
        return robot_mqtt_api.call_video_text(robot_id=robot_id, meta=meta)
    if name == "detect_gesture":
        return robot_mqtt_api.detect_gesture_text(robot_id=robot_id, meta=meta)
    if name == "detect_pet":
        return robot_mqtt_api.detect_pet_text(robot_id=robot_id, meta=meta)
    if name == "understand_environment":
        return robot_mqtt_api.understand_environment_text(robot_id=robot_id, meta=meta)
    if name == "cancel_robot_task":
        return robot_mqtt_api.cancel_task_text(
            task_uuid=args.get("task_uuid", ""),
            robot_id=robot_id,
            meta=meta,
        )
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
                "serverInfo": {"name": "robot-sse-server", "version": "0.1.0"},
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
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(e)},
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
        logger.error(f"处理消息失败: {e}")
        return json.dumps({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health_check():
    return json.dumps({"status": "ok", "service": "robot-sse-server"}), 200


if __name__ == "__main__":
    logger.info("启动机器人 MCP SSE 服务器...")
    logger.info("服务地址: http://127.0.0.1:5003")
    logger.info("MCP SSE 端点: http://127.0.0.1:5003/mcp (GET)")
    app.run(host="0.0.0.0", port=5003, debug=False, threaded=True)
