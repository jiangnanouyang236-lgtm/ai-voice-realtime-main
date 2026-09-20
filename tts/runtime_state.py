"""
TTS runtime state backed by server-config snapshots.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from server_config.models import TTSProfileConfig, TTSSettingsConfig, utcnow
from server_config.repository import ConfigRepository


logger = logging.getLogger(__name__)


class TTSRuntimeState:
    def __init__(self, *, database_url: str) -> None:
        self.database_url = (database_url or "").strip()
        if not self.database_url:
            raise ValueError("TTS runtime 缺少 CONFIG_DATABASE_URL，当前分支要求从数据库加载配置")

        self.repository = ConfigRepository(self.database_url)
        self._lock = threading.RLock()
        self._source = "db"
        self._config_version: int | None = None
        self._loaded_at = utcnow()
        self._settings = TTSSettingsConfig(
            realtime_model="qwen3-tts",
            default_voice="serena",
            default_speed=1.0,
        )
        self._tts_profiles: dict[str, TTSProfileConfig] = {}
        self._request_count = 0
        self._last_request_at = None
        self._last_error: str | None = None
        self._last_error_at = None
        self._last_reload_error: str | None = None

        try:
            self._load_from_database()
        except Exception as exc:
            logger.exception("从数据库加载 TTS 配置失败: %s", exc)
            self._last_reload_error = str(exc)
            self._loaded_at = utcnow()
            raise

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "success": True,
                "source": self._source,
                "config_version": self._config_version,
                "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
                "tts_settings": self._settings.to_dict(),
                "tts_profile_count": len(self._tts_profiles),
                "tts_profiles": [
                    {
                        "tts_id": profile.tts_id,
                        "tts_name": profile.tts_name,
                        "provider_type": profile.provider_type,
                        "speed": profile.speed,
                        "enabled": profile.enabled,
                    }
                    for profile in sorted(self._tts_profiles.values(), key=lambda item: item.tts_id)
                ],
                "request_count": self._request_count,
                "last_request_at": self._last_request_at.isoformat() if self._last_request_at else None,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at.isoformat() if self._last_error_at else None,
                "last_reload_error": self._last_reload_error,
            }

    async def validate(self, version: int | None = None) -> dict[str, Any]:
        snapshot = await asyncio.to_thread(self._build_runtime_snapshot, version)
        return {
            "success": True,
            "source": snapshot.source,
            "config_version": snapshot.config_version,
            "validated_at": utcnow().isoformat(),
            "tts_settings": snapshot.tts_settings.to_dict(),
            "tts_profile_count": len(snapshot.tts_profiles),
        }

    async def reload(self, version: int | None = None) -> dict[str, Any]:
        try:
            snapshot = await asyncio.to_thread(self._build_runtime_snapshot, version)
            with self._lock:
                self._source = snapshot.source
                self._config_version = snapshot.config_version
                self._loaded_at = snapshot.loaded_at
                self._settings = snapshot.tts_settings
                self._tts_profiles = dict(snapshot.tts_profiles)
                self._last_reload_error = None
        except Exception as exc:
            logger.exception("重新加载 TTS runtime 配置失败: %s", exc)
            with self._lock:
                self._last_reload_error = str(exc)
            raise
        return self.get_status()

    def get_settings(self) -> TTSSettingsConfig:
        with self._lock:
            return self._settings

    def get_tts_profile(self, tts_profile_id: str) -> TTSProfileConfig:
        profile_id = (tts_profile_id or "").strip()
        if not profile_id:
            raise ValueError("TTS 请求缺少 tts_profile_id")
        with self._lock:
            profile = self._tts_profiles.get(profile_id)
        if profile is None:
            raise ValueError(f"TTS Profile 未加载或不存在: {profile_id}")
        if not profile.enabled:
            raise ValueError(f"TTS Profile 已禁用: {profile_id}")
        return profile

    def mark_request_started(self) -> None:
        with self._lock:
            self._request_count += 1
            self._last_request_at = utcnow()

    def mark_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
            self._last_error_at = utcnow()

    def clear_error(self) -> None:
        with self._lock:
            self._last_error = None
            self._last_error_at = None

    def _build_runtime_snapshot(self, version: int | None = None):
        return self.repository.load_tts_runtime_snapshot(version=version)

    def _load_from_database(self) -> None:
        snapshot = self._build_runtime_snapshot()
        with self._lock:
            self._source = snapshot.source
            self._config_version = snapshot.config_version
            self._loaded_at = snapshot.loaded_at
            self._settings = snapshot.tts_settings
            self._tts_profiles = dict(snapshot.tts_profiles)
