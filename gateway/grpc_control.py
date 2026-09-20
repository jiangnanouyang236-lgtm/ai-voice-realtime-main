"""Helpers for cancelling gRPC streams and classifying local cancellations."""

from __future__ import annotations

import logging

import grpc


logger = logging.getLogger(__name__)


DEFAULT_GRPC_CHANNEL_OPTIONS = (
    ("grpc.keepalive_time_ms", 60000),
    ("grpc.keepalive_timeout_ms", 20000),
    ("grpc.http2.max_pings_without_data", 2),
)


def grpc_timeout_arg(timeout_seconds: float | int | None) -> float | None:
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def create_grpc_channel(target: str, *, secure: bool, options=None):
    channel_options = list(options or DEFAULT_GRPC_CHANNEL_OPTIONS)
    if secure:
        credentials = grpc.ssl_channel_credentials()
        return grpc.secure_channel(target, credentials, options=channel_options)
    return grpc.insecure_channel(target, options=channel_options)


def cancel_grpc_call_holder(holder: dict, label: str, session_id: str) -> bool:
    call = holder.get("call")
    if call is None:
        return False
    cancel = getattr(call, "cancel", None)
    if not callable(cancel):
        return False
    try:
        cancel()
        logger.info("会话 %s: 已取消 %s gRPC 流", session_id, label)
        return True
    except Exception as exc:
        logger.warning("会话 %s: 取消 %s gRPC 流失败: %s", session_id, label, exc)
        return False


def is_locally_cancelled_grpc_error(exc: Exception) -> bool:
    if not is_cancelled_grpc_error(exc):
        return False
    details = getattr(exc, "details", None)
    try:
        detail_text = details() if callable(details) else str(exc)
    except Exception:
        return False
    return "locally cancelled" in str(detail_text).lower()


def is_cancelled_grpc_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    try:
        return bool(callable(code) and code() == grpc.StatusCode.CANCELLED)
    except Exception:
        return False


def is_channel_ready(channel) -> bool:
    """
    检查 gRPC channel 是否处于可复用状态。

    gRPC 底层 API 返回整数状态，而测试替身也可能返回公开枚举；统一成
    connectivity code 后再判断。READY、IDLE 和正常建连中的 CONNECTING
    都应复用，避免并发请求在连接尚未 READY 时反复替换共享 channel。
    """
    if channel is None:
        return False

    try:
        state = channel._channel.check_connectivity_state(False)
        code = _connectivity_state_code(state)
        return code in {
            _connectivity_state_code(grpc.ChannelConnectivity.IDLE),
            _connectivity_state_code(grpc.ChannelConnectivity.CONNECTING),
            _connectivity_state_code(grpc.ChannelConnectivity.READY),
        }
    except Exception as exc:
        logger.warning("检查 channel 状态失败: %s", exc)
        return False


def _connectivity_state_code(state) -> int:
    if isinstance(state, grpc.ChannelConnectivity):
        value = state.value
        return int(value[0] if isinstance(value, tuple) else value)
    return int(state)


def channel_connectivity_label(channel) -> str:
    if channel is None:
        return "missing"
    try:
        state = channel._channel.check_connectivity_state(False)
        code = _connectivity_state_code(state)
        for connectivity in grpc.ChannelConnectivity:
            if _connectivity_state_code(connectivity) == code:
                return connectivity.name.lower()
        return f"unknown:{code}"
    except Exception as exc:
        return f"unknown: {exc}"
