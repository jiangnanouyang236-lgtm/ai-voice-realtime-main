"""
TTS 内部配置管理 HTTP 接口。
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse


logger = logging.getLogger(__name__)


class ConfigAdminActions(Protocol):
    def get_config_status(self) -> dict[str, Any]:
        ...

    def validate_runtime_config(self, version: int | None = None) -> dict[str, Any]:
        ...

    def reload_runtime_config(self, version: int | None = None) -> dict[str, Any]:
        ...


class _ConfigAdminHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], actions: ConfigAdminActions):
        super().__init__(server_address, _ConfigAdminRequestHandler)
        self.actions = actions


class _ConfigAdminRequestHandler(BaseHTTPRequestHandler):
    server: _ConfigAdminHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/internal/config/status":
            try:
                payload = self.server.actions.get_config_status()
                self._write_json(HTTPStatus.OK, payload)
            except Exception as exc:
                logger.exception("读取 TTS 配置状态失败: %s", exc)
                self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"success": False, "message": str(exc)})
            return

        if parsed.path != "/internal/config/validate":
            self._write_json(HTTPStatus.NOT_FOUND, {"success": False, "message": "Not found"})
            return

        try:
            payload = self.server.actions.validate_runtime_config(self._parse_version(parsed.query))
            self._write_json(HTTPStatus.OK, payload)
        except Exception as exc:
            logger.exception("校验 TTS 配置失败: %s", exc)
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"success": False, "message": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/internal/config/reload":
            self._write_json(HTTPStatus.NOT_FOUND, {"success": False, "message": "Not found"})
            return

        try:
            payload = self.server.actions.reload_runtime_config(self._parse_version(parsed.query))
            self._write_json(HTTPStatus.OK, payload)
        except Exception as exc:
            logger.exception("重新加载 TTS 配置失败: %s", exc)
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"success": False, "message": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("TTSConfigHTTP %s - %s", self.address_string(), format % args)

    @staticmethod
    def _parse_version(query: str) -> int | None:
        raw_value = parse_qs(query).get("version", [None])[0]
        if raw_value in (None, ""):
            return None
        return int(raw_value)

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ConfigAdminHTTPServer:
    def __init__(self, host: str, port: int, actions: ConfigAdminActions):
        self._server = _ConfigAdminHTTPServer((host, port), actions)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="tts-config-admin-http",
            daemon=True,
        )
        self.host = host
        self.port = port

    def start(self) -> None:
        self._thread.start()
        logger.info("TTS 内部配置管理 HTTP 已启动: http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
