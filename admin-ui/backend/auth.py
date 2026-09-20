"""
Small cookie-based auth helpers for the Admin UI backend.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Request, Response

from config import Settings


COOKIE_NAME = "admin_session"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _b64encode(digest)


def create_session_token(username: str, settings: Settings) -> str:
    now = int(time.time())
    body = {
        "sub": username,
        "iat": now,
        "exp": now + settings.admin_session_ttl_seconds,
        "nonce": secrets.token_urlsafe(12),
    }
    payload = _b64encode(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    signature = _sign(payload, settings.admin_session_secret)
    return f"{payload}.{signature}"


def verify_session_token(token: str | None, settings: Settings) -> dict[str, Any] | None:
    if settings.admin_auth_disabled:
        return {"sub": settings.admin_username, "auth_disabled": True}
    if not token or "." not in token:
        return None

    payload, signature = token.rsplit(".", 1)
    expected = _sign(payload, settings.admin_session_secret)
    if not hmac.compare_digest(signature, expected):
        return None

    try:
        body = json.loads(_b64decode(payload))
    except Exception:
        return None

    if body.get("sub") != settings.admin_username:
        return None
    if int(body.get("exp", 0)) < int(time.time()):
        return None
    return body


def authenticate_credentials(username: str, password: str, settings: Settings) -> bool:
    if settings.admin_auth_disabled:
        return True
    username_ok = hmac.compare_digest(username, settings.admin_username)
    password_ok = hmac.compare_digest(password, settings.admin_password)
    return username_ok and password_ok


def get_current_admin(request: Request, settings: Settings) -> dict[str, Any] | None:
    return verify_session_token(request.cookies.get(COOKIE_NAME), settings)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="lax",
        path="/",
    )
