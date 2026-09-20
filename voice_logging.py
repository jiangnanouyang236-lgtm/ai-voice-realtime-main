from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_LOG_FORMAT = "text"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5

_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|api[_-]?key|password|passwd|robot[_-]?secret|secret|token)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT_RE = re.compile(
    r"\b(authorization|api[_-]?key|password|passwd|robot[_-]?secret|secret|token)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)

_STANDARD_RECORD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__.keys()) | {
    "asctime",
    "message",
}

_LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[35m",
}
_RESET = "\033[0m"


def configure_logging(service: str, *, force: bool = True) -> dict[str, Any]:
    """Configure process-wide Python logging for voice services."""
    level_name = _env_first(("PYTHON_LOG_LEVEL", "VOICE_LOG_LEVEL", "LOG_LEVEL"), DEFAULT_LOG_LEVEL)
    level = _parse_level(level_name)
    fmt = _env_first(("PYTHON_LOG_FORMAT", "VOICE_LOG_FORMAT", "LOG_FORMAT"), DEFAULT_LOG_FORMAT).lower()
    color = _env_first(("PYTHON_LOG_COLOR", "VOICE_LOG_COLOR", "LOG_COLOR"), "auto").lower()
    stream_name = _env_first(("PYTHON_LOG_STREAM", "VOICE_LOG_STREAM", "LOG_STREAM"), "stdout").lower()
    log_file = _resolve_log_file(service)
    max_bytes = _env_int(
        ("PYTHON_LOG_MAX_BYTES", "VOICE_LOG_MAX_BYTES", "LOG_MAX_BYTES"),
        DEFAULT_LOG_MAX_BYTES,
    )
    backup_count = _env_int(
        ("PYTHON_LOG_BACKUP_COUNT", "VOICE_LOG_BACKUP_COUNT", "LOG_BACKUP_COUNT"),
        DEFAULT_LOG_BACKUP_COUNT,
    )

    root = logging.getLogger()
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()

    handlers: list[logging.Handler] = []
    stream = sys.stderr if stream_name == "stderr" else sys.stdout
    console_handler = logging.StreamHandler(stream)
    console_handler.setFormatter(_build_formatter(fmt, service, _should_color(color, stream)))
    handlers.append(console_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max(0, max_bytes),
            backupCount=max(0, backup_count),
            encoding="utf-8",
        )
        file_handler.setFormatter(_build_formatter(fmt, service, False))
        handlers.append(file_handler)

    service_filter = _ServiceFilter(service)
    for handler in handlers:
        handler.addFilter(service_filter)
        root.addHandler(handler)

    root.setLevel(level)
    logging.captureWarnings(True)
    return {
        "service": service,
        "level": logging.getLevelName(level),
        "format": fmt,
        "color": color,
        "file": log_file or "",
        "max_bytes": max_bytes,
        "backup_count": backup_count,
    }


class _ServiceFilter(logging.Filter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self.service
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return _redact_text(rendered)


class _ColorFormatter(_RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        original_level = record.levelname
        color = _LEVEL_COLORS.get(original_level)
        if color:
            record.levelname = f"{color}{original_level}{_RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_level


class _JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "level": record.levelname.lower(),
            "service": getattr(record, "service", self.service),
            "logger": record.name,
            "message": _redact_text(record.getMessage()),
            "module": record.module,
            "line": record.lineno,
            "process": record.process,
            "thread": record.threadName,
        }
        if record.exc_info:
            payload["exception"] = _redact_text(self.formatException(record.exc_info))
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key == "service":
                continue
            payload[key] = _redact_value(key, value)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _build_formatter(fmt: str, service: str, use_color: bool) -> logging.Formatter:
    if fmt == "json":
        return _JsonFormatter(service)
    pattern = "%(asctime)s.%(msecs)03d %(levelname)s service=%(service)s logger=%(name)s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S%z"
    formatter_cls = _ColorFormatter if use_color else _RedactingFormatter
    return formatter_cls(pattern, datefmt=datefmt)


def _resolve_log_file(service: str) -> str:
    explicit_file = _env_first(("PYTHON_LOG_FILE", "VOICE_LOG_FILE", "LOG_FILE"), "")
    if explicit_file:
        return explicit_file
    log_dir = _env_first(("PYTHON_LOG_DIR", "VOICE_LOG_DIR", "LOG_DIR"), "")
    if not log_dir:
        return ""
    return str(Path(log_dir) / f"{service}.log")


def _env_first(names: tuple[str, ...], default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_int(names: tuple[str, ...], default: int) -> int:
    raw = _env_first(names, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_level(raw: str) -> int:
    name = (raw or DEFAULT_LOG_LEVEL).strip().upper()
    return int(getattr(logging, name, logging.INFO))


def _should_color(raw: str, stream: Any) -> bool:
    value = (raw or "auto").strip().lower()
    if value in {"1", "true", "yes", "on", "always"}:
        return True
    if value in {"0", "false", "no", "off", "never"}:
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _redact_text(value: str) -> str:
    return _SENSITIVE_TEXT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", value)


def _redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return "[redacted]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    return value
