"""
Helpers for one-time Robot secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_robot_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_robot_secret(secret: str, *, salt: str | None = None) -> str:
    salt = salt or secrets.token_urlsafe(16)
    digest = hashlib.sha256(f"{salt}:{secret}".encode("utf-8")).hexdigest()
    return f"sha256${salt}${digest}"


def verify_robot_secret(secret: str, secret_hash: str | None) -> bool:
    if not secret or not secret_hash:
        return False
    try:
        algorithm, salt, expected = secret_hash.split("$", 2)
    except ValueError:
        return False
    if algorithm != "sha256":
        return False
    digest = hashlib.sha256(f"{salt}:{secret}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, expected)
