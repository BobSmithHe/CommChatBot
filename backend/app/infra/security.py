from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from jose import JWTError, jwt

from .config import get_settings


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return base64.b64encode(salt + digest).decode("ascii")


def verify_password(password: str, encoded: str) -> bool:
    if not encoded:
        return False
    raw = base64.b64decode(encoded.encode("ascii"))
    salt, expected = raw[:16], raw[16:]
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return hmac.compare_digest(actual, expected)


def create_access_token(subject: str) -> str:
    settings = get_settings()
    payload = {
        "sub": subject,
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> str | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    if payload.get("type") != "access":
        return None
    subject = payload.get("sub")
    return str(subject) if subject else None


@lru_cache
def _jwt_secret() -> str:
    settings = get_settings()
    configured = settings.jwt_secret_key.strip()
    if configured and not configured.lower().startswith(("change-me", "replace-with")):
        return configured
    secret_path = Path(settings.data_dir) / ".jwt-secret"
    if secret_path.is_file():
        stored = secret_path.read_text(encoding="utf-8").strip()
        if len(stored) >= 32:
            return stored
    generated = secrets.token_urlsafe(48)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(generated, encoding="utf-8")
    return generated
