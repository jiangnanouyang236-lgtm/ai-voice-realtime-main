"""
Admin backend settings.
"""

from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Load a small .env file without overriding real environment variables."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT_DIR / ".env")


class Settings:
    def __init__(self) -> None:
        self.database_url = os.getenv("CONFIG_DATABASE_URL", "").strip()
        self.admin_api_host = os.getenv("ADMIN_API_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.admin_api_port = int(os.getenv("ADMIN_API_PORT", "8282"))
        self.admin_username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")
        self.admin_session_secret = os.getenv("ADMIN_SESSION_SECRET", "").strip() or self.admin_password
        self.admin_session_ttl_seconds = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", str(12 * 60 * 60)))
        self.admin_cookie_secure = os.getenv("ADMIN_COOKIE_SECURE", "false").lower() == "true"
        self.admin_auth_disabled = os.getenv("ADMIN_AUTH_DISABLED", "false").lower() == "true"
        self.llm_internal_base_url = os.getenv("LLM_INTERNAL_BASE_URL", "http://127.0.0.1:18053").rstrip("/")
        self.gateway_internal_base_url = os.getenv("GATEWAY_INTERNAL_BASE_URL", "http://127.0.0.1:7860").rstrip("/")
        self.tts_internal_base_url = os.getenv("TTS_INTERNAL_BASE_URL", "http://127.0.0.1:18052").rstrip("/")
        self.cors_origins = [
            item.strip()
            for item in os.getenv("ADMIN_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",")
            if item.strip()
        ]

        if not self.database_url:
            raise ValueError("缺少环境变量 CONFIG_DATABASE_URL")
        if not self.admin_auth_disabled and not self.admin_password:
            raise ValueError("缺少环境变量 ADMIN_PASSWORD，Admin UI 登录鉴权无法启用")
        if not self.admin_auth_disabled and self.admin_session_ttl_seconds <= 0:
            raise ValueError("ADMIN_SESSION_TTL_SECONDS 必须大于 0")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
