#!/usr/bin/env python3
"""
机器人 MQTT 控制 API

通过 robot_id 动态拼接 MQTT topic，避免多机器人场景继续绑定 companion_01。
"""

import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: F401  # load project .env before reading ROBOT_MQTT_* env vars

logger = logging.getLogger(__name__)

# MQTT 连接信息必须由环境变量或部署密钥注入，避免代码默认连到生产 broker。
MQTT_HOST = os.getenv("ROBOT_MQTT_HOST", "").strip()
MQTT_PORT = int(os.getenv("ROBOT_MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("ROBOT_MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.getenv("ROBOT_MQTT_PASSWORD", "")
MQTT_CLIENT_ID = os.getenv("ROBOT_MQTT_CLIENT_ID", "")
MQTT_KEEPALIVE = int(os.getenv("ROBOT_MQTT_KEEPALIVE", "60"))
MQTT_QOS = int(os.getenv("ROBOT_MQTT_QOS", "0"))

MQTT_TOPIC_PREFIX = os.getenv("ROBOT_MQTT_TOPIC_PREFIX", "windaka").strip().strip("/") or "windaka"
DEFAULT_ROBOT_ID = (
    os.getenv("ROBOT_MQTT_DEFAULT_ROBOT_ID")
    or os.getenv("ROBOT_ID")
    or "companion_01"
).strip()
MANUAL_CONTROL_TOPIC_SUFFIX = "task/manual_control_cmd"
MANUAL_CONTROL_METHOD = "/task/manual_control_cmd"
DEFAULT_MSG_ID = int(os.getenv("ROBOT_MQTT_MSG_ID", "4"))
CREATE_MAP_TOPIC_SUFFIX = "task/create_map"
CREATE_MAP_METHOD = "/task/create_map"
CREATE_MAP_MSG_ID = 1
PATROL_TOPIC_SUFFIX = "task/patrol"
PATROL_METHOD = "/task/patrol"
PATROL_MSG_ID = 4
FOLLOW_TOPIC_SUFFIX = "task/follow"
FOLLOW_METHOD = "/task/follow"
FOLLOW_MSG_ID = 4
RECHARGE_TOPIC_SUFFIX = "task/recharge"
RECHARGE_METHOD = "/task/recharge"
RECHARGE_MSG_ID = 7
DANCE_TOPIC_SUFFIX = "device/dancing"
DANCE_METHOD = "/device/dancing"
DANCE_MSG_ID = 4
SING_TOPIC_SUFFIX = "device/sing"
SING_METHOD = "/device/sing"
SING_MSG_ID = 7
VIDEO_CALL_TOPIC_SUFFIX = "device/call"
VIDEO_CALL_METHOD = "/device/call"
VIDEO_CALL_MSG_ID = 7
VIDEO_CALL_ACTION = 1
VIDEO_CALL_CODE = 1
VISUAL_DETECTION_TOPIC_SUFFIX = "device/visual_detec"
VISUAL_DETECTION_METHOD = "/device/visual_detec"
VISUAL_DETECTION_MSG_ID = 7
TASK_CONTROL_TOPIC_SUFFIX = "task/control"
TASK_CONTROL_METHOD = "/task/control"
TASK_CONTROL_MSG_ID = 4
CANCEL_TASK_OPERATION_TYPE = 3
ACTION_TO_MOVE_TYPE = {
    "forward": int(os.getenv("ROBOT_MQTT_TYPE_FORWARD", "1")),
    "backward": int(os.getenv("ROBOT_MQTT_TYPE_BACKWARD", "2")),
    "turn_left": int(os.getenv("ROBOT_MQTT_TYPE_TURN_LEFT", "3")),
    "turn_right": int(os.getenv("ROBOT_MQTT_TYPE_TURN_RIGHT", "4")),
    "stop": int(os.getenv("ROBOT_MQTT_TYPE_STOP", "5")),
    "greet": int(os.getenv("ROBOT_MQTT_TYPE_GREET", "6")),
    "handshake": int(os.getenv("ROBOT_MQTT_TYPE_HANDSHAKE", "7")),
    "cheer": int(os.getenv("ROBOT_MQTT_TYPE_CHEER", "8")),
}
VALID_ACTIONS = tuple(ACTION_TO_MOVE_TYPE.keys())
DEFAULT_ACTION = os.getenv("ROBOT_MQTT_DEFAULT_ACTION", "forward").strip().lower() or "forward"
VISUAL_DETECTION_TYPE = {
    "gesture": 1,
    "pet": 2,
    "environment": 3,
}
ACTION_RESULT_TEXT = {
    "forward": "我已经开始向前移动了。",
    "backward": "我已经开始向后移动了。",
    "turn_left": "我已经开始左转了。",
    "turn_right": "我已经开始右转了。",
    "stop": "我已经停下来了。",
    "greet": "好的，我来打个招呼。",
    "handshake": "好的，我来握手。",
    "cheer": "好的，我来喝彩。",
}
TASK_RESULT_TEXT = {
    "create_map": "我已经开始创建地图了。",
    "patrol": "我已经开始巡检了。",
    "follow": "我已经开始追随了。",
    "recharge": "好的，我去充电了。",
    "dance": "好呀，我开始跳舞啦。",
    "sing": "好的，我开始播放音乐。",
    "call_video": "好的，我开始呼叫视频电话。",
    "detect_gesture": "好的，我开始检测手势。",
    "detect_pet": "好的，我开始检测宠物。",
    "understand_environment": "好的，我开始理解周围环境。",
    "cancel_task": "好的，我已尝试取消当前任务。",
}


def _to_int(value: Any, field_name: str) -> int:
    """将输入转换为整数，并在失败时给出明确错误。"""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc


def normalize_robot_id(robot_id: Any | None = None) -> str:
    """标准化 robot_id，并确保它可以安全作为 MQTT topic 的一个段。"""
    text = str(robot_id or DEFAULT_ROBOT_ID or "").strip().strip("/")
    if not text:
        raise ValueError("robot_id 不能为空")
    if any(ch in text for ch in ("/", "#", "+")) or any(ch.isspace() for ch in text):
        raise ValueError("robot_id 不能包含空白字符、/、# 或 +")
    return text


def build_robot_topic(robot_id: Any | None, suffix: str) -> str:
    """按 robot_id 生成机器人 MQTT topic。"""
    topic_suffix = str(suffix or "").strip().strip("/")
    if not topic_suffix:
        raise ValueError("topic suffix 不能为空")
    return f"{MQTT_TOPIC_PREFIX}/{normalize_robot_id(robot_id)}/mcp/{topic_suffix}"


def _normalize_action(action: Any) -> str:
    """标准化并校验 action。"""
    if action is None:
        return DEFAULT_ACTION
    action_text = str(action).strip().lower()
    if not action_text:
        return DEFAULT_ACTION
    if action_text not in ACTION_TO_MOVE_TYPE:
        raise ValueError(f"action 必须是 {list(VALID_ACTIONS)} 之一")
    return action_text


def build_manual_control_payload(
    action: str = DEFAULT_ACTION,
    msg_id: int = DEFAULT_MSG_ID,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造机器人手动控制消息。"""
    action_value = _normalize_action(action)
    move_type = ACTION_TO_MOVE_TYPE[action_value]
    final_timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "msg_id": _to_int(msg_id, "msg_id"),
        "msg_type": "request",
        "method": MANUAL_CONTROL_METHOD,
        "timestamp": _to_int(final_timestamp, "timestamp"),
        "data": {
            "type": _to_int(move_type, "type"),
        },
    }


def build_simple_task_payload(
    method: str,
    msg_id: int,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造简单任务消息，data 固定为空对象。"""
    final_timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "msg_id": _to_int(msg_id, "msg_id"),
        "msg_type": "request",
        "method": method,
        "timestamp": _to_int(final_timestamp, "timestamp"),
        "data": {},
    }


def build_visual_detection_payload(
    visual_type: str,
    msg_id: int = VISUAL_DETECTION_MSG_ID,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造视觉识别消息，type: 1=手势检测, 2=宠物检测, 3=环境理解。"""
    normalized_type = _normalize_visual_type(visual_type)
    final_timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "msg_id": _to_int(msg_id, "msg_id"),
        "msg_type": "request",
        "method": VISUAL_DETECTION_METHOD,
        "timestamp": _to_int(final_timestamp, "timestamp"),
        "data": {
            "type": VISUAL_DETECTION_TYPE[normalized_type],
        },
    }


def build_video_call_payload(timestamp_ms: int | None = None) -> dict[str, Any]:
    """构造固定 action/code 的视频呼叫消息。"""
    final_timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "msg_id": VIDEO_CALL_MSG_ID,
        "msg_type": "request",
        "method": VIDEO_CALL_METHOD,
        "timestamp": _to_int(final_timestamp, "timestamp"),
        "data": {
            "action": VIDEO_CALL_ACTION,
            "code": VIDEO_CALL_CODE,
        },
    }


def build_task_control_payload(
    *,
    task_uuid: str = "",
    operation_type: int = CANCEL_TASK_OPERATION_TYPE,
    msg_id: int = TASK_CONTROL_MSG_ID,
    timestamp_ms: int | None = None,
) -> dict[str, Any]:
    """构造任务控制消息。"""
    final_timestamp = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "msg_id": _to_int(msg_id, "msg_id"),
        "msg_type": "request",
        "method": TASK_CONTROL_METHOD,
        "timestamp": _to_int(final_timestamp, "timestamp"),
        "data": {
            "task_uuid": str(task_uuid or ""),
            "operation_type": _to_int(operation_type, "operation_type"),
        },
    }


def _normalize_visual_type(visual_type: Any) -> str:
    """标准化视觉识别类型。"""
    text = str(visual_type or "").strip().lower()
    aliases = {
        "1": "gesture",
        "gesture": "gesture",
        "hand": "gesture",
        "hand_gesture": "gesture",
        "手势": "gesture",
        "手势检测": "gesture",
        "2": "pet",
        "pet": "pet",
        "cat": "pet",
        "dog": "pet",
        "宠物": "pet",
        "宠物检测": "pet",
        "3": "environment",
        "environment": "environment",
        "env": "environment",
        "scene": "environment",
        "环境": "environment",
        "环境理解": "environment",
    }
    normalized = aliases.get(text)
    if not normalized:
        raise ValueError("visual_type 必须是 gesture、pet 或 environment")
    return normalized


def _publish_payload(
    topic: str,
    payload: dict[str, Any],
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发布指定 topic/payload 的 MQTT 消息。

    meta: 可选的追踪/审计元数据（如 session_id/trace_id），会以 ``meta`` 字段
    一并写入 MQTT payload，便于跨进程/跨服务排障时与 Gateway 的 trace 串联。
    """
    if not MQTT_HOST:
        raise RuntimeError("ROBOT_MQTT_HOST 未配置，机器人 MQTT 发布已禁用")

    try:
        from paho.mqtt import publish
    except ImportError as exc:
        raise RuntimeError("未安装 paho-mqtt，请先执行 pip install -r requirements.txt") from exc

    if meta:
        # 防御性 copy，避免污染上游传入的 dict
        payload = {**payload, "meta": dict(meta)}
    payload_text = json.dumps(payload, ensure_ascii=False)

    auth = None
    if MQTT_USERNAME:
        auth = {
            "username": MQTT_USERNAME,
            "password": MQTT_PASSWORD,
        }

    client_id = MQTT_CLIENT_ID or f"robot-mcp-{uuid.uuid4().hex[:8]}"

    try:
        publish.single(
            topic=topic,
            payload=payload_text,
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            client_id=client_id,
            keepalive=MQTT_KEEPALIVE,
            qos=MQTT_QOS,
            auth=auth,
        )
    except Exception as exc:
        logger.error("发布机器人 MQTT 消息失败: %r", exc)
        raise RuntimeError(
            f"发布机器人 MQTT 消息失败: broker={MQTT_HOST}:{MQTT_PORT}, topic={topic}, error={exc}"
        ) from exc

    logger.info(
        "已发送机器人控制消息: broker=%s:%s topic=%s payload=%s",
        MQTT_HOST,
        MQTT_PORT,
        topic,
        payload_text,
    )
    return {
        "broker": f"{MQTT_HOST}:{MQTT_PORT}",
        "topic": topic,
        "payload": payload,
    }


def publish_manual_control_cmd(
    action: str = DEFAULT_ACTION,
    msg_id: int = DEFAULT_MSG_ID,
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    发布机器人手动控制命令。

    返回值用于 MCP 工具结果展示。
    """
    payload = build_manual_control_payload(
        action=action,
        msg_id=msg_id,
        timestamp_ms=timestamp_ms,
    )
    action_value = _normalize_action(action)
    result = _publish_payload(
        build_robot_topic(robot_id, MANUAL_CONTROL_TOPIC_SUFFIX),
        payload,
        meta=meta,
    )
    result["robot_id"] = normalize_robot_id(robot_id)
    result["action"] = action_value
    return result


def publish_simple_task_cmd(
    *,
    topic_suffix: str,
    method: str,
    msg_id: int,
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发布 data 为空对象的简单任务命令。"""
    payload = build_simple_task_payload(
        method=method,
        msg_id=msg_id,
        timestamp_ms=timestamp_ms,
    )
    result = _publish_payload(build_robot_topic(robot_id, topic_suffix), payload, meta=meta)
    result["robot_id"] = normalize_robot_id(robot_id)
    return result


def publish_task_control_cmd(
    *,
    task_uuid: str = "",
    operation_type: int = CANCEL_TASK_OPERATION_TYPE,
    msg_id: int = TASK_CONTROL_MSG_ID,
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发布任务控制命令。"""
    payload = build_task_control_payload(
        task_uuid=task_uuid,
        operation_type=operation_type,
        msg_id=msg_id,
        timestamp_ms=timestamp_ms,
    )
    result = _publish_payload(build_robot_topic(robot_id, TASK_CONTROL_TOPIC_SUFFIX), payload, meta=meta)
    result["robot_id"] = normalize_robot_id(robot_id)
    return result


def publish_visual_detection_cmd(
    visual_type: str,
    msg_id: int = VISUAL_DETECTION_MSG_ID,
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发布视觉识别命令。"""
    normalized_type = _normalize_visual_type(visual_type)
    payload = build_visual_detection_payload(
        visual_type=normalized_type,
        msg_id=msg_id,
        timestamp_ms=timestamp_ms,
    )
    result = _publish_payload(build_robot_topic(robot_id, VISUAL_DETECTION_TOPIC_SUFFIX), payload, meta=meta)
    result["robot_id"] = normalize_robot_id(robot_id)
    result["visual_type"] = normalized_type
    result["type"] = VISUAL_DETECTION_TYPE[normalized_type]
    return result


def publish_video_call_cmd(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发布固定参数的视频呼叫命令。"""
    payload = build_video_call_payload(timestamp_ms=timestamp_ms)
    result = _publish_payload(build_robot_topic(robot_id, VIDEO_CALL_TOPIC_SUFFIX), payload, meta=meta)
    result["robot_id"] = normalize_robot_id(robot_id)
    return result


def move_robot_text(
    action: str = DEFAULT_ACTION,
    msg_id: int = DEFAULT_MSG_ID,
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送机器人移动指令，并返回便于 LLM 理解的文本。"""
    action_value = _normalize_action(action)
    result = publish_manual_control_cmd(
        action=action_value,
        msg_id=msg_id,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人动作结果摘要: robot_id=%s action=%s", result["robot_id"], result["action"])
    return ACTION_RESULT_TEXT[result["action"]]


def create_map_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送创建地图指令。"""
    result = publish_simple_task_cmd(
        topic_suffix=CREATE_MAP_TOPIC_SUFFIX,
        method=CREATE_MAP_METHOD,
        msg_id=CREATE_MAP_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人任务结果摘要: robot_id=%s task=create_map", result["robot_id"])
    return TASK_RESULT_TEXT["create_map"]


def patrol_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送巡检指令。"""
    result = publish_simple_task_cmd(
        topic_suffix=PATROL_TOPIC_SUFFIX,
        method=PATROL_METHOD,
        msg_id=PATROL_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人任务结果摘要: robot_id=%s task=patrol", result["robot_id"])
    return TASK_RESULT_TEXT["patrol"]


def follow_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送追随指令。"""
    result = publish_simple_task_cmd(
        topic_suffix=FOLLOW_TOPIC_SUFFIX,
        method=FOLLOW_METHOD,
        msg_id=FOLLOW_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人任务结果摘要: robot_id=%s task=follow", result["robot_id"])
    return TASK_RESULT_TEXT["follow"]


def recharge_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送回桩充电指令。"""
    result = publish_simple_task_cmd(
        topic_suffix=RECHARGE_TOPIC_SUFFIX,
        method=RECHARGE_METHOD,
        msg_id=RECHARGE_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人任务结果摘要: robot_id=%s task=recharge", result["robot_id"])
    return TASK_RESULT_TEXT["recharge"]


def dance_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送跳舞指令。"""
    result = publish_simple_task_cmd(
        topic_suffix=DANCE_TOPIC_SUFFIX,
        method=DANCE_METHOD,
        msg_id=DANCE_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人任务结果摘要: robot_id=%s task=dance", result["robot_id"])
    return TASK_RESULT_TEXT["dance"]


def sing_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送唱歌/播放音乐指令。"""
    result = publish_simple_task_cmd(
        topic_suffix=SING_TOPIC_SUFFIX,
        method=SING_METHOD,
        msg_id=SING_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人任务结果摘要: robot_id=%s task=sing", result["robot_id"])
    return TASK_RESULT_TEXT["sing"]


def call_video_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送视频呼叫指令。"""
    result = publish_video_call_cmd(
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人任务结果摘要: robot_id=%s task=call_video", result["robot_id"])
    return TASK_RESULT_TEXT["call_video"]


def detect_gesture_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送手势检测指令。"""
    result = publish_visual_detection_cmd(
        visual_type="gesture",
        msg_id=VISUAL_DETECTION_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人视觉结果摘要: robot_id=%s visual_type=gesture", result["robot_id"])
    return TASK_RESULT_TEXT["detect_gesture"]


def detect_pet_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送宠物检测指令。"""
    result = publish_visual_detection_cmd(
        visual_type="pet",
        msg_id=VISUAL_DETECTION_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人视觉结果摘要: robot_id=%s visual_type=pet", result["robot_id"])
    return TASK_RESULT_TEXT["detect_pet"]


def understand_environment_text(
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送环境理解指令。"""
    result = publish_visual_detection_cmd(
        visual_type="environment",
        msg_id=VISUAL_DETECTION_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info("机器人视觉结果摘要: robot_id=%s visual_type=environment", result["robot_id"])
    return TASK_RESULT_TEXT["understand_environment"]


def cancel_task_text(
    task_uuid: str = "",
    timestamp_ms: int | None = None,
    robot_id: Any | None = None,
    *,
    meta: dict[str, Any] | None = None,
) -> str:
    """发送取消当前任务指令。"""
    result = publish_task_control_cmd(
        task_uuid=task_uuid,
        operation_type=CANCEL_TASK_OPERATION_TYPE,
        msg_id=TASK_CONTROL_MSG_ID,
        timestamp_ms=timestamp_ms,
        robot_id=robot_id,
        meta=meta,
    )
    logger.info(
        "机器人任务结果摘要: robot_id=%s task=cancel_task task_uuid=%s",
        result["robot_id"],
        task_uuid or "",
    )
    return TASK_RESULT_TEXT["cancel_task"]
