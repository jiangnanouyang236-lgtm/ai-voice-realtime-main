from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import time
import uuid
from typing import Any

import httpx

from llm.agent_runtime import AgentToolInvoker, AgentToolResult

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，使用默认值 %s", name, value, default)
        return default


def _normalize_topic_part(value: Any, field_name: str) -> str:
    text = str(value or "").strip().strip("/")
    if not text:
        raise ValueError(f"{field_name} 不能为空")
    if any(ch in text for ch in ("/", "#", "+")) or any(ch.isspace() for ch in text):
        raise ValueError(f"{field_name} 不能包含空白字符、/、# 或 +")
    return text


@dataclass(frozen=True)
class CognitiveReportMqttPublisher:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    client_id: str
    keepalive: int
    qos: int
    topic_prefix: str
    topic_suffix: str
    method: str
    msg_id: int

    @classmethod
    def from_env(cls) -> "CognitiveReportMqttPublisher":
        host = os.getenv("COGNITIVE_REPORT_MQTT_HOST", "").strip()
        return cls(
            enabled=_env_bool("COGNITIVE_REPORT_MQTT_ENABLED", bool(host)),
            host=host,
            port=_env_int("COGNITIVE_REPORT_MQTT_PORT", 1883),
            username=os.getenv("COGNITIVE_REPORT_MQTT_USERNAME", "").strip(),
            password=os.getenv("COGNITIVE_REPORT_MQTT_PASSWORD", ""),
            client_id=os.getenv("COGNITIVE_REPORT_MQTT_CLIENT_ID", "").strip(),
            keepalive=_env_int("COGNITIVE_REPORT_MQTT_KEEPALIVE", 60),
            qos=_env_int("COGNITIVE_REPORT_MQTT_QOS", 0),
            topic_prefix=os.getenv("COGNITIVE_REPORT_MQTT_TOPIC_PREFIX", "windaka").strip().strip("/") or "windaka",
            topic_suffix=os.getenv("COGNITIVE_REPORT_TOPIC_SUFFIX", "device/cog_ass_report").strip().strip("/")
            or "device/cog_ass_report",
            method=os.getenv("COGNITIVE_REPORT_METHOD", "/device/cog_ass_report").strip()
            or "/device/cog_ass_report",
            msg_id=_env_int("COGNITIVE_REPORT_MSG_ID", 7),
        )

    async def publish(self, report: dict[str, Any]) -> AgentToolResult:
        report_id = str(report.get("report_id") or "")
        robot_id = str(report.get("robot_id") or "").strip()

        if not self.enabled:
            logger.info("认知报告 MQTT 上报已关闭: report_id=%s robot_id=%s", report_id or "-", robot_id or "-")
            return AgentToolResult(ok=True, message="disabled", data={"report_id": report_id})

        if not robot_id:
            return AgentToolResult(ok=False, message="missing robot_id", data={"report_id": report_id})
        if not self.host:
            return AgentToolResult(ok=False, message="missing mqtt host", data={"report_id": report_id})

        try:
            safe_robot_id = _normalize_topic_part(robot_id, "robot_id")
            topic = f"{self.topic_prefix}/{safe_robot_id}/mcp/{self.topic_suffix}"
            payload = {
                "msg_id": self.msg_id,
                "msg_type": "request",
                "method": self.method,
                "timestamp": int(time.time() * 1000),
                "data": report,
            }
            await asyncio.to_thread(self._publish_sync, topic, payload)
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "认知报告 MQTT 上报失败: robot_id=%s report_id=%s error=%s",
                robot_id,
                report_id or "-",
                error_message,
            )
            return AgentToolResult(
                ok=False,
                message=error_message,
                data={"report_id": report_id, "robot_id": robot_id},
            )

        logger.info(
            "认知报告 MQTT 上报成功: robot_id=%s report_id=%s topic=%s",
            robot_id,
            report_id or "-",
            topic,
        )
        return AgentToolResult(
            ok=True,
            message="published",
            data={"report_id": report_id, "robot_id": robot_id, "topic": topic},
        )

    def _publish_sync(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            from paho.mqtt import publish
        except ImportError as exc:
            raise RuntimeError("未安装 paho-mqtt，请先执行 pip install -r requirements.txt") from exc

        auth = None
        if self.username:
            auth = {
                "username": self.username,
                "password": self.password,
            }

        client_id = self.client_id or f"cognitive-report-{uuid.uuid4().hex[:8]}"
        publish.single(
            topic=topic,
            payload=json.dumps(payload, ensure_ascii=False),
            hostname=self.host,
            port=self.port,
            client_id=client_id,
            keepalive=self.keepalive,
            qos=self.qos,
            auth=auth,
        )


class DefaultAgentToolInvoker(AgentToolInvoker):
    def __init__(
        self,
        visitor_submit_url: str | None = None,
        cognitive_report_publisher: CognitiveReportMqttPublisher | None = None,
    ):
        self.visitor_submit_url = visitor_submit_url or os.getenv(
            "VISITOR_SUBMIT_URL",
            "http://localhost:12345/add",
        )
        self.cognitive_report_publisher = (
            cognitive_report_publisher or CognitiveReportMqttPublisher.from_env()
        )

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> AgentToolResult:
        if tool_name == "visitor.submit_registration":
            return await self._submit_visitor_registration(tool_name, arguments)
        if tool_name == "cognitive_report.publish":
            return await self.cognitive_report_publisher.publish(arguments or {})

        logger.warning("Agent FunctionCall 不存在: %s args=%s", tool_name, arguments)
        return AgentToolResult(
            ok=False,
            message=f"unknown tool: {tool_name}",
            data={"tool_name": tool_name},
        )

    async def _submit_visitor_registration(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> AgentToolResult:
        logger.info("Agent FunctionCall: %s args=%s", tool_name, arguments)
        payload = {
            "tool_name": tool_name,
            "arguments": arguments,
        }
        try:
            async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
                response = await client.post(self.visitor_submit_url, json=payload)
            logger.info(
                "Agent FunctionCall HTTP 完成: tool=%s, url=%s, status=%s",
                tool_name,
                self.visitor_submit_url,
                response.status_code,
            )
            return AgentToolResult(
                ok=200 <= response.status_code < 300,
                message=f"http_status={response.status_code}",
                data={
                    "tool_name": tool_name,
                    "url": self.visitor_submit_url,
                    "status_code": response.status_code,
                },
            )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc!r}"
            logger.warning(
                "Agent FunctionCall HTTP 失败: tool=%s, url=%s, error=%s",
                tool_name,
                self.visitor_submit_url,
                error_message,
            )
            return AgentToolResult(
                ok=False,
                message=error_message,
                data={
                    "tool_name": tool_name,
                    "url": self.visitor_submit_url,
                },
            )
