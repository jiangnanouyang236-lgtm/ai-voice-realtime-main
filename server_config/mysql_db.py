"""
MySQL/MariaDB connection helpers for server-config.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


JSON_FIELDS = {
    "args_json",
    "headers_json",
    "snapshot_json",
    "config_json",
    "trigger_examples_json",
    "allowed_tools_json",
    "tool_groups_json",
    "provider_config_json",
}
BOOL_FIELDS = {
    "enabled",
    "is_default",
    "is_new",
    "enabled_by_default",
    "secret_configured",
    "robot_enabled",
}


def connect_mysql(database_url: str) -> Any:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"mysql", "mariadb"}:
        raise ValueError("CONFIG_DATABASE_URL 需要使用 mysql:// 或 mariadb:// 连接串")
    if not parsed.hostname:
        raise ValueError("CONFIG_DATABASE_URL 缺少数据库 host")
    if not parsed.path or parsed.path == "/":
        raise ValueError("CONFIG_DATABASE_URL 缺少数据库名称")

    query = parse_qs(parsed.query)
    charset = query.get("charset", ["utf8mb4"])[0]
    connect_timeout = int(os.getenv("CONFIG_DB_CONNECT_TIMEOUT", query.get("connect_timeout", ["5"])[0]))
    read_timeout = int(os.getenv("CONFIG_DB_READ_TIMEOUT", query.get("read_timeout", ["10"])[0]))
    write_timeout = int(os.getenv("CONFIG_DB_WRITE_TIMEOUT", query.get("write_timeout", ["10"])[0]))

    import pymysql
    import pymysql.cursors

    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=unquote(parsed.path.lstrip("/")),
        charset=charset,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        write_timeout=write_timeout,
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def decode_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None

    decoded = dict(row)
    for field in JSON_FIELDS:
        if field in decoded:
            decoded[field] = decode_json_value(decoded[field])

    for field in BOOL_FIELDS:
        if field in decoded and decoded[field] is not None:
            decoded[field] = bool(decoded[field])

    return decoded


def decode_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [decode_row(row) or {} for row in rows]


def decode_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return json.loads(text)
    return value


def placeholders(values: list[Any] | tuple[Any, ...]) -> str:
    if not values:
        raise ValueError("Cannot build placeholders for an empty value list")
    return ", ".join(["%s"] * len(values))
