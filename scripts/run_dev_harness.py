#!/usr/bin/env python3
"""Run bounded local-service scenarios against authorized remote test dependencies."""

from __future__ import annotations

import argparse
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAFE_ROBOT_IDS = {"test_01"}
ACTION_CASES = (
    ("greet", 6, "请控制机器人和我打个招呼。"),
    ("cheer", 8, "请控制机器人欢呼一下。"),
)
DIRECT_TEXT_PROMPT = "请用一句话描述秋天的颜色。"
AGENT_ENTRY_PROMPT = "我感觉最近记忆有点跟不上了"
AGENT_CONFIRM_PROMPT = "好，开始吧"
UTILS_TIME_PROMPT = "请通过时间工具告诉我现在的完整日期、时间和星期。"
VOICE_AUDIO_FIXTURE = ROOT / "deploy/vllm/tts-base/base/jialan-6s.mp3"
VOICE_AUDIO_FIXTURE_SHA256 = "dca5c1b4328cc42a2eb3edfddad30e48b9f2ac545f597bb6e3465bb90f075020"
VOICE_AUDIO_EXPECTED_FRAGMENTS = ("先别着急", "听清楚", "重点", "明白")
VISION_FIXTURE = ROOT / "data/eval_gold/vision/classroom-001.jpg"
VISION_FIXTURE_SHA256 = "115e960ccdc599d35a5d15995d4f96938a92599637e772ea9d9001688dba98dc"
VISION_PROMPT = "请客观描述这张图片中的场景和主要可见元素。"
VISION_REQUIRED_FACT_GROUPS = (
    ("教室", "课堂"),
    ("学生", "同学"),
    ("课桌", "桌子"),
    ("黑板",),
    ("窗户", "窗边"),
    ("书本", "课本", "书籍"),
)


class HarnessRunError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_voice_audio_fixture() -> dict[str, Any]:
    if not VOICE_AUDIO_FIXTURE.is_file():
        raise HarnessRunError(f"固定语音夹具不存在: {VOICE_AUDIO_FIXTURE.relative_to(ROOT)}")
    digest = _sha256(VOICE_AUDIO_FIXTURE)
    if digest != VOICE_AUDIO_FIXTURE_SHA256:
        raise HarnessRunError("固定语音夹具 SHA-256 不匹配")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HarnessRunError("缺少 ffmpeg，无法只读转换固定语音夹具")
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(VOICE_AUDIO_FIXTURE),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    pcm = result.stdout
    if not pcm:
        raise HarnessRunError("固定语音夹具转换后为空")
    codec_env = os.environ.copy()
    opus_lib_dir = Path("/opt/homebrew/lib")
    if opus_lib_dir.exists():
        codec_env["DYLD_LIBRARY_PATH"] = os.pathsep.join(
            part
            for part in (
                str(opus_lib_dir),
                codec_env.get("DYLD_LIBRARY_PATH", ""),
            )
            if part
        )
    encoded = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from gateway.opus_audio import encode_pcm16_to_opus_packet_stream; "
                "payload,_=encode_pcm16_to_opus_packet_stream("
                "sys.stdin.buffer.read(),sample_rate=16000,channels=1,opus_frame_ms=20); "
                "sys.stdout.buffer.write(payload)"
            ),
        ],
        cwd=ROOT,
        env=codec_env,
        input=pcm,
        check=True,
        capture_output=True,
    )
    opus_payload = encoded.stdout
    from gateway.opus_audio import parse_opus_packet_stream

    packets = parse_opus_packet_stream(opus_payload)
    return {
        "source_path": str(VOICE_AUDIO_FIXTURE.relative_to(ROOT)),
        "source_sha256": digest,
        "source_kind": "tracked_reference_recording",
        "pcm_bytes": len(pcm),
        "opus_payload": opus_payload,
        "opus_bytes": len(opus_payload),
        "packet_count": len(packets),
        "duration_ms": int(len(pcm) / 2 / 16000 * 1000),
        "opus_metadata": {
            "sample_rate": 16000,
            "channels": 1,
            "opus_frame_ms": 20,
            "packet_count": len(packets),
        },
    }


def _prepare_voice_wav_fixture(output_path: Path) -> dict[str, Any]:
    if not VOICE_AUDIO_FIXTURE.is_file() or _sha256(VOICE_AUDIO_FIXTURE) != VOICE_AUDIO_FIXTURE_SHA256:
        raise HarnessRunError("固定语音夹具不存在或 SHA-256 不匹配")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise HarnessRunError("缺少 ffmpeg，无法只读转换固定语音夹具")
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(VOICE_AUDIO_FIXTURE),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        cwd=ROOT,
        check=True,
    )
    return {
        "source_path": str(VOICE_AUDIO_FIXTURE.relative_to(ROOT)),
        "source_sha256": VOICE_AUDIO_FIXTURE_SHA256,
        "wav_path": str(output_path.relative_to(ROOT)),
        "wav_sha256": _sha256(output_path),
    }


def _m1_internal_voice_env(gateway_port: int) -> dict[str, str]:
    return {
        "GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE": "m1",
        "GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL": (
            f"ws://127.0.0.1:{gateway_port}/internal/voice/ws"
        ),
    }


def _git_state() -> tuple[str, bool]:
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=ROOT,
            text=True,
        ).strip()
    )
    return commit, dirty


def _safe_output_dir(path: Path) -> Path:
    resolved = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    allowed = ((ROOT / "reports").resolve(), (ROOT / "tmp").resolve())
    if not any(resolved.is_relative_to(parent) for parent in allowed):
        raise HarnessRunError("输出目录必须位于 reports/ 或 tmp/")
    return resolved


def _port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _proxy_bypass_hosts(env: dict[str, str]) -> set[str]:
    hosts = {"127.0.0.1", "localhost"}
    for key in (
        "CONFIG_DATABASE_URL",
        "LLM_BASE_URL",
        "LLM_ROUTER_BASE_URL",
        "LLM_VISION_GATEWAY_BASE_URL",
        "QWEN_ASR_BASE_URL",
        "QWEN3_TTS_CUSTOM_VOICE_WS_URL",
        "QWEN3_TTS_BASE_WS_URL",
    ):
        value = env.get(key, "").strip()
        if not value:
            continue
        host = urlsplit(value).hostname
        if host:
            hosts.add(host)
    mqtt_host = env.get("ROBOT_MQTT_HOST", "").strip()
    if mqtt_host:
        hosts.add(mqtt_host)
    return hosts


def _resolve_voice_robot_secret(env: dict[str, str], robot_id: str) -> str:
    if env.get("GATEWAY_REQUIRE_ROBOT_SECRET", "false").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return ""
    configured_robot_id = env.get("ROBOT_ID", "").strip()
    if configured_robot_id and configured_robot_id != robot_id:
        raise HarnessRunError(
            "ROBOT_ID 与目标 Robot 不一致；拒绝尝试来源不明确的 Robot Secret"
        )
    robot_secret = env.get("ROBOT_SECRET", "").strip()
    if not robot_secret:
        raise HarnessRunError(
            "当前 Gateway 强制 Robot Secret，但环境未配置目标 Robot 凭据"
        )
    return robot_secret


def _load_voice_private_credentials(
    env: dict[str, str], path: Path | None = None
) -> dict[str, str]:
    merged = env.copy()
    private_path = path or ROOT / "rust_client" / ".env.local"
    if not private_path.exists():
        return merged
    allowed = {"ROBOT_ID", "ROBOT_SECRET"}
    for raw_line in private_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed or merged.get(key, "").strip():
            continue
        merged[key] = value.strip().strip("'").strip('"')
    return merged


def _wait_http(url: str, proc: subprocess.Popen[Any], timeout: float) -> None:
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise HarnessRunError(f"健康检查只允许 localhost: {url}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise HarnessRunError(f"服务提前退出: {url}, exit={proc.returncode}")
        try:
            connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=1)
            connection.request("GET", parsed.path or "/")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise HarnessRunError(f"服务就绪超时: {url}")


def _wait_port(port: int, proc: subprocess.Popen[Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise HarnessRunError(f"服务提前退出: port={port}, exit={proc.returncode}")
        if not _port_is_free(port):
            return
        time.sleep(0.2)
    raise HarnessRunError(f"服务就绪超时: port={port}")


def _start_process(
    name: str,
    command: list[str],
    *,
    env: dict[str, str],
    log_dir: Path,
) -> subprocess.Popen[Any]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    log_handle = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        log_handle.close()


def _stop_processes(processes: list[tuple[str, subprocess.Popen[Any]]]) -> None:
    for _name, proc in reversed(processes):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 8
    for _name, proc in reversed(processes):
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _run_preflight(log_dir: Path, *, profile: str = "hybrid") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/ai_preflight.py",
            "--profile",
            profile,
            "--connectivity",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (log_dir / "preflight.log").write_text(result.stdout, encoding="utf-8")
    if result.returncode != 0:
        raise HarnessRunError("hybrid connectivity preflight 未通过，详见 preflight.log")


def _load_runtime_target(robot_id: str, requested_bot_id: str | None) -> dict[str, Any]:
    import config
    from server_config.repository import ConfigRepository

    if not config.MCP_ENABLED:
        raise HarnessRunError("MCP_ENABLED=false，拒绝通过修改配置制造测试通过")
    snapshot = ConfigRepository(config.CONFIG_DATABASE_URL).load_runtime_snapshot()
    robot = snapshot.robots.get(robot_id)
    if robot is None or not robot.enabled:
        raise HarnessRunError(f"Runtime Snapshot 中 Robot 不存在或未启用: {robot_id}")
    bot_id = requested_bot_id or robot.assigned_bot_id
    if bot_id != robot.assigned_bot_id:
        raise HarnessRunError(
            f"Bot 必须使用 Robot 运行态绑定: expected={robot.assigned_bot_id}, requested={bot_id}"
        )
    bot = snapshot.bots.get(bot_id)
    if bot is None or not bot.enabled:
        raise HarnessRunError(f"Runtime Snapshot 中 Bot 不存在或未启用: {bot_id}")
    if "robot_remote" not in bot.mcp_servers:
        raise HarnessRunError(f"Bot 未绑定 robot_remote: {bot_id}")
    mcp = snapshot.mcp_servers.get("robot_remote")
    if mcp is None or not mcp.enabled or mcp.type != "sse" or not mcp.url:
        raise HarnessRunError("robot_remote 必须是已启用且带 URL 的 SSE Server")
    parsed = urlsplit(mcp.url)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.path != "/mcp":
        raise HarnessRunError("robot_remote 必须指向当前机器的 /mcp，拒绝启动来源不明的远端 MCP")
    mcp_port = parsed.port or 80
    utils = snapshot.mcp_servers.get("utils_remote")
    utils_mcp_url = ""
    utils_mcp_port = None
    if "utils_remote" in bot.mcp_servers:
        if utils is None or not utils.enabled or utils.type != "sse" or not utils.url:
            raise HarnessRunError("utils_remote 必须是已启用且带 URL 的 SSE Server")
        utils_parsed = urlsplit(utils.url)
        if (
            utils_parsed.hostname not in {"127.0.0.1", "localhost"}
            or utils_parsed.path != "/mcp"
        ):
            raise HarnessRunError(
                "utils_remote 必须指向当前机器的 /mcp，拒绝启动来源不明的远端 MCP"
            )
        utils_mcp_url = utils.url
        utils_mcp_port = utils_parsed.port or 80
    return {
        "config_version": snapshot.config_version,
        "source": snapshot.source,
        "robot_id": robot_id,
        "bot_id": bot_id,
        "bot_name": bot.name,
        "bot_mcp_servers": list(bot.mcp_servers),
        "mcp_url": mcp.url,
        "mcp_port": mcp_port,
        "utils_mcp_url": utils_mcp_url,
        "utils_mcp_port": utils_mcp_port,
    }


def _load_voice_runtime_target(robot_id: str, requested_bot_id: str | None) -> dict[str, Any]:
    import config
    from server_config.repository import ConfigRepository

    snapshot = ConfigRepository(config.CONFIG_DATABASE_URL).load_runtime_snapshot()
    robot = snapshot.robots.get(robot_id)
    if robot is None or not robot.enabled:
        raise HarnessRunError(f"Runtime Snapshot 中 Robot 不存在或未启用: {robot_id}")
    bot_id = requested_bot_id or robot.assigned_bot_id
    if bot_id != robot.assigned_bot_id:
        raise HarnessRunError(
            f"Bot 必须使用 Robot 运行态绑定: expected={robot.assigned_bot_id}, requested={bot_id}"
        )
    bot = snapshot.bots.get(bot_id)
    if bot is None or not bot.enabled:
        raise HarnessRunError(f"Runtime Snapshot 中 Bot 不存在或未启用: {bot_id}")
    profile = snapshot.tts_profiles.get(bot.tts_profile_id)
    if profile is None or not profile.enabled:
        raise HarnessRunError(
            f"Bot 绑定的 TTS Profile 不存在或未启用: {bot.tts_profile_id}"
        )
    return {
        "config_version": snapshot.config_version,
        "source": snapshot.source,
        "robot_id": robot_id,
        "bot_id": bot_id,
        "bot_name": bot.name,
        "tts_profile_id": profile.tts_id,
        "tts_provider_type": profile.provider_type,
    }


class MqttCapture:
    def __init__(self, *, topic: str, expected_trace_ids: set[str]):
        from paho.mqtt import client as mqtt

        self._mqtt = mqtt
        self.topic = topic
        self.expected_trace_ids = expected_trace_ids
        self.messages: list[dict[str, Any]] = []
        self.subscribed = threading.Event()
        self.complete = threading.Event()
        self.error: str | None = None
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"dev-harness-{uuid.uuid4().hex[:10]}",
        )
        username = os.getenv("ROBOT_MQTT_USERNAME", "").strip()
        if username:
            self.client.username_pw_set(username, os.getenv("ROBOT_MQTT_PASSWORD", ""))
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        reason_value = int(getattr(reason_code, "value", reason_code))
        if reason_value != 0:
            self.error = f"MQTT connect failed: reason_code={reason_value}"
            self.complete.set()
            return
        client.subscribe(self.topic, qos=int(os.getenv("ROBOT_MQTT_QOS", "0")))

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None) -> None:
        self.subscribed.set()

    def _on_message(self, client, userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        trace_id = str(payload.get("meta", {}).get("trace_id", ""))
        if trace_id not in self.expected_trace_ids:
            return
        self.messages.append(
            {
                "topic": message.topic,
                "qos": message.qos,
                "retain": bool(message.retain),
                "payload": payload,
            }
        )
        captured = {
            str(item["payload"].get("meta", {}).get("trace_id", ""))
            for item in self.messages
        }
        if self.expected_trace_ids.issubset(captured):
            self.complete.set()

    def start(self, timeout: float) -> None:
        try:
            self.client.connect(
                os.environ["ROBOT_MQTT_HOST"],
                int(os.getenv("ROBOT_MQTT_PORT", "1883")),
                int(os.getenv("ROBOT_MQTT_KEEPALIVE", "60")),
            )
        except Exception as exc:
            raise HarnessRunError(f"MQTT 连接失败: {exc}") from exc
        self.client.loop_start()
        if not self.subscribed.wait(timeout):
            raise HarnessRunError(self.error or "MQTT 精确 Topic 订阅超时")

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


def _invoke_llm_actions(
    *,
    port: int,
    runtime: dict[str, Any],
    run_id: str,
    traces: dict[str, str],
    timeout: float,
) -> list[dict[str, Any]]:
    import grpc
    from llm import llm_service_pb2, llm_service_pb2_grpc

    stub = llm_service_pb2_grpc.LLMServiceStub(
        grpc.insecure_channel(f"127.0.0.1:{port}")
    )
    results = []
    for action, expected_type, prompt in ACTION_CASES:
        chunks: list[str] = []
        metrics: dict[str, Any] = {}
        request = llm_service_pb2.ChatRequest(
            text=prompt,
            session_id=f"dev-harness-{run_id}",
            bot_id=runtime["bot_id"],
            robot_id=runtime["robot_id"],
            trace_id=traces[action],
        )
        try:
            for response in stub.StreamChat(request, timeout=timeout):
                if response.text:
                    chunks.append(response.text)
                if response.metrics_json:
                    metrics = json.loads(response.metrics_json)
        except grpc.RpcError as exc:
            raise HarnessRunError(
                f"LLM gRPC 调用失败: action={action}, code={exc.code().name}"
            ) from exc
        if metrics.get("llm_selected_tool_name") != "robot_remote__move_robot":
            raise HarnessRunError(f"{action} 未选择 robot_remote__move_robot")
        if int(metrics.get("llm_tool_call_count", 0)) != 1:
            raise HarnessRunError(f"{action} 工具调用次数不是 1")
        results.append(
            {
                "action": action,
                "expected_type": expected_type,
                "prompt": prompt,
                "trace_id": traces[action],
                "response": "".join(chunks),
                "metrics": metrics,
            }
        )
    return results


def _verify_capture(
    *,
    capture: MqttCapture,
    traces: dict[str, str],
    topic: str,
) -> None:
    by_trace = {
        str(item["payload"].get("meta", {}).get("trace_id", "")): item
        for item in capture.messages
    }
    for action, expected_type, _prompt in ACTION_CASES:
        trace_id = traces[action]
        item = by_trace.get(trace_id)
        if item is None:
            raise HarnessRunError(f"MQTT 未收到 action={action}, trace_id={trace_id}")
        payload = item["payload"]
        if item["topic"] != topic:
            raise HarnessRunError(f"MQTT Topic 不匹配: {item['topic']}")
        if item["retain"]:
            raise HarnessRunError(f"MQTT 消息不应 retained: action={action}")
        if payload.get("method") != "/task/manual_control_cmd":
            raise HarnessRunError(f"MQTT method 不匹配: action={action}")
        if payload.get("data", {}).get("type") != expected_type:
            raise HarnessRunError(f"MQTT action type 不匹配: action={action}")
        if payload.get("meta", {}).get("session_id") == trace_id:
            raise HarnessRunError("session_id 不能冒充逐轮 trace_id")


def _write_certified_report(
    *,
    output_dir: Path,
    raw_path: Path,
    runtime: dict[str, Any],
    traces: dict[str, str],
    authorization_reference: str,
) -> Path:
    from scripts.capture_evidence_environment import build_inventory
    from scripts.ai_preflight import load_manifest
    from scripts.validate_evidence_report import validate_report

    inventory = build_inventory(
        root=ROOT,
        profile="hybrid",
        capability="robot_mcp",
        claimed_level="HYBRID_VERIFIED",
    )
    environment_path = output_dir / "environment.json"
    environment_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    binding = f"{runtime['robot_id']}:{runtime['bot_id']}"
    trace_ids = list(traces.values())
    observed_endpoints = inventory["endpoints"]
    endpoint_scope = "external" if any(
        item["scope"] == "external" for item in observed_endpoints
    ) else "lan"
    evidence = []
    for component in ("runtime_snapshot", "llm", "mcp", "mqtt_broker"):
        evidence.append(
            {
                "id": component,
                "status": "PASS",
                "kind": "hybrid_integration",
                "component": component,
                "source": "artifact",
                "observed_at": _now(),
                "summary": f"{component} observed both bounded test_01 actions",
                "trace_ids": trace_ids,
                "bot_bindings": [binding],
                "artifact": str(raw_path.relative_to(ROOT)),
                "sha256": _sha256(raw_path),
            }
        )
    report = {
        "schema_version": "ai-voice-evidence/v1",
        "objective": "本地当前代码通过 test_01 验证 LLM-MCP-线上 MQTT 独立消费闭环",
        "commit": inventory["commit"],
        "capability": "robot_mcp",
        "claimed_level": "HYBRID_VERIFIED",
        "endpoint_scope": endpoint_scope,
        "observed_endpoints": observed_endpoints,
        "authorization": {
            "connectivity": True,
            "external_write": True,
            "hardware_action": False,
            "reference": authorization_reference,
        },
        "assertions": {
            "runtime_snapshot": True,
            "same_trace": True,
            "trace_ids": trace_ids,
            "bot_bindings": [binding],
        },
        "environment_artifact": {
            "artifact": str(environment_path.relative_to(ROOT)),
            "sha256": _sha256(environment_path),
        },
        "evidence": evidence,
        "unknowns": [
            "未经过部署环境 Gateway，因此不是 LIVE_VERIFIED",
            "Broker 独立消费成功不等于 Robot 业务消费者 ACK",
            "单 Bot smoke 不替代 Live 多 Bot 共享路由验收",
        ],
    }
    report_path = output_dir / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    problems = validate_report(
        report,
        load_manifest(ROOT),
        report_path=report_path,
        root=ROOT,
    )
    if problems:
        raise HarnessRunError("证据报告未通过 Harness：" + "; ".join(problems))
    return report_path


def _verify_voice_client_event_probe(
    *,
    detail: dict[str, Any],
    expected_event: str,
) -> dict[str, Any]:
    trace_id = str(detail.get("trace_id") or "").strip()
    session_id = str(detail.get("session_id") or "").strip()
    if not trace_id or not session_id:
        raise HarnessRunError("Voice probe 缺少 session_id 或 trace_id")
    if detail.get("error"):
        raise HarnessRunError(f"Voice probe 失败: {detail['error']}")

    messages = [
        item.get("message")
        for item in detail.get("messages", [])
        if isinstance(item, dict) and isinstance(item.get("message"), dict)
    ]
    message_types = [str(item.get("type") or "") for item in messages]
    if "protocol.error" in message_types:
        raise HarnessRunError("Voice probe 返回 protocol.error")
    for required_type in (
        "session.opened",
        "orchestrator.status",
        "playback_start",
        "<binary>",
        "response.done",
        "session.closed",
    ):
        if required_type not in message_types:
            raise HarnessRunError(f"Voice probe 缺少消息: {required_type}")

    accepted = next(
        (
            item
            for item in messages
            if item.get("type") == "orchestrator.status"
            and (item.get("payload") or {}).get("mode") == "client_event_active_accepted"
        ),
        None,
    )
    if accepted is None or accepted.get("trace_id") != trace_id:
        raise HarnessRunError("client_event active ACK 未绑定目标 trace")
    if (accepted.get("payload") or {}).get("event") != expected_event:
        raise HarnessRunError("client_event active ACK 的 event 不匹配")

    playback_start = next(
        (item for item in messages if item.get("type") == "playback_start"),
        None,
    )
    if playback_start is None or playback_start.get("trace_id") != trace_id:
        raise HarnessRunError("playback_start 未绑定目标 trace")

    audio_messages = [item for item in messages if item.get("type") == "<binary>"]
    audio_payload_bytes = 0
    for item in audio_messages:
        if item.get("decode_error"):
            raise HarnessRunError(f"Voice 音频帧无法解码: {item['decode_error']}")
        header = item.get("header")
        if not isinstance(header, dict):
            raise HarnessRunError("Voice 音频帧缺少 VAF1 header")
        if header.get("event_type") != "response.audio":
            raise HarnessRunError("Voice 音频帧 event_type 不是 response.audio")
        if header.get("direction") != "downlink":
            raise HarnessRunError("Voice 音频帧 direction 不是 downlink")
        if header.get("trace_id") != trace_id or header.get("session_id") != session_id:
            raise HarnessRunError("Voice 音频帧未绑定目标 session/trace")
        payload_bytes = item.get("payload_bytes")
        if not isinstance(payload_bytes, int) or payload_bytes <= 0:
            raise HarnessRunError("Voice 音频帧 payload 为空")
        audio_payload_bytes += payload_bytes

    response_done = next(
        (item for item in messages if item.get("type") == "response.done"),
        None,
    )
    if response_done is None or response_done.get("trace_id") != trace_id:
        raise HarnessRunError("response.done 未绑定目标 trace")
    done_payload = response_done.get("payload") or {}
    if done_payload.get("reason") != "completed" or done_payload.get("event") != expected_event:
        raise HarnessRunError("response.done 未声明目标 client_event 正常完成")

    return {
        "session_id": session_id,
        "trace_id": trace_id,
        "event": expected_event,
        "message_types": message_types,
        "audio_frames": len(audio_messages),
        "audio_payload_bytes": audio_payload_bytes,
        "playback_id": playback_start.get("playback_id"),
    }


def _verify_agent_continuity_probe(detail: dict[str, Any]) -> dict[str, Any]:
    session_id = str(detail.get("session_id") or "").strip()
    turns = detail.get("turns") or []
    if not session_id or len(turns) != 2:
        raise HarnessRunError("Agent continuity probe 缺少同一 M1 session 的两轮结果")
    verified_turns = []
    for expected_index, turn in enumerate(turns, start=1):
        messages = turn.get("messages") or []
        json_messages = [item for item in messages if item.get("type") != "<binary>"]
        binary_messages = [item for item in messages if item.get("type") == "<binary>"]
        accepted = [
            item
            for item in json_messages
            if item.get("type") == "orchestrator.status"
            and (item.get("payload") or {}).get("mode") == "input_text_active_accepted"
        ]
        asr = [item for item in json_messages if item.get("type") == "response.asr"]
        done = [item for item in json_messages if item.get("type") == "response.done"]
        errors = [
            item
            for item in json_messages
            if item.get("type") in {"response.error", "protocol.error"}
        ]
        if not accepted or not asr or not done or not binary_messages or errors:
            raise HarnessRunError(
                f"Agent continuity 第 {expected_index} 轮缺少 ACK/ASR/TTS/done 或出现错误"
            )
        for item in accepted + asr + done:
            if item.get("session_id") != session_id:
                raise HarnessRunError("Agent continuity typed response 切换了 session_id")
        for item in binary_messages:
            if (item.get("header") or {}).get("session_id") != session_id:
                raise HarnessRunError("Agent continuity TTS 音频切换了 session_id")
        verified_turns.append(
            {
                "turn": expected_index,
                "trace_id": turn.get("trace_id"),
                "utterance_id": turn.get("utterance_id"),
                "response_asr": (asr[0].get("payload") or {}).get("text"),
                "audio_frames": len(binary_messages),
                "terminal": done[-1].get("type"),
            }
        )
    return {"session_id": session_id, "turns": verified_turns}


def _verify_active_audio_probe(detail: dict[str, Any]) -> dict[str, Any]:
    trace_id = str(detail.get("trace_id") or "").strip()
    session_id = str(detail.get("session_id") or "").strip()
    utterance_id = str(detail.get("utterance_id") or "").strip()
    if not trace_id or not session_id or not utterance_id:
        raise HarnessRunError("Audio probe 缺少 session/trace/utterance")
    if detail.get("error"):
        raise HarnessRunError(f"Audio probe 失败: {detail['error']}")
    messages = [
        item.get("message")
        for item in detail.get("messages", [])
        if isinstance(item, dict) and isinstance(item.get("message"), dict)
    ]
    message_types = [str(item.get("type") or "") for item in messages]
    for required_type in (
        "session.opened",
        "response.asr",
        "playback_start",
        "<binary>",
        "response.done",
        "session.closed",
    ):
        if required_type not in message_types:
            raise HarnessRunError(f"Audio probe 缺少消息: {required_type}")
    if any(item in message_types for item in ("response.error", "protocol.error")):
        raise HarnessRunError("Audio probe 返回错误终态")
    asr = next(item for item in messages if item.get("type") == "response.asr")
    if asr.get("trace_id") != trace_id or asr.get("utterance_id") != utterance_id:
        raise HarnessRunError("ASR 响应未绑定目标 trace/utterance")
    asr_payload = asr.get("payload") or {}
    asr_text = str(asr_payload.get("text") or "").strip()
    missing_fragments = [item for item in VOICE_AUDIO_EXPECTED_FRAGMENTS if item not in asr_text]
    if missing_fragments:
        raise HarnessRunError("固定语音 ASR 事实校验失败: " + ",".join(missing_fragments))
    audio_messages = [item for item in messages if item.get("type") == "<binary>"]
    audio_payload_bytes = 0
    for item in audio_messages:
        header = item.get("header") or {}
        if header.get("event_type") != "response.audio" or header.get("direction") != "downlink":
            raise HarnessRunError("Audio probe 下行音频协议不匹配")
        if header.get("trace_id") != trace_id or header.get("session_id") != session_id:
            raise HarnessRunError("Audio probe 下行音频未绑定目标 session/trace")
        audio_payload_bytes += int(item.get("payload_bytes") or 0)
    if audio_payload_bytes <= 0:
        raise HarnessRunError("Audio probe 下行音频为空")
    terminal = next(item for item in messages if item.get("type") == "response.done")
    if terminal.get("trace_id") != trace_id:
        raise HarnessRunError("Audio probe response.done 未绑定目标 trace")
    if (terminal.get("payload") or {}).get("reason") != "completed":
        raise HarnessRunError("Audio probe 未正常完成")
    return {
        "session_id": session_id,
        "trace_id": trace_id,
        "utterance_id": utterance_id,
        "asr_text": asr_text,
        "message_types": message_types,
        "audio_frames": len(audio_messages),
        "audio_payload_bytes": audio_payload_bytes,
    }


def _write_voice_certified_report(
    *,
    output_dir: Path,
    raw_path: Path,
    runtime: dict[str, Any],
    trace_id: str,
    authorization_reference: str,
) -> Path:
    from scripts.capture_evidence_environment import build_inventory
    from scripts.ai_preflight import load_manifest
    from scripts.validate_evidence_report import validate_report

    inventory = build_inventory(
        root=ROOT,
        profile="local",
        capability="voice_client_event",
        claimed_level="HYBRID_VERIFIED",
    )
    environment_path = output_dir / "environment.json"
    environment_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    binding = f"{runtime['robot_id']}:{runtime['bot_id']}"
    observed_endpoints = inventory["endpoints"]
    endpoint_scope = "external" if any(
        item["scope"] == "external" for item in observed_endpoints
    ) else "lan"
    artifact = str(raw_path.relative_to(ROOT))
    evidence = [
        {
            "id": component,
            "status": "PASS",
            "kind": "hybrid_integration",
            "component": component,
            "source": "artifact",
            "observed_at": _now(),
            "summary": f"{component} observed active client_event TTS audio",
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
            "artifact": artifact,
            "sha256": _sha256(raw_path),
        }
        for component in ("runtime_snapshot", "python_gateway", "tts")
    ]
    report = {
        "schema_version": "ai-voice-evidence/v1",
        "objective": "本地 Python Gateway 连接测试数据库和内网 TTS，验证 active client_event 音频闭环",
        "commit": inventory["commit"],
        "capability": "voice_client_event",
        "claimed_level": "HYBRID_VERIFIED",
        "endpoint_scope": endpoint_scope,
        "observed_endpoints": observed_endpoints,
        "authorization": {
            "connectivity": True,
            "external_write": False,
            "hardware_action": False,
            "reference": authorization_reference,
        },
        "assertions": {
            "runtime_snapshot": True,
            "same_trace": True,
            "real_audio": True,
            "active_client_event": True,
            "llm_bypassed_by_contract": True,
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
        },
        "environment_artifact": {
            "artifact": str(environment_path.relative_to(ROOT)),
            "sha256": _sha256(environment_path),
        },
        "evidence": evidence,
        "unknowns": [
            "client_event 按协议绕过 ASR、LLM、Router 和会话历史",
            "未经过 Go Gateway、Rust Client 或部署环境",
            "未验证声卡播放和实体硬件",
        ],
    }
    report_path = output_dir / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    problems = validate_report(
        report,
        load_manifest(ROOT),
        report_path=report_path,
        root=ROOT,
    )
    if problems:
        raise HarnessRunError("证据报告未通过 Harness：" + "; ".join(problems))
    return report_path


def _verify_direct_text_probe(
    *,
    probe: dict[str, Any],
    trace_payload: dict[str, Any],
    runtime: dict[str, Any],
    expected_route: str = "chat",
    expected_action: str | None = None,
) -> dict[str, Any]:
    trace_id = str(probe.get("trace_id") or "").strip()
    session_id = str(probe.get("session_id") or "").strip()
    if not probe.get("ok") or not trace_id or not session_id:
        raise HarnessRunError("Direct text probe 缺少成功状态或 session/trace")
    if probe.get("robot_id") != runtime["robot_id"] or probe.get("bot_id") != runtime["bot_id"]:
        raise HarnessRunError("Direct text probe 的 Robot/Bot 与 Runtime Snapshot 不一致")

    messages = probe.get("messages") or []
    message_types = [str(item.get("type") or "") for item in messages]
    if "done" not in message_types:
        raise HarnessRunError("Direct text probe 缺少 done")
    echoed = next(
        (
            item.get("message")
            for item in messages
            if item.get("type") == "text"
            and (item.get("message") or {}).get("content") == probe.get("input_text")
        ),
        None,
    )
    if echoed is None:
        raise HarnessRunError("Gateway 未确认 direct text 输入")
    done = next((item.get("message") for item in messages if item.get("type") == "done"), None)
    if not isinstance(done, dict) or done.get("trace_id") != trace_id:
        raise HarnessRunError("done 未绑定目标 trace")
    round_id = str(done.get("round_id") or "").strip()
    playback_id = str(done.get("playback_id") or "").strip()
    if not round_id or not playback_id:
        raise HarnessRunError("done 缺少 round_id 或 playback_id")

    audio_frames = probe.get("audio_frames") or []
    audio_payload_bytes = 0
    for frame in audio_frames:
        if frame.get("decode_error"):
            raise HarnessRunError(f"Direct text 音频帧无法解码: {frame['decode_error']}")
        header = frame.get("header")
        if not isinstance(header, dict):
            raise HarnessRunError("Direct text 音频帧缺少 VAF1 header")
        if (
            header.get("type") != "audio_frame"
            or header.get("direction") != "server_tts"
            or header.get("encoding") != "opus"
        ):
            raise HarnessRunError("Direct text 音频帧不符合 /ws server_tts Opus 契约")
        if (
            header.get("trace_id") != trace_id
            or header.get("round_id") != round_id
            or header.get("playback_id") != playback_id
        ):
            raise HarnessRunError("Direct text 音频帧未绑定目标 trace/round/playback")
        payload_bytes = frame.get("bytes")
        if not isinstance(payload_bytes, int) or payload_bytes <= 0:
            raise HarnessRunError("Direct text 音频 payload 为空")
        audio_payload_bytes += payload_bytes
    if not audio_frames:
        raise HarnessRunError("Direct text 未返回真实音频")

    trace = trace_payload.get("trace") if isinstance(trace_payload, dict) else None
    if not trace_payload.get("success") or not isinstance(trace, dict):
        raise HarnessRunError("Gateway 未返回目标 trace")
    if trace.get("trace_id") != trace_id or trace.get("session_id") != session_id:
        raise HarnessRunError("Gateway trace 与 probe session/trace 不一致")
    if trace.get("robot_id") != runtime["robot_id"] or trace.get("bot_id") != runtime["bot_id"]:
        raise HarnessRunError("Gateway trace 的 Robot/Bot 绑定不一致")
    stages = [
        str(item.get("stage") or "")
        for item in trace.get("events", [])
        if isinstance(item, dict)
    ]
    for stage in (
        "text_received",
        "direct_text_ready",
        "llm_internal_metrics",
        "llm_first_token",
        "llm_done",
        "tts_first_audio",
        "llm_tts_done",
    ):
        if stage not in stages:
            raise HarnessRunError(f"Gateway trace 缺少真实阶段: {stage}")
    metrics = trace.get("metrics") or {}
    if int(metrics.get("llm_response_chars") or 0) <= 0:
        raise HarnessRunError("主 LLM 未生成有效文本")
    llm_done_event = next(
        (
            item
            for item in reversed(trace.get("events", []))
            if isinstance(item, dict) and item.get("stage") == "llm_done"
        ),
        None,
    )
    response_text = str(((llm_done_event or {}).get("summary") or {}).get("text") or "").strip()
    if not response_text:
        raise HarnessRunError("Gateway trace 未保留主 LLM 最终文本")

    if expected_route == "chat":
        if metrics.get("llm_router_classifier_used") is not True:
            raise HarnessRunError("本轮未真实调用 Router 分类器")
        if "llm_router" not in str(metrics.get("llm_router_source") or ""):
            raise HarnessRunError("Router source 不是独立 LLM Router")
        if metrics.get("llm_router_kind") != "chat":
            raise HarnessRunError("安全 direct text 基线未被 Router 判定为 chat")
        if metrics.get("llm_stream_mode") != "chat_bypass_tools":
            raise HarnessRunError("安全 direct text 基线未进入 chat_bypass_tools")
        if int(metrics.get("llm_tool_call_count") or 0) != 0:
            raise HarnessRunError("安全 direct text 基线不应调用工具")
    elif expected_route == "utils":
        from scripts.eval_bot_utils_matrix import expected_time_facts, validate_time_fidelity

        if metrics.get("llm_router_kind") != "tool" or metrics.get("llm_router_category") != "utils":
            raise HarnessRunError("时间请求未被 Router 判定为 utils 工具路线")
        selected_tool = str(metrics.get("llm_selected_tool_name") or "").replace(
            ".", "__"
        )
        if selected_tool != "utils_remote__get_now_context":
            raise HarnessRunError(
                "时间请求未选择权威 get_now_context 工具"
                f"（observed={selected_tool or '<missing>'}）"
            )
        if int(metrics.get("llm_first_round_tools_count") or 0) != 1:
            raise HarnessRunError("时间工具首轮候选未收窄为唯一确定性工具")
        if int(metrics.get("llm_tool_call_count") or 0) != 1:
            raise HarnessRunError("时间工具必须且只能执行一次")
        fidelity_errors = validate_time_fidelity(response_text, expected_time_facts())
        if fidelity_errors:
            raise HarnessRunError("时间工具回答事实校验失败: " + ",".join(fidelity_errors))
    elif expected_route == "robot":
        if expected_action not in {item[0] for item in ACTION_CASES}:
            raise HarnessRunError(f"未知 Robot 动作断言: {expected_action}")
        if (
            metrics.get("llm_router_kind") != "tool"
            or metrics.get("llm_router_category") != "robot"
        ):
            raise HarnessRunError(f"{expected_action} 未被 Router 判定为 robot 工具路线")
        selected_tool = str(metrics.get("llm_selected_tool_name") or "").replace(
            ".", "__"
        )
        if selected_tool != "robot_remote__move_robot":
            raise HarnessRunError(
                f"{expected_action} 未选择 robot_remote__move_robot"
            )
        if int(metrics.get("llm_first_round_tools_count") or 0) != 1:
            raise HarnessRunError(f"{expected_action} 首轮候选未收窄为唯一工具")
        if int(metrics.get("llm_tool_call_count") or 0) != 1:
            raise HarnessRunError(f"{expected_action} 工具调用次数不是 1")
    else:
        raise HarnessRunError(f"未知 direct text 路由断言: {expected_route}")

    return {
        "session_id": session_id,
        "trace_id": trace_id,
        "round_id": round_id,
        "playback_id": playback_id,
        "input_text": probe.get("input_text"),
        "message_types": message_types,
        "audio_frames": len(audio_frames),
        "audio_payload_bytes": audio_payload_bytes,
        "stages": stages,
        "metrics": metrics,
        "response_text": response_text,
        "diagnosis": trace.get("diagnosis"),
    }


def _write_direct_text_certified_report(
    *,
    output_dir: Path,
    raw_path: Path,
    runtime: dict[str, Any],
    trace_id: str,
    authorization_reference: str,
    utils_matrix: dict[str, Any] | None = None,
) -> Path:
    from scripts.capture_evidence_environment import build_inventory
    from scripts.ai_preflight import load_manifest
    from scripts.validate_evidence_report import validate_report

    utils_route = isinstance(utils_matrix, dict)
    capability = "utils_tool_route" if utils_route else "voice_direct_text"
    inventory = build_inventory(
        root=ROOT,
        profile="lan",
        capability=capability,
        claimed_level="HYBRID_VERIFIED",
    )
    environment_path = output_dir / "environment.json"
    environment_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    binding = f"{runtime['robot_id']}:{runtime['bot_id']}"
    matrix_results = (utils_matrix or {}).get("results") or []
    matrix_trace_ids = [
        str(item.get("trace_id"))
        for item in matrix_results
        if item.get("trace_id")
    ]
    trace_ids = [trace_id, *matrix_trace_ids]
    matrix_bindings = [
        f"runtime:{item['bot_id']}"
        for item in matrix_results
        if item.get("bot_id")
    ]
    bot_bindings = [binding, *matrix_bindings]
    observed_endpoints = inventory["endpoints"]
    artifact = str(raw_path.relative_to(ROOT))
    components = [
        "runtime_snapshot",
        "python_gateway",
        "router",
        "llm",
        "tts",
    ]
    if utils_route:
        components.insert(-1, "utils_mcp")
    evidence = []
    for component in components:
        component_trace_ids = (
            trace_ids
            if component in {"runtime_snapshot", "router", "llm", "utils_mcp"}
            else [trace_id]
        )
        component_bindings = (
            bot_bindings
            if component in {"runtime_snapshot", "router", "llm", "utils_mcp"}
            else [binding]
        )
        evidence.append({
            "id": component,
            "status": "PASS",
            "kind": "hybrid_integration",
            "component": component,
            "source": "artifact",
            "observed_at": _now(),
            "summary": f"{component} observed direct text Router/LLM/TTS chain",
            "trace_ids": component_trace_ids,
            "bot_bindings": component_bindings,
            "artifact": artifact,
            "sha256": _sha256(raw_path),
        })
    assertions = {
        "runtime_snapshot": True,
        "same_trace": True,
        "main_llm_response": True,
        "real_audio": True,
        "asr_bypassed_by_contract": True,
        # 只有 test_01 这一条 trace 真实经过 Gateway/TTS；Bot 矩阵仅覆盖
        # Runtime/Router/LLM/Utils，不得把它扩大声明为每个 Bot 的音频闭环。
        "trace_ids": [trace_id],
        "bot_bindings": [binding],
    }
    if utils_route:
        assertions.update(
            {
                "utils_tool_called_once": True,
                "utils_fact_fidelity": True,
                "all_enabled_bots_covered": True,
            }
        )
    else:
        assertions.update(
            {
                "router_classifier_used": True,
                "no_tool_call": True,
            }
        )
    report = {
        "schema_version": "ai-voice-evidence/v1",
        "objective": (
            "验证全部启用 Bot 的 Utils 路由与事实一致性，并验证 test_01 Gateway/TTS 音频闭环"
            if utils_route
            else "本地 Gateway/LLM/TTS 连接测试数据库和内网模型，验证 direct text Router 到音频闭环"
        ),
        "commit": inventory["commit"],
        "capability": capability,
        "claimed_level": "HYBRID_VERIFIED",
        "endpoint_scope": "external" if any(
            item["scope"] == "external" for item in observed_endpoints
        ) else "lan",
        "observed_endpoints": observed_endpoints,
        "authorization": {
            "connectivity": True,
            "external_write": False,
            "hardware_action": False,
            "reference": authorization_reference,
        },
        "assertions": assertions,
        "environment_artifact": {
            "artifact": str(environment_path.relative_to(ROOT)),
            "sha256": _sha256(environment_path),
        },
        "evidence": evidence,
        "unknowns": [
            "direct text 按协议绕过 ASR",
            "未经过 Go Gateway、Rust Client 或部署环境",
            "未验证声卡播放和实体硬件",
        ],
    }
    report_path = output_dir / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    problems = validate_report(
        report,
        load_manifest(ROOT),
        report_path=report_path,
        root=ROOT,
    )
    if problems:
        raise HarnessRunError("证据报告未通过 Harness：" + "; ".join(problems))
    return report_path


def _write_robot_voice_certified_report(
    *,
    output_dir: Path,
    raw_path: Path,
    runtime: dict[str, Any],
    traces: dict[str, str],
    authorization_reference: str,
) -> Path:
    from scripts.capture_evidence_environment import build_inventory
    from scripts.ai_preflight import load_manifest
    from scripts.validate_evidence_report import validate_report

    inventory = build_inventory(
        root=ROOT,
        profile="hybrid",
        capability="robot_voice_route",
        claimed_level="HYBRID_VERIFIED",
    )
    environment_path = output_dir / "environment.json"
    environment_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    binding = f"{runtime['robot_id']}:{runtime['bot_id']}"
    trace_ids = list(traces.values())
    artifact = str(raw_path.relative_to(ROOT))
    evidence = [
        {
            "id": component,
            "status": "PASS",
            "kind": "hybrid_integration",
            "component": component,
            "source": "artifact",
            "observed_at": _now(),
            "summary": f"{component} observed both test_01 Gateway robot actions",
            "trace_ids": trace_ids,
            "bot_bindings": [binding],
            "artifact": artifact,
            "sha256": _sha256(raw_path),
        }
        for component in (
            "runtime_snapshot",
            "python_gateway",
            "router",
            "llm",
            "robot_mcp",
            "mqtt_broker",
            "tts",
        )
    ]
    observed_endpoints = inventory["endpoints"]
    report = {
        "schema_version": "ai-voice-evidence/v1",
        "objective": "本地 Gateway/Router/LLM/Robot MCP/TTS 连接测试数据库和线上 MQTT，验证 test_01 两轮动作闭环",
        "commit": inventory["commit"],
        "capability": "robot_voice_route",
        "claimed_level": "HYBRID_VERIFIED",
        "endpoint_scope": "external" if any(
            item["scope"] == "external" for item in observed_endpoints
        ) else "lan",
        "observed_endpoints": observed_endpoints,
        "authorization": {
            "connectivity": True,
            "external_write": True,
            "hardware_action": False,
            "reference": authorization_reference,
        },
        "assertions": {
            "runtime_snapshot": True,
            "same_trace": True,
            "exact_robot_tool": True,
            "expected_action_types": True,
            "independent_subscriber_received_both": True,
            "real_audio": True,
            "asr_bypassed_by_contract": True,
            "trace_ids": trace_ids,
            "bot_bindings": [binding],
        },
        "environment_artifact": {
            "artifact": str(environment_path.relative_to(ROOT)),
            "sha256": _sha256(environment_path),
        },
        "evidence": evidence,
        "unknowns": [
            "direct text 按协议绕过 ASR",
            "未经过 Go Gateway、Rust Client 或部署环境",
            "Broker 独立消费成功不等于 Robot 业务消费者 ACK 或实体动作完成",
        ],
    }
    report_path = output_dir / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    problems = validate_report(
        report,
        load_manifest(ROOT),
        report_path=report_path,
        root=ROOT,
    )
    if problems:
        raise HarnessRunError("证据报告未通过 Harness：" + "; ".join(problems))
    return report_path


def _write_audio_certified_report(
    *,
    output_dir: Path,
    raw_path: Path,
    runtime: dict[str, Any],
    trace_id: str,
    authorization_reference: str,
) -> Path:
    from scripts.capture_evidence_environment import build_inventory
    from scripts.ai_preflight import load_manifest
    from scripts.validate_evidence_report import validate_report

    inventory = build_inventory(
        root=ROOT,
        profile="lan",
        capability="voice_python_audio",
        claimed_level="HYBRID_VERIFIED",
    )
    environment_path = output_dir / "environment.json"
    environment_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    binding = f"{runtime['robot_id']}:{runtime['bot_id']}"
    artifact = str(raw_path.relative_to(ROOT))
    evidence = [
        {
            "id": component,
            "status": "PASS",
            "kind": "hybrid_integration",
            "component": component,
            "source": "artifact",
            "observed_at": _now(),
            "summary": f"{component} observed tracked audio input on the same trace",
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
            "artifact": artifact,
            "sha256": _sha256(raw_path),
        }
        for component in (
            "runtime_snapshot",
            "python_gateway",
            "stt",
            "router",
            "llm",
            "tts",
        )
    ]
    observed_endpoints = inventory["endpoints"]
    report = {
        "schema_version": "ai-voice-evidence/v1",
        "objective": "使用哈希绑定的仓库固定录音，验证本地 Python Gateway/STT/Router/LLM/TTS 音频闭环",
        "commit": inventory["commit"],
        "capability": "voice_python_audio",
        "claimed_level": "HYBRID_VERIFIED",
        "endpoint_scope": "external" if any(
            item["scope"] == "external" for item in observed_endpoints
        ) else "lan",
        "observed_endpoints": observed_endpoints,
        "authorization": {
            "connectivity": True,
            "external_write": False,
            "hardware_action": False,
            "reference": authorization_reference,
        },
        "assertions": {
            "runtime_snapshot": True,
            "same_trace": True,
            "tracked_audio_fixture": True,
            "asr_fact_fidelity": True,
            "main_llm_response": True,
            "real_audio": True,
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
        },
        "environment_artifact": {
            "artifact": str(environment_path.relative_to(ROOT)),
            "sha256": _sha256(environment_path),
        },
        "evidence": evidence,
        "unknowns": [
            "输入是仓库固定参考录音，不证明现场麦克风、噪声或远场声学质量",
            "未经过 Go Gateway、Rust Client 或部署环境",
            "未验证声卡播放和实体硬件",
        ],
    }
    report_path = output_dir / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    problems = validate_report(
        report,
        load_manifest(ROOT),
        report_path=report_path,
        root=ROOT,
    )
    if problems:
        raise HarnessRunError("证据报告未通过 Harness：" + "; ".join(problems))
    return report_path


def _write_m1_e2e_certified_report(
    *,
    output_dir: Path,
    raw_path: Path,
    runtime: dict[str, Any],
    trace_id: str,
    authorization_reference: str,
) -> Path:
    from scripts.capture_evidence_environment import build_inventory
    from scripts.ai_preflight import load_manifest
    from scripts.validate_evidence_report import validate_report

    inventory = build_inventory(
        root=ROOT,
        profile="lan",
        capability="voice_e2e",
        claimed_level="HYBRID_VERIFIED",
    )
    environment_path = output_dir / "environment.json"
    environment_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    binding = f"{runtime['robot_id']}:{runtime['bot_id']}"
    artifact = str(raw_path.relative_to(ROOT))
    evidence = [
        {
            "id": component,
            "status": "PASS",
            "kind": "hybrid_integration",
            "component": component,
            "source": "artifact",
            "observed_at": _now(),
            "summary": f"{component} observed the fixed-audio M1 WebRTC round on the same trace",
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
            "artifact": artifact,
            "sha256": _sha256(raw_path),
        }
        for component in (
            "runtime_snapshot",
            "rust_client",
            "go_gateway",
            "python_gateway",
            "stt",
            "router",
            "llm",
            "tts",
        )
    ]
    observed_endpoints = inventory["endpoints"]
    report = {
        "schema_version": "ai-voice-evidence/v1",
        "objective": "验证当前代码的 Rust Client -> Go Gateway -> Python Gateway -> STT/Router/LLM/TTS M1 WebRTC 闭环",
        "commit": inventory["commit"],
        "capability": "voice_e2e",
        "claimed_level": "HYBRID_VERIFIED",
        "endpoint_scope": "external" if any(
            item["scope"] == "external" for item in observed_endpoints
        ) else "lan",
        "observed_endpoints": observed_endpoints,
        "authorization": {
            "connectivity": True,
            "external_write": False,
            "hardware_action": False,
            "reference": authorization_reference,
        },
        "assertions": {
            "runtime_snapshot": True,
            "same_trace": True,
            "tracked_audio_fixture": True,
            "native_webrtc_rtp": True,
            "real_audio": True,
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
        },
        "environment_artifact": {
            "artifact": str(environment_path.relative_to(ROOT)),
            "sha256": _sha256(environment_path),
        },
        "evidence": evidence,
        "unknowns": [
            "输入是仓库固定录音，不证明现场麦克风、远场声学或硬件采集",
            "本地启动 Rust/Go/Python 服务，不代表已部署环境",
            "下行 RTP 已由 Rust 接收和解码，但未验证真实扬声器播放",
        ],
    }
    report_path = output_dir / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    problems = validate_report(
        report,
        load_manifest(ROOT),
        report_path=report_path,
        root=ROOT,
    )
    if problems:
        raise HarnessRunError("证据报告未通过 Harness：" + "; ".join(problems))
    return report_path


def _write_vision_certified_report(
    *,
    output_dir: Path,
    raw_path: Path,
    runtime: dict[str, Any],
    trace_id: str,
    authorization_reference: str,
) -> Path:
    from scripts.capture_evidence_environment import build_inventory
    from scripts.ai_preflight import load_manifest
    from scripts.validate_evidence_report import validate_report

    inventory = build_inventory(
        root=ROOT,
        profile="lan",
        capability="vision",
        claimed_level="HYBRID_VERIFIED",
    )
    environment_path = output_dir / "environment.json"
    environment_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    binding = f"{runtime['robot_id']}:{runtime['bot_id']}"
    artifact = str(raw_path.relative_to(ROOT))
    components = (
        "runtime_snapshot",
        "image_source",
        "rust_client",
        "go_gateway",
        "python_gateway",
        "llm",
        "tts",
    )
    evidence = [
        {
            "id": component,
            "status": "PASS",
            "kind": "hybrid_integration",
            "component": component,
            "source": "artifact",
            "observed_at": _now(),
            "summary": f"{component} observed the owner-approved image on the same Vision trace",
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
            "artifact": artifact,
            "sha256": _sha256(raw_path),
        }
        for component in components
    ]
    observed_endpoints = inventory["endpoints"]
    report = {
        "schema_version": "ai-voice-evidence/v1",
        "objective": "验证 Gold 图片经 Rust vision-v1、Go 快照缓存和 Python/LLM 取图路径生成忠实回答",
        "commit": inventory["commit"],
        "capability": "vision",
        "claimed_level": "HYBRID_VERIFIED",
        "endpoint_scope": "external" if any(
            item["scope"] == "external" for item in observed_endpoints
        ) else "lan",
        "observed_endpoints": observed_endpoints,
        "authorization": {
            "connectivity": True,
            "external_write": False,
            "hardware_action": False,
            "reference": authorization_reference,
        },
        "assertions": {
            "runtime_snapshot": True,
            "same_trace": True,
            "real_image": True,
            "programmatic_image_path": True,
            "visual_fact_fidelity": True,
            "trace_ids": [trace_id],
            "bot_bindings": [binding],
        },
        "environment_artifact": {
            "artifact": str(environment_path.relative_to(ROOT)),
            "sha256": _sha256(environment_path),
        },
        "evidence": evidence,
        "unknowns": [
            "使用固定 Gold 图片，不证明实体摄像头采集",
            "本地启动 Rust/Go/Python，不代表部署环境",
            "未验证连续视频帧、过期帧竞争或硬件摄像头稳定性",
        ],
    }
    report_path = output_dir / "evidence-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    problems = validate_report(
        report, load_manifest(ROOT), report_path=report_path, root=ROOT
    )
    if problems:
        raise HarnessRunError("证据报告未通过 Harness：" + "; ".join(problems))
    return report_path


def _run_voice_m1_e2e(args: argparse.Namespace) -> int:
    vision_route = args.scenario == "voice-vision"
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    output_dir = _safe_output_dir(
        args.output_dir or Path("reports") / f"dev-harness-{args.scenario}-{run_id}"
    )
    log_dir = output_dir / "logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    commit, dirty = _git_state()
    if args.certify and dirty:
        print("Harness refused: --certify 要求 clean worktree；开发诊断请移除 --certify")
        return 2
    try:
        _run_preflight(log_dir, profile="lan")
        runtime = _load_voice_runtime_target(args.robot_id, args.bot_id)
        ports = (5001, 5003, 5004, args.stt_port, args.llm_port, args.tts_port, args.gateway_port, args.go_port)
        for port in ports:
            if not _port_is_free(port):
                raise HarnessRunError(f"隔离端口已被占用: {port}；Runner 不复用也不终止来源不明的进程")

        wav = _prepare_voice_wav_fixture(output_dir / "fixed-input-16k.wav")
        if vision_route:
            if not VISION_FIXTURE.is_file():
                raise HarnessRunError(f"Vision Gold 图片不存在: {VISION_FIXTURE}")
            actual_vision_sha = _sha256(VISION_FIXTURE)
            if actual_vision_sha != VISION_FIXTURE_SHA256:
                raise HarnessRunError(
                    f"Vision Gold 图片 SHA256 不匹配: {actual_vision_sha}"
                )
        env = _load_voice_private_credentials(os.environ.copy())
        env["GATEWAY_REQUIRE_ROBOT_SECRET"] = "true"
        _resolve_voice_robot_secret(env, runtime["robot_id"])
        direct_hosts = _proxy_bypass_hosts(env)
        for key in ("NO_PROXY", "no_proxy"):
            direct_hosts.update(filter(None, env.get(key, "").split(",")))
            env[key] = ",".join(sorted(direct_hosts))
        env.update(
            {
                "STT_GRPC_SERVER_PORT": str(args.stt_port),
                "LLM_GRPC_SERVER_PORT": str(args.llm_port),
                "TTS_GRPC_SERVER_PORT": str(args.tts_port),
                "GATEWAY_BIND_HOST": "127.0.0.1",
                "GATEWAY_BIND_PORT": str(args.gateway_port),
                "STT_SERVICE_URL": f"grpc://127.0.0.1:{args.stt_port}",
                "LLM_SERVICE_URL": f"grpc://127.0.0.1:{args.llm_port}",
                "TTS_SERVICE_URL": f"grpc://127.0.0.1:{args.tts_port}",
            }
        )
        env.update(_m1_internal_voice_env(args.gateway_port))
        if vision_route:
            vision_token = env.get("LLM_VISION_GATEWAY_TOKEN", "").strip()
            if not vision_token:
                raise HarnessRunError("私有环境缺少 LLM_VISION_GATEWAY_TOKEN")
            env.update(
                {
                    "GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN": vision_token,
                    "LLM_VISION_GATEWAY_BASE_URL": f"http://127.0.0.1:{args.go_port}",
                    "VISION_SNAPSHOT_ENABLED": "true",
                    "VISION_SNAPSHOT_PATH": str(output_dir / "vision-snapshot.jpg"),
                    "VISION_SNAPSHOT_SCAN_INTERVAL_MS": "100",
                    "RUST_LIVE_SMOKE_VISION_FIXTURE": str(VISION_FIXTURE),
                    "RUST_LIVE_SMOKE_VISION_SETTLE_MS": "1800",
                    "RUST_LIVE_SMOKE_DIRECT_TEXT": VISION_PROMPT,
                }
            )
        command = [
            sys.executable,
            "scripts/smoke_go_webrtc_python_gateway.py",
            "--sample",
            str(output_dir / "fixed-input-16k.wav"),
            "--client",
            "rust",
            "--robot-id",
            runtime["robot_id"],
            "--bot-id",
            runtime["bot_id"],
            "--go-addr",
            f"127.0.0.1:{args.go_port}",
            "--gateway-ws-url",
            f"ws://127.0.0.1:{args.gateway_port}/ws",
            "--require-robot-secret",
            "--log-dir",
            str(log_dir),
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=max(180.0, args.timeout * 4),
            check=False,
        )
        runner_log = log_dir / "runner.log"
        runner_log.write_text(result.stdout, encoding="utf-8")
        if result.returncode != 0:
            raise HarnessRunError(
                f"{args.scenario} WebRTC smoke 未通过 exit_code={result.returncode}，"
                "详见 logs/runner.log"
            )

        stability_match = re.search(r"^stability_report=(\{.*\})$", result.stdout, re.MULTILINE)
        if not stability_match:
            raise HarnessRunError("Rust smoke 未输出 stability_report")
        stability = json.loads(stability_match.group(1))
        reports = stability.get("reports") or []
        if len(reports) != 1 or stability.get("tts_audio_rounds") != 1:
            raise HarnessRunError("Rust smoke 未完成单轮有效 TTS")
        if not vision_route and stability.get("valid_asr_rounds") != 1:
            raise HarnessRunError("Rust smoke 未完成单轮有效 ASR")
        round_report = reports[0]
        natural_barge_in_route = bool(
            not vision_route
            and str(env.get("RUST_LIVE_SMOKE_BARGE_IN_AFTER_PLAYBACK_MS", "")).strip()
        )
        if natural_barge_in_route:
            if round_report.get("barge_in_decision") != "new_intent":
                raise HarnessRunError("自然打断 smoke 未得到 new_intent")
            if round_report.get("playback_cancel_ms") is None:
                raise HarnessRunError("自然打断 smoke 未收到旧 playback_cancel")
            if round_report.get("replacement_playback_start_ms") is None:
                raise HarnessRunError("自然打断 smoke 未收到替换 playback_start")
            if int(round_report.get("replacement_audio_frame_count") or 0) <= 0:
                raise HarnessRunError("自然打断 smoke 未收到替换 RTP 音频")
            if round_report.get("done_ms") is None:
                raise HarnessRunError("自然打断 smoke 未收到替换轮次 done")
            barge_in_asr_text = str(round_report.get("barge_in_asr_text") or "").strip()
            if not barge_in_asr_text:
                raise HarnessRunError("自然打断 smoke 缺少候选 ASR 文本")
            expected_fragments = [
                item.strip()
                for item in str(
                    env.get("RUST_LIVE_SMOKE_BARGE_IN_EXPECTED_FRAGMENTS", "")
                ).split("|")
                if item.strip()
            ]
            if expected_fragments and not any(
                fragment in barge_in_asr_text for fragment in expected_fragments
            ):
                raise HarnessRunError(
                    "自然打断候选 ASR 缺少预期语义片段: "
                    f"expected={expected_fragments}, actual={barge_in_asr_text}"
                )
        utterance_id = str(round_report["utterance_id"])
        trace_id = f"{'vision' if vision_route else 'audio'}-{utterance_id}"
        if vision_route:
            required_log_markers = {
                "rust_client.log": [
                    "transport_policy=webrtc_only rtc_audio_uplink=rtp_only",
                    "vision_fixture_injected=",
                    "direct_text_sent trace_id=",
                    "audio_frame_count=",
                ],
                "go_gateway.log": [trace_id, "vision_snapshot_accepted", "internal_voice_session_opened"],
                "python_gateway.log": [trace_id, "LLM 完成", "收到首个 TTS 音频块"],
                "llm.log": [f"trace_id={trace_id}", "视觉上下文已附加", "router=chat/vision_context"],
                "tts.log": [f"trace_id={trace_id}", "TTS 首个 Local Qwen3 PCM", "TTS 节奏统计"],
            }
        else:
            required_log_markers = {
                "rust_client.log": ["transport_policy=webrtc_only rtc_audio_uplink=rtp_only", "audio_uplink_ready_ms=", "audio_sent utterance_id=", "json_type=response.asr valid=true", "audio_frame_count="],
                "go_gateway.log": [trace_id, "internal_voice_session_opened", '"msg":"rtp_track_closed"', '"seq_gaps":0'],
                "python_gateway.log": [trace_id, "Internal voice input_audio accepted", "ASR 识别完成", "LLM 内部指标 router=chat/llm_router_async", "收到首个 TTS 音频块"],
                "stt.log": ["收到语音识别请求", "Qwen ASR 完成", "STT 推理完成"],
                "llm.log": [f"trace_id={trace_id}", "source=llm_router_async", "LLM 请求指标"],
                "tts.log": [f"trace_id={trace_id}", "TTS 首个 Local Qwen3 PCM", "TTS 节奏统计"],
            }
            if natural_barge_in_route:
                required_log_markers["python_gateway.log"] = [
                    marker
                    for marker in required_log_markers["python_gateway.log"]
                    if marker != "LLM 内部指标 router=chat/llm_router_async"
                ]
                required_log_markers["python_gateway.log"].append("LLM 内部指标 router=")
                required_log_markers["go_gateway.log"] = [
                    marker
                    for marker in required_log_markers["go_gateway.log"]
                    if marker != '"msg":"rtp_track_closed"'
                ]
                required_log_markers["go_gateway.log"].append(
                    "downlink_rtp_first_packet_sent"
                )
                required_log_markers["rust_client.log"].extend(
                    ["barge_in_probe_sent", "barge_in_decision=new_intent", "json_type=playback_cancel"]
                )
                if env.get("RUST_LIVE_SMOKE_BARGE_IN_TEXT", "").strip():
                    required_log_markers["rust_client.log"].append("barge_in_sample=")
                required_log_markers["go_gateway.log"].extend(
                    ["barge_in_commit_ack", "internal_voice_interrupt_ack", "downlink_rtp_resumed"]
                )
                required_log_markers["python_gateway.log"].append("Barge-in Probe 完成")
        marker_counts: dict[str, dict[str, int]] = {}
        for filename, markers in required_log_markers.items():
            content = (log_dir / filename).read_text(encoding="utf-8", errors="replace")
            marker_counts[filename] = {marker: content.count(marker) for marker in markers}
            missing = [marker for marker, count in marker_counts[filename].items() if count == 0]
            if missing:
                raise HarnessRunError(f"{filename} 缺少链路证据标记: {missing}")
        go_log = (log_dir / "go_gateway.log").read_text(encoding="utf-8", errors="replace")
        if "python_gateway_bridge_start" in go_log:
            raise HarnessRunError("Go Gateway 降级到 M0 Python bridge，拒绝作为 M1 证据")

        barge_in_fixture = None
        barge_in_manifest_path = output_dir / "barge-in-input-16k.json"
        if env.get("RUST_LIVE_SMOKE_BARGE_IN_TEXT", "").strip():
            if not barge_in_manifest_path.is_file():
                raise HarnessRunError("TTS-Base 模拟用户语音缺少生成清单")
            barge_in_fixture = json.loads(
                barge_in_manifest_path.read_text(encoding="utf-8")
            )
            generated_wav_path = output_dir / "barge-in-input-16k.wav"
            if not generated_wav_path.is_file():
                raise HarnessRunError("TTS-Base 模拟用户 WAV 不存在")
            if barge_in_fixture.get("wav_sha256") != _sha256(generated_wav_path):
                raise HarnessRunError("TTS-Base 模拟用户 WAV SHA-256 不匹配")
            if barge_in_fixture.get("text") != env["RUST_LIVE_SMOKE_BARGE_IN_TEXT"].strip():
                raise HarnessRunError("TTS-Base 模拟用户文本与生成清单不一致")

        visual_response = None
        missing_fact_groups: list[list[str]] = []
        if vision_route:
            python_log = (log_dir / "python_gateway.log").read_text(
                encoding="utf-8", errors="replace"
            )
            response_matches = re.findall(
                r"LLM 完成 \(\d+字\) - (.+)$", python_log, re.MULTILINE
            )
            if not response_matches:
                raise HarnessRunError("Python Gateway 日志中未找到 Vision 最终回答")
            visual_response = response_matches[-1]
            missing_fact_groups = [
                list(group)
                for group in VISION_REQUIRED_FACT_GROUPS
                if not any(term in visual_response for term in group)
            ]
            if missing_fact_groups:
                raise HarnessRunError(
                    f"Vision 回答缺少 Gold 可见事实组: {missing_fact_groups}"
                )

        raw = {
            "schema_version": "ai-voice-dev-harness-observation/v1",
            "classification": "HYBRID_DIAGNOSTIC" if dirty else "HYBRID_CANDIDATE",
            "observed_at": _now(),
            "commit": commit,
            "dirty": dirty,
            "scenario": args.scenario,
            "runtime_snapshot": runtime,
            "fixture": (
                {
                    "path": str(VISION_FIXTURE.relative_to(ROOT)),
                    "sha256": VISION_FIXTURE_SHA256,
                    "prompt": VISION_PROMPT,
                }
                if vision_route
                else wav
            ),
            "trace_id": trace_id,
            "stability_report": stability,
            "barge_in_fixture": barge_in_fixture,
            "log_marker_counts": marker_counts,
            "visual_response": visual_response,
            "missing_visual_fact_groups": missing_fact_groups,
            "assertions": (
                {
                    "target_is_allowlisted": True,
                    "runtime_snapshot": True,
                    "same_trace": True,
                    "real_image": True,
                    "programmatic_image_path": True,
                    "native_webrtc_data_channel": True,
                    "vision_snapshot_accepted": True,
                    "vision_context_attached": True,
                    "visual_fact_fidelity": not missing_fact_groups,
                    "tts_rtp_received": True,
                    "stale_audio_frames_zero": round_report.get("stale_audio_frame_count") == 0,
                }
                if vision_route
                else {
                    "target_is_allowlisted": True,
                    "runtime_snapshot": True,
                    "same_trace": True,
                    "tracked_audio_fixture": True,
                    "native_webrtc_rtp": True,
                    "real_audio": True,
                    "asr_valid": True,
                    "router_and_llm_observed": True,
                    "tts_rtp_received": True,
                    "stale_audio_frames_zero": round_report.get("stale_audio_frame_count") == 0,
                    **(
                        {
                            "barge_in_new_intent": round_report.get("barge_in_decision") == "new_intent",
                            "barge_in_asr_text_observed": bool(
                                str(round_report.get("barge_in_asr_text") or "").strip()
                            ),
                            "tts_base_user_fixture_bound": (
                                barge_in_fixture is not None
                                if env.get("RUST_LIVE_SMOKE_BARGE_IN_TEXT", "").strip()
                                else True
                            ),
                            "old_playback_cancelled": round_report.get("playback_cancel_ms") is not None,
                            "replacement_playback_started": round_report.get("replacement_playback_start_ms") is not None,
                            "replacement_rtp_received": int(round_report.get("replacement_audio_frame_count") or 0) > 0,
                            "replacement_round_done": round_report.get("done_ms") is not None,
                        }
                        if natural_barge_in_route
                        else {}
                    ),
                }
            ),
            "limitations": (
                [
                    "使用固定 Gold 图片，不证明实体摄像头采集",
                    "本地启动 Rust/Go/Python，不是部署环境验证",
                    "未验证连续视频帧、过期帧竞争或硬件摄像头稳定性",
                ]
                if vision_route
                else [
                    "固定录音，不是现场麦克风或声学测试",
                    "本地启动 Rust/Go/Python，不是部署环境验证",
                    "未验证扬声器与实体硬件",
                ]
            ),
        }
        raw_path = output_dir / "observation.json"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        label = "Vision" if vision_route else "M1 Voice E2E"
        print(f"Hybrid {label} passed: {raw_path.relative_to(ROOT)}")
        if args.certify:
            writer = (
                _write_vision_certified_report
                if vision_route
                else _write_m1_e2e_certified_report
            )
            report_path = writer(
                output_dir=output_dir,
                raw_path=raw_path,
                runtime=runtime,
                trace_id=trace_id,
                authorization_reference=args.authorization_reference.strip(),
            )
            print(f"Evidence report valid: {report_path.relative_to(ROOT)}")
        return 0
    except (HarnessRunError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Harness failed: {exc}")
        return 1


def _run_voice_client_event(args: argparse.Namespace) -> int:
    from scripts import probe_python_gateway_internal_voice as voice_probe

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    output_dir = _safe_output_dir(
        args.output_dir or Path("reports") / f"dev-harness-{args.scenario}-{run_id}"
    )
    log_dir = output_dir / "logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    commit, dirty = _git_state()
    if args.certify and dirty:
        print("Harness refused: --certify 要求 clean worktree；开发诊断请移除 --certify")
        return 2

    processes: list[tuple[str, subprocess.Popen[Any]]] = []
    try:
        _run_preflight(log_dir, profile="local")
        runtime = _load_voice_runtime_target(args.robot_id, args.bot_id)
        for port in (args.tts_port, args.tts_admin_port, args.gateway_port):
            if not _port_is_free(port):
                raise HarnessRunError(
                    f"隔离端口已被占用: {port}；Runner 不复用也不终止来源不明的进程"
                )

        env = _load_voice_private_credentials(os.environ.copy())
        robot_secret = _resolve_voice_robot_secret(env, runtime["robot_id"])
        direct_hosts = _proxy_bypass_hosts(env)
        for key in ("NO_PROXY", "no_proxy"):
            direct_hosts.update(filter(None, env.get(key, "").split(",")))
            env[key] = ",".join(sorted(direct_hosts))
        env["TTS_GRPC_SERVER_PORT"] = str(args.tts_port)
        env["TTS_ADMIN_PORT"] = str(args.tts_admin_port)
        env["GATEWAY_BIND_HOST"] = "127.0.0.1"
        env["GATEWAY_BIND_PORT"] = str(args.gateway_port)
        env["TTS_SERVICE_URL"] = f"grpc://127.0.0.1:{args.tts_port}"
        opus_lib_dir = Path("/opt/homebrew/lib")
        if opus_lib_dir.exists():
            current_dyld = env.get("DYLD_LIBRARY_PATH", "")
            env["DYLD_LIBRARY_PATH"] = os.pathsep.join(
                part for part in (str(opus_lib_dir), current_dyld) if part
            )

        tts_proc = _start_process(
            "tts-grpc",
            [sys.executable, "tts/tts_grpc_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("tts-grpc", tts_proc))
        _wait_port(args.tts_port, tts_proc, args.timeout)

        gateway_proc = _start_process(
            "python-gateway",
            [sys.executable, "gateway/gateway_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("python-gateway", gateway_proc))
        base_url = f"http://127.0.0.1:{args.gateway_port}"
        _wait_http(f"{base_url}/healthz", gateway_proc, args.timeout)

        status_url, ws_url = voice_probe.derive_urls(base_url)
        status = voice_probe.get_status(status_url, args.timeout)
        if not status.ok:
            raise HarnessRunError("Python Gateway /internal/status 未通过")
        event = "wake_idle"
        probe = voice_probe.probe_internal_voice(
            ws_url,
            robot_id=runtime["robot_id"],
            robot_secret=robot_secret,
            client_type="codex_hybrid_harness",
            bot_id=runtime["bot_id"],
            timeout=args.timeout,
            client_event=event,
        )
        if not probe.ok:
            raise HarnessRunError("active client_event probe 未通过，详见 Gateway/TTS 日志")
        verified = _verify_voice_client_event_probe(
            detail=probe.detail,
            expected_event=event,
        )
        raw = {
            "schema_version": "ai-voice-dev-harness-observation/v1",
            "classification": "HYBRID_DIAGNOSTIC" if dirty else "HYBRID_CANDIDATE",
            "observed_at": _now(),
            "commit": commit,
            "dirty": dirty,
            "scenario": args.scenario,
            "runtime_snapshot": runtime,
            "gateway_status": voice_probe.summarize_status(status.detail),
            "client_event": verified,
            "assertions": {
                "target_is_allowlisted": True,
                "runtime_snapshot": True,
                "active_client_event": True,
                "real_audio": True,
                "same_trace": True,
                "llm_bypassed_by_contract": True,
            },
            "limitations": [
                "client_event 按协议绕过 ASR、LLM、Router 和会话历史",
                "未经过 Go Gateway、Rust Client 或部署环境",
                "未验证声卡播放和实体硬件",
            ],
        }
        raw_path = output_dir / "observation.json"
        raw_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Hybrid scenario passed: {raw_path.relative_to(ROOT)}")
        if args.certify:
            report_path = _write_voice_certified_report(
                output_dir=output_dir,
                raw_path=raw_path,
                runtime=runtime,
                trace_id=verified["trace_id"],
                authorization_reference=args.authorization_reference.strip(),
            )
            print(f"Evidence report valid: {report_path.relative_to(ROOT)}")
        return 0
    except (HarnessRunError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Harness failed: {exc}")
        return 1
    finally:
        _stop_processes(processes)


def _run_voice_direct_text(args: argparse.Namespace) -> int:
    import requests
    from scripts import probe_python_gateway_internal_voice as voice_probe
    from scripts import probe_python_gateway_ws_audio as gateway_probe

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    output_dir = _safe_output_dir(
        args.output_dir or Path("reports") / f"dev-harness-{args.scenario}-{run_id}"
    )
    log_dir = output_dir / "logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    commit, dirty = _git_state()
    if args.certify and dirty:
        print("Harness refused: --certify 要求 clean worktree；开发诊断请移除 --certify")
        return 2

    processes: list[tuple[str, subprocess.Popen[Any]]] = []
    capture: MqttCapture | None = None
    try:
        utils_route = args.scenario == "voice-utils-time"
        robot_route = args.scenario == "voice-robot-actions"
        audio_route = args.scenario == "voice-audio"
        agent_route = args.scenario == "voice-agent-continuity"
        _run_preflight(log_dir, profile="hybrid" if robot_route else "lan")
        runtime = _load_runtime_target(args.robot_id, args.bot_id)
        voice_runtime = _load_voice_runtime_target(args.robot_id, args.bot_id)
        runtime.update(
            {
                "tts_profile_id": voice_runtime["tts_profile_id"],
                "tts_provider_type": voice_runtime["tts_provider_type"],
            }
        )
        if utils_route and not runtime.get("utils_mcp_port"):
            raise HarnessRunError(f"Bot 未绑定可用 utils_remote: {runtime['bot_id']}")
        ports = [
            runtime["mcp_port"],
            args.llm_port,
            args.llm_admin_port,
            args.tts_port,
            args.tts_admin_port,
            args.gateway_port,
        ]
        if audio_route:
            ports.extend((args.stt_port, args.stt_admin_port))
        if utils_route and runtime.get("utils_mcp_port"):
            ports.append(runtime["utils_mcp_port"])
        for port in ports:
            if not _port_is_free(port):
                raise HarnessRunError(
                    f"隔离端口已被占用: {port}；Runner 不复用也不终止来源不明的进程"
                )

        env = _load_voice_private_credentials(os.environ.copy())
        robot_secret = _resolve_voice_robot_secret(env, runtime["robot_id"])
        direct_hosts = _proxy_bypass_hosts(env)
        for key in ("NO_PROXY", "no_proxy"):
            direct_hosts.update(filter(None, env.get(key, "").split(",")))
            env[key] = ",".join(sorted(direct_hosts))
        env["LLM_GRPC_SERVER_PORT"] = str(args.llm_port)
        env["LLM_ADMIN_PORT"] = str(args.llm_admin_port)
        env["STT_GRPC_SERVER_PORT"] = str(args.stt_port)
        env["STT_ADMIN_PORT"] = str(args.stt_admin_port)
        env["TTS_GRPC_SERVER_PORT"] = str(args.tts_port)
        env["TTS_ADMIN_PORT"] = str(args.tts_admin_port)
        env["GATEWAY_BIND_HOST"] = "127.0.0.1"
        env["GATEWAY_BIND_PORT"] = str(args.gateway_port)
        env["LLM_SERVICE_URL"] = f"grpc://127.0.0.1:{args.llm_port}"
        env["STT_SERVICE_URL"] = f"grpc://127.0.0.1:{args.stt_port}"
        env["TTS_SERVICE_URL"] = f"grpc://127.0.0.1:{args.tts_port}"
        opus_lib_dir = Path("/opt/homebrew/lib")
        if opus_lib_dir.exists():
            current_dyld = env.get("DYLD_LIBRARY_PATH", "")
            env["DYLD_LIBRARY_PATH"] = os.pathsep.join(
                part for part in (str(opus_lib_dir), current_dyld) if part
            )

        mcp_proc = _start_process(
            "robot-mcp",
            [sys.executable, "mcp_servers/robot_sse_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("robot-mcp", mcp_proc))
        _wait_http(
            f"http://127.0.0.1:{runtime['mcp_port']}/health",
            mcp_proc,
            min(args.timeout, 30),
        )

        if utils_route and runtime.get("utils_mcp_port"):
            utils_proc = _start_process(
                "utils-mcp",
                [sys.executable, "mcp_servers/utils_sse_server.py"],
                env=env,
                log_dir=log_dir,
            )
            processes.append(("utils-mcp", utils_proc))
            _wait_http(
                f"http://127.0.0.1:{runtime['utils_mcp_port']}/health",
                utils_proc,
                min(args.timeout, 30),
            )

        if audio_route:
            stt_proc = _start_process(
                "stt-grpc",
                [sys.executable, "stt/stt_grpc_server.py"],
                env=env,
                log_dir=log_dir,
            )
            processes.append(("stt-grpc", stt_proc))
            _wait_port(args.stt_port, stt_proc, args.timeout)

        llm_proc = _start_process(
            "llm-grpc",
            [sys.executable, "llm/llm_grpc_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("llm-grpc", llm_proc))
        _wait_port(args.llm_port, llm_proc, args.timeout)

        utils_matrix = None
        if utils_route:
            from scripts.eval_bot_utils_matrix import run_matrix

            utils_matrix = run_matrix(args.llm_port, args.timeout)
            (output_dir / "utils-matrix.json").write_text(
                json.dumps(utils_matrix, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if (
                utils_matrix.get("passed") != utils_matrix.get("total")
                or not utils_matrix.get("shared_route_policy")
            ):
                raise HarnessRunError(
                    "全部启用 Bot 的 Utils 路由/事实矩阵未通过"
                )

        tts_proc = _start_process(
            "tts-grpc",
            [sys.executable, "tts/tts_grpc_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("tts-grpc", tts_proc))
        _wait_port(args.tts_port, tts_proc, args.timeout)

        gateway_proc = _start_process(
            "python-gateway",
            [sys.executable, "gateway/gateway_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("python-gateway", gateway_proc))
        base_url = f"http://127.0.0.1:{args.gateway_port}"
        _wait_http(f"{base_url}/healthz", gateway_proc, args.timeout)

        if agent_route:
            probe = voice_probe.probe_input_text_sequence(
                f"ws://127.0.0.1:{args.gateway_port}/internal/voice/ws",
                robot_id=runtime["robot_id"],
                robot_secret=robot_secret,
                client_type="codex_hybrid_harness",
                bot_id=runtime["bot_id"],
                texts=[AGENT_ENTRY_PROMPT, AGENT_CONFIRM_PROMPT],
                timeout=args.timeout,
            )
            if not probe.ok:
                raise HarnessRunError("M1 Agent continuity probe 未通过")
            verified = _verify_agent_continuity_probe(probe.detail)
            llm_log = (log_dir / "llm-grpc.log").read_text(
                encoding="utf-8", errors="replace"
            )
            gateway_log = (log_dir / "python-gateway.log").read_text(
                encoding="utf-8", errors="replace"
            )
            session_id = verified["session_id"]
            entered_marker = f"会话 {session_id} 进入 Agent: cognitive_screening_agent"
            continued_markers = (
                f"会话 {session_id} 由 Agent 流式接管: agent=cognitive_screening_agent",
                f"会话 {session_id} 由 Agent 接管: agent=cognitive_screening_agent",
            )
            if entered_marker not in llm_log:
                raise HarnessRunError("主 LLM 未记录当前 M1 session 进入认知 Agent")
            if not any(marker in llm_log for marker in continued_markers):
                raise HarnessRunError("第二轮未由同一 M1 session 的认知 Agent 接管")
            if 'WebSocket /ws" [accepted]' in gateway_log:
                raise HarnessRunError("Agent continuity 期间出现了 M0 /ws 会话")
            raw = {
                "schema_version": "ai-voice-dev-harness-observation/v1",
                "classification": "HYBRID_DIAGNOSTIC",
                "observed_at": _now(),
                "commit": commit,
                "dirty": dirty,
                "scenario": args.scenario,
                "runtime_snapshot": runtime,
                "agent_continuity": verified,
                "assertions": {
                    "target_is_allowlisted": True,
                    "runtime_snapshot": True,
                    "same_m1_session": True,
                    "input_text_commit_twice": True,
                    "cognitive_agent_entered": True,
                    "cognitive_agent_continued": True,
                    "no_m0_session_fork": True,
                    "real_llm": True,
                    "real_tts_audio": True,
                    "asr_reused_without_second_call": True,
                },
                "limitations": [
                    "使用 typed 候选文本模拟 Turn Gate 提升，不经过现场 ASR/VAD",
                    "直接验证 Python M1/LLM/TTS，不经过 Go/Rust 或部署环境",
                    "未验证声卡播放和实体硬件",
                ],
            }
            raw_path = output_dir / "observation.json"
            raw_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Hybrid Agent continuity passed: {raw_path.relative_to(ROOT)}")
            return 0

        if audio_route:
            fixture = _prepare_voice_audio_fixture()
            trace_id = f"dev-harness-{run_id}-audio"
            probe = voice_probe.probe_active_audio(
                f"ws://127.0.0.1:{args.gateway_port}/internal/voice/ws",
                robot_id=runtime["robot_id"],
                robot_secret=robot_secret,
                client_type="codex_hybrid_harness",
                bot_id=runtime["bot_id"],
                opus_payload=fixture.pop("opus_payload"),
                packet_count=fixture["packet_count"],
                duration_ms=fixture["duration_ms"],
                timeout=args.timeout,
                trace_id=trace_id,
            )
            if not probe.ok:
                raise HarnessRunError("active audio probe 未通过，详见 Gateway/STT 日志")
            verified = _verify_active_audio_probe(probe.detail)
            trace_response = requests.get(
                f"{base_url}/internal/runtime/traces/{trace_id}", timeout=args.timeout
            )
            if not trace_response.ok:
                raise HarnessRunError("Audio Gateway trace 查询失败")
            trace_payload = trace_response.json()
            (output_dir / "gateway-trace.json").write_text(
                json.dumps(trace_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            trace = trace_payload.get("trace") or {}
            stages = {
                str(item.get("stage") or "")
                for item in trace.get("events", [])
                if isinstance(item, dict)
            }
            for stage in ("audio_decoded", "asr_done", "llm_done", "tts_first_audio"):
                if stage not in stages:
                    raise HarnessRunError(f"Audio Gateway trace 缺少真实阶段: {stage}")
            metrics = trace.get("metrics") or {}
            if float(metrics.get("asr_inference_ms") or 0) <= 0:
                raise HarnessRunError("Audio Gateway trace 缺少真实 ASR 推理耗时")
            if int(metrics.get("llm_response_chars") or 0) <= 0:
                raise HarnessRunError("Audio Gateway trace 缺少主 LLM 回复")
            if int(metrics.get("audio_chunks") or 0) <= 0:
                raise HarnessRunError("Audio Gateway trace 缺少 TTS 音频")
            raw = {
                "schema_version": "ai-voice-dev-harness-observation/v1",
                "classification": "HYBRID_DIAGNOSTIC" if dirty else "HYBRID_CANDIDATE",
                "observed_at": _now(),
                "commit": commit,
                "dirty": dirty,
                "scenario": args.scenario,
                "runtime_snapshot": runtime,
                "fixture": fixture,
                "active_audio": verified,
                "trace_metrics": metrics,
                "trace_stages": sorted(stages),
                "assertions": {
                    "target_is_allowlisted": True,
                    "runtime_snapshot": True,
                    "same_trace": True,
                    "tracked_audio_fixture": True,
                    "asr_fact_fidelity": True,
                    "main_llm_response": True,
                    "real_audio": True,
                },
                "limitations": [
                    "输入是仓库固定参考录音，不代表现场麦克风或声学鲁棒性",
                    "未经过 Go Gateway、Rust Client 或部署环境",
                    "未验证声卡播放和实体硬件",
                ],
            }
            raw_path = output_dir / "observation.json"
            raw_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Hybrid scenario passed: {raw_path.relative_to(ROOT)}")
            if args.certify:
                report_path = _write_audio_certified_report(
                    output_dir=output_dir,
                    raw_path=raw_path,
                    runtime=runtime,
                    trace_id=trace_id,
                    authorization_reference=args.authorization_reference.strip(),
                )
                print(f"Evidence report valid: {report_path.relative_to(ROOT)}")
            return 0

        if robot_route:
            traces = {
                action: f"dev-harness-{run_id}-{action}"
                for action, _type, _prompt in ACTION_CASES
            }
            topic_prefix = (
                os.getenv("ROBOT_MQTT_TOPIC_PREFIX", "windaka").strip().strip("/")
                or "windaka"
            )
            topic = f"{topic_prefix}/{args.robot_id}/mcp/task/manual_control_cmd"
            capture = MqttCapture(topic=topic, expected_trace_ids=set(traces.values()))
            capture.start(min(args.timeout, 30))
            verified_actions = []
            for action, expected_type, prompt in ACTION_CASES:
                trace_id = traces[action]
                probe = gateway_probe.run_text_probe(
                    ws_url=f"ws://127.0.0.1:{args.gateway_port}/ws",
                    robot_id=runtime["robot_id"],
                    robot_secret=robot_secret,
                    bot_id=runtime["bot_id"],
                    text=prompt,
                    trace_id=trace_id,
                    timeout=args.timeout,
                )
                trace_response = requests.get(
                    f"{base_url}/internal/runtime/traces/{trace_id}", timeout=args.timeout
                )
                if not trace_response.ok:
                    raise HarnessRunError(f"Gateway trace 查询失败: action={action}")
                trace_payload = trace_response.json()
                (output_dir / f"gateway-trace-{action}.json").write_text(
                    json.dumps(trace_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                verified = _verify_direct_text_probe(
                    probe=probe,
                    trace_payload=trace_payload,
                    runtime=runtime,
                    expected_route="robot",
                    expected_action=action,
                )
                verified_actions.append(
                    {
                        "action": action,
                        "expected_type": expected_type,
                        "prompt": prompt,
                        **verified,
                    }
                )
            if not capture.complete.wait(min(args.timeout, 30)):
                raise HarnessRunError("MQTT 未在超时内收到两条带目标 trace 的消息")
            _verify_capture(capture=capture, traces=traces, topic=topic)
            raw = {
                "schema_version": "ai-voice-dev-harness-observation/v1",
                "classification": "HYBRID_DIAGNOSTIC" if dirty else "HYBRID_CANDIDATE",
                "observed_at": _now(),
                "commit": commit,
                "dirty": dirty,
                "scenario": args.scenario,
                "runtime_snapshot": runtime,
                "topic": topic,
                "trace_ids": list(traces.values()),
                "actions": verified_actions,
                "mqtt_messages": capture.messages,
                "assertions": {
                    "target_is_allowlisted": True,
                    "runtime_snapshot": True,
                    "same_trace": True,
                    "exact_robot_tool": True,
                    "expected_action_types": True,
                    "independent_subscriber_received_both": True,
                    "real_audio": True,
                    "asr_bypassed_by_contract": True,
                },
                "limitations": [
                    "direct text 按协议绕过 ASR",
                    "未经过 Go Gateway、Rust Client 或部署环境",
                    "未验证 Robot 业务消费者 ACK 或实体动作完成",
                ],
            }
            raw_path = output_dir / "observation.json"
            raw_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(f"Hybrid scenario passed: {raw_path.relative_to(ROOT)}")
            if args.certify:
                report_path = _write_robot_voice_certified_report(
                    output_dir=output_dir,
                    raw_path=raw_path,
                    runtime=runtime,
                    traces=traces,
                    authorization_reference=args.authorization_reference.strip(),
                )
                print(f"Evidence report valid: {report_path.relative_to(ROOT)}")
            return 0

        trace_id = f"dev-harness-{run_id}-{'utils-time' if utils_route else 'direct-text'}"
        prompt = UTILS_TIME_PROMPT if utils_route else DIRECT_TEXT_PROMPT
        probe = gateway_probe.run_text_probe(
            ws_url=f"ws://127.0.0.1:{args.gateway_port}/ws",
            robot_id=runtime["robot_id"],
            robot_secret=robot_secret,
            bot_id=runtime["bot_id"],
            text=prompt,
            trace_id=trace_id,
            timeout=args.timeout,
        )
        trace_response = requests.get(
            f"{base_url}/internal/runtime/traces/{trace_id}", timeout=args.timeout
        )
        if not trace_response.ok:
            raise HarnessRunError("Gateway trace 查询失败")
        trace_payload = trace_response.json()
        (output_dir / "gateway-trace.json").write_text(
            json.dumps(trace_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        verified = _verify_direct_text_probe(
            probe=probe,
            trace_payload=trace_payload,
            runtime=runtime,
            expected_route="utils" if utils_route else "chat",
        )
        observation_assertions = {
            "target_is_allowlisted": True,
            "runtime_snapshot": True,
            "same_trace": True,
            "main_llm_response": True,
            "real_audio": True,
            "asr_bypassed_by_contract": True,
        }
        if utils_route:
            observation_assertions.update(
                {
                    "utils_tool_called_once": True,
                    "utils_fact_fidelity": True,
                    "all_enabled_bots_covered": True,
                }
            )
        else:
            observation_assertions.update(
                {
                    "router_classifier_used": True,
                    "no_tool_call": True,
                }
            )
        raw = {
            "schema_version": "ai-voice-dev-harness-observation/v1",
            "classification": "HYBRID_DIAGNOSTIC" if dirty else "HYBRID_CANDIDATE",
            "observed_at": _now(),
            "commit": commit,
            "dirty": dirty,
            "scenario": args.scenario,
            "runtime_snapshot": runtime,
            "direct_text": verified,
            "utils_matrix": utils_matrix,
            "assertions": observation_assertions,
            "limitations": [
                "direct text 按协议绕过 ASR",
                "未经过 Go Gateway、Rust Client 或部署环境",
                "未验证声卡播放和实体硬件",
            ],
        }
        raw_path = output_dir / "observation.json"
        raw_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Hybrid scenario passed: {raw_path.relative_to(ROOT)}")
        if args.certify:
            report_path = _write_direct_text_certified_report(
                output_dir=output_dir,
                raw_path=raw_path,
                runtime=runtime,
                trace_id=trace_id,
                authorization_reference=args.authorization_reference.strip(),
                utils_matrix=utils_matrix,
            )
            print(f"Evidence report valid: {report_path.relative_to(ROOT)}")
        return 0
    except (HarnessRunError, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Harness failed: {exc}")
        return 1
    finally:
        if capture is not None:
            capture.stop()
        _stop_processes(processes)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=[
            "robot-actions",
            "voice-client-event",
            "voice-audio",
            "voice-m1-e2e",
            "voice-agent-continuity",
            "voice-vision",
            "voice-direct-text",
            "voice-robot-actions",
            "voice-utils-time",
        ],
        default="robot-actions",
    )
    parser.add_argument("--robot-id", default="test_01")
    parser.add_argument("--bot-id")
    parser.add_argument("--llm-port", type=int, default=55053)
    parser.add_argument("--llm-admin-port", type=int, default=58053)
    parser.add_argument("--tts-port", type=int, default=55052)
    parser.add_argument("--tts-admin-port", type=int, default=58052)
    parser.add_argument("--stt-port", type=int, default=55054)
    parser.add_argument("--stt-admin-port", type=int, default=58054)
    parser.add_argument("--gateway-port", type=int, default=57860)
    parser.add_argument("--go-port", type=int, default=58282)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--authorize-connectivity", action="store_true")
    parser.add_argument("--authorize-external-write", action="store_true")
    parser.add_argument("--authorization-reference", default="")
    parser.add_argument(
        "--certify",
        action="store_true",
        help="要求 clean worktree，并生成可校验的 HYBRID_VERIFIED 报告",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.robot_id not in SAFE_ROBOT_IDS:
        print(f"Harness refused: Robot ID 不在固定测试白名单: {args.robot_id}")
        return 2
    if args.dry_run:
        if args.scenario == "voice-vision":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "fixture": str(VISION_FIXTURE.relative_to(ROOT)),
                        "fixture_sha256": VISION_FIXTURE_SHA256,
                        "prompt": VISION_PROMPT,
                        "local_services": [
                            "rust_client",
                            "go_gateway",
                            "python_gateway",
                            "llm_grpc",
                            "tts_grpc",
                        ],
                        "transport": "native_webrtc_vision_v1_data_channel",
                        "claimed_level": "HYBRID_VERIFIED",
                        "requires": ["--authorize-connectivity", "--authorization-reference"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.scenario == "voice-m1-e2e":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "fixture": str(VOICE_AUDIO_FIXTURE.relative_to(ROOT)),
                        "fixture_sha256": VOICE_AUDIO_FIXTURE_SHA256,
                        "local_services": [
                            "rust_client",
                            "go_gateway",
                            "python_gateway",
                            "stt_grpc",
                            "llm_grpc",
                            "tts_grpc",
                        ],
                        "transport": "native_webrtc_rtp_only",
                        "claimed_level": "HYBRID_VERIFIED",
                        "requires": ["--authorize-connectivity", "--authorization-reference"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.scenario == "voice-audio":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "fixture": str(VOICE_AUDIO_FIXTURE.relative_to(ROOT)),
                        "fixture_sha256": VOICE_AUDIO_FIXTURE_SHA256,
                        "local_services": [
                            "robot_mcp",
                            "stt_grpc",
                            "llm_grpc",
                            "tts_grpc",
                            "python_gateway",
                        ],
                        "remote_dependencies": [
                            "test_mysql",
                            "asr_model",
                            "router_model",
                            "main_llm_model",
                            "tts_model",
                        ],
                        "input_scope": "tracked_reference_recording",
                        "requires": [
                            "--authorize-connectivity",
                            "--authorization-reference",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.scenario == "voice-robot-actions":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "actions": [item[0] for item in ACTION_CASES],
                        "local_services": [
                            "robot_mcp",
                            "llm_grpc",
                            "tts_grpc",
                            "python_gateway",
                        ],
                        "remote_dependencies": [
                            "test_mysql",
                            "router_model",
                            "main_llm_model",
                            "tts_model",
                            "mqtt",
                        ],
                        "asr_path": "bypassed_by_direct_text_contract",
                        "requires": [
                            "--authorize-connectivity",
                            "--authorize-external-write",
                            "--authorization-reference",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.scenario == "voice-utils-time":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "input": UTILS_TIME_PROMPT,
                        "local_services": [
                            "robot_mcp",
                            "utils_mcp",
                            "llm_grpc",
                            "tts_grpc",
                            "python_gateway",
                        ],
                        "coverage": "all_enabled_bots_plus_test_01_audio",
                        "external_write": False,
                        "requires": [
                            "--authorize-connectivity",
                            "--authorization-reference",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.scenario == "voice-direct-text":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "input": DIRECT_TEXT_PROMPT,
                        "local_services": [
                            "robot_mcp",
                            "llm_grpc",
                            "tts_grpc",
                            "python_gateway",
                        ],
                        "remote_dependencies": [
                            "test_mysql",
                            "router_model",
                            "main_llm_model",
                            "tts_model",
                        ],
                        "asr_path": "bypassed_by_direct_text_contract",
                        "requires": [
                            "--authorize-connectivity",
                            "--authorization-reference",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.scenario == "voice-agent-continuity":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "inputs": [AGENT_ENTRY_PROMPT, AGENT_CONFIRM_PROMPT],
                        "transport": "m1_input_text_commit_same_session",
                        "local_services": [
                            "robot_mcp",
                            "llm_grpc",
                            "tts_grpc",
                            "python_gateway",
                        ],
                        "remote_dependencies": [
                            "test_mysql",
                            "router_model",
                            "main_llm_model",
                            "tts_model",
                        ],
                        "requires": [
                            "--authorize-connectivity",
                            "--authorization-reference",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.scenario == "voice-client-event":
            print(
                json.dumps(
                    {
                        "scenario": args.scenario,
                        "robot_id": args.robot_id,
                        "event": "wake_idle",
                        "local_services": ["tts_grpc", "python_gateway"],
                        "remote_dependencies": ["test_mysql", "tts_model"],
                        "llm_path": "bypassed_by_client_event_contract",
                        "requires": [
                            "--authorize-connectivity",
                            "--authorization-reference",
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "scenario": args.scenario,
                    "robot_id": args.robot_id,
                    "actions": [item[0] for item in ACTION_CASES],
                    "local_services": ["robot_mcp", "llm_grpc"],
                    "remote_dependencies": ["test_mysql", "mqtt", "vllm_readiness_preflight"],
                    "requires": [
                        "--authorize-connectivity",
                        "--authorize-external-write",
                        "--authorization-reference",
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not args.authorize_connectivity:
        print("Harness refused: 必须显式授权 connectivity")
        return 2
    if (
        args.scenario in {"robot-actions", "voice-robot-actions"}
        and not args.authorize_external_write
    ):
        print("Harness refused: 必须显式授权 connectivity 和 external write")
        return 2
    if not args.authorization_reference.strip():
        print("Harness refused: 必须提供 --authorization-reference")
        return 2
    if args.scenario == "voice-client-event":
        return _run_voice_client_event(args)
    if args.scenario in {"voice-m1-e2e", "voice-vision"}:
        return _run_voice_m1_e2e(args)
    if args.scenario in {
        "voice-audio",
        "voice-agent-continuity",
        "voice-direct-text",
        "voice-robot-actions",
        "voice-utils-time",
    }:
        return _run_voice_direct_text(args)

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    output_dir = _safe_output_dir(
        args.output_dir or Path("reports") / f"dev-harness-{args.scenario}-{run_id}"
    )
    log_dir = output_dir / "logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    commit, dirty = _git_state()
    if args.certify and dirty:
        print("Harness refused: --certify 要求 clean worktree；开发诊断请移除 --certify")
        return 2

    processes: list[tuple[str, subprocess.Popen[Any]]] = []
    capture: MqttCapture | None = None
    try:
        _run_preflight(log_dir)
        runtime = _load_runtime_target(args.robot_id, args.bot_id)
        for port in (runtime["mcp_port"], args.llm_port, args.llm_admin_port):
            if not _port_is_free(port):
                raise HarnessRunError(
                    f"隔离端口已被占用: {port}；Runner 不复用也不终止来源不明的进程"
                )

        env = os.environ.copy()
        direct_hosts = _proxy_bypass_hosts(env)
        for key in ("NO_PROXY", "no_proxy"):
            direct_hosts.update(filter(None, env.get(key, "").split(",")))
            env[key] = ",".join(sorted(direct_hosts))
        env["LLM_GRPC_SERVER_PORT"] = str(args.llm_port)
        env["LLM_ADMIN_PORT"] = str(args.llm_admin_port)

        mcp_proc = _start_process(
            "robot-mcp",
            [sys.executable, "mcp_servers/robot_sse_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("robot-mcp", mcp_proc))
        _wait_http(
            f"http://127.0.0.1:{runtime['mcp_port']}/health",
            mcp_proc,
            min(args.timeout, 30),
        )

        llm_proc = _start_process(
            "llm-grpc",
            [sys.executable, "llm/llm_grpc_server.py"],
            env=env,
            log_dir=log_dir,
        )
        processes.append(("llm-grpc", llm_proc))
        _wait_port(args.llm_port, llm_proc, args.timeout)

        traces = {
            action: f"dev-harness-{run_id}-{action}" for action, _type, _prompt in ACTION_CASES
        }
        topic_prefix = os.getenv("ROBOT_MQTT_TOPIC_PREFIX", "windaka").strip().strip("/") or "windaka"
        topic = f"{topic_prefix}/{args.robot_id}/mcp/task/manual_control_cmd"
        capture = MqttCapture(topic=topic, expected_trace_ids=set(traces.values()))
        capture.start(min(args.timeout, 30))
        llm_results = _invoke_llm_actions(
            port=args.llm_port,
            runtime=runtime,
            run_id=run_id,
            traces=traces,
            timeout=args.timeout,
        )
        if not capture.complete.wait(min(args.timeout, 30)):
            raise HarnessRunError("MQTT 未在超时内收到两条带目标 trace 的消息")
        _verify_capture(capture=capture, traces=traces, topic=topic)

        raw = {
            "schema_version": "ai-voice-dev-harness-observation/v1",
            "classification": "HYBRID_DIAGNOSTIC" if dirty else "HYBRID_CANDIDATE",
            "observed_at": _now(),
            "commit": commit,
            "dirty": dirty,
            "scenario": args.scenario,
            "runtime_snapshot": runtime,
            "topic": topic,
            "trace_ids": list(traces.values()),
            "llm_results": llm_results,
            "mqtt_messages": capture.messages,
            "assertions": {
                "target_is_allowlisted": True,
                "same_trace": True,
                "expected_action_types": True,
                "non_retained": True,
                "independent_subscriber_received_both": True,
            },
            "limitations": [
                "未经过部署环境 Gateway",
                "未验证 Robot 业务消费者 ACK",
                "仅覆盖单 Bot smoke",
            ],
        }
        raw_path = output_dir / "observation.json"
        raw_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Hybrid scenario passed: {raw_path.relative_to(ROOT)}")
        if args.certify:
            report_path = _write_certified_report(
                output_dir=output_dir,
                raw_path=raw_path,
                runtime=runtime,
                traces=traces,
                authorization_reference=args.authorization_reference.strip(),
            )
            print(f"Evidence report valid: {report_path.relative_to(ROOT)}")
        return 0
    except (HarnessRunError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Harness failed: {exc}")
        return 1
    finally:
        if capture is not None:
            capture.stop()
        _stop_processes(processes)


if __name__ == "__main__":
    raise SystemExit(main())
