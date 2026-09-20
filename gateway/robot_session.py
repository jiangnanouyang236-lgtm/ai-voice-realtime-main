from __future__ import annotations

import asyncio
from typing import Any


async def register_robot_session(
    *,
    session_manager,
    runtime_state,
    session_id: str,
    payload: dict[str, Any],
    robot_secret_required: bool,
) -> dict[str, Any]:
    """注册当前 WebSocket 对应的 Robot，并绑定默认/已配置 Bot。"""
    robot_id = str(payload.get("robot_id", "")).strip()
    client_type = str(payload.get("client_type", "")).strip() or None
    robot_secret = str(payload.get("robot_secret", "")).strip()

    if not robot_id:
        raise ValueError("缺少 robot_id")
    if not runtime_state:
        raise RuntimeError("Gateway 未配置 CONFIG_DATABASE_URL，无法启用 robot 注册")

    row = await asyncio.to_thread(
        runtime_state.register_robot,
        robot_id,
        client_type,
        allow_create=not robot_secret_required,
    )
    if not row["enabled"]:
        raise ValueError(f"该机器人已被禁用: {robot_id}")
    if robot_secret_required:
        if not row.get("robot_secret_hash"):
            raise ValueError(f"Robot {robot_id} 尚未配置 secret，请先在后台重置生成")
        verified = await asyncio.to_thread(runtime_state.verify_robot_secret, robot_id, robot_secret)
        if not verified:
            raise ValueError("robot_secret 校验失败")
    elif row.get("robot_secret_hash") and robot_secret:
        verified = await asyncio.to_thread(runtime_state.verify_robot_secret, robot_id, robot_secret)
        if not verified:
            raise ValueError("robot_secret 校验失败")

    ok = session_manager.register_robot(
        session_id,
        robot_id=row["robot_id"],
        bot_id=row["assigned_bot_id"],
        bot_name=row["bot_name"],
        client_type=row.get("client_type"),
        is_new_robot=bool(row["is_new"]),
    )
    if not ok:
        raise RuntimeError("会话不存在，无法完成机器人注册")

    return {
        "robot_id": row["robot_id"],
        "bot_id": row["assigned_bot_id"],
        "bot_name": row["bot_name"],
        "client_type": row.get("client_type"),
        "is_new_robot": bool(row["created"]),
    }


def build_registered_message_payload(session_id: str, registration: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "robot_id": registration["robot_id"],
        "bot_id": registration["bot_id"],
        "bot_name": registration["bot_name"],
        "client_type": registration["client_type"],
        "is_new_robot": registration["is_new_robot"],
    }


async def resolve_request_bot(
    *,
    session_manager,
    runtime_state,
    session_id: str,
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    """
    解析当前请求应该使用的 Bot。

    优先级：
    1. 已 register 的新协议会话：从 Gateway 已加载的 runtime snapshot 中读取 robot 绑定
    2. 旧协议客户端：继续兼容 audio 消息中的 bot_id，但只接受已 Apply 到当前 runtime 的 Bot
    """
    if session_manager.is_registered(session_id):
        robot_id = session_manager.get_robot_id(session_id)
        if not robot_id:
            raise ValueError("当前会话缺少 robot_id")
        if not runtime_state:
            raise RuntimeError("Gateway 未配置 CONFIG_DATABASE_URL，无法解析 robot 绑定")

        row = await asyncio.to_thread(runtime_state.resolve_robot_bot_binding, robot_id)
        if not row:
            raise ValueError(f"Robot {robot_id} 尚未应用到当前运行时配置，请先 Apply")
        if not row.get("robot_enabled", True):
            raise ValueError(f"该机器人已被禁用: {robot_id}")

        bot_id = row.get("assigned_bot_id")
        bot_name = row.get("bot_name")
        if not bot_id:
            raise ValueError(f"robot {robot_id} 当前没有绑定 Bot")

        session_manager.update_registered_bot(session_id, bot_id=bot_id, bot_name=bot_name)
        return bot_id, bot_name

    legacy_bot_id = str(payload.get("bot_id", "")).strip() or None
    if legacy_bot_id and runtime_state:
        return await asyncio.to_thread(runtime_state.resolve_legacy_bot, legacy_bot_id)
    return legacy_bot_id, None
