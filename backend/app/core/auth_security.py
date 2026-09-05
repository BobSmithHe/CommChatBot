from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..infra.config import get_settings
from ..infra.database import (
    AuthAuditRecord,
    PasswordResetTokenRecord,
    RefreshTokenRecord,
    User,
)
from ..infra.security import create_access_token, hash_password


class AuthRateLimitError(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Too many authentication attempts")
        self.retry_after = retry_after


class AuthRateLimiter:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._memory: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._redis = None

    def check(self, action: str, identity: str, limit: int) -> None:
        window = max(10, self.settings.auth_rate_limit_window_seconds)
        bucket = int(time.time() // window)
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
        key = f"commchat:auth-limit:{action}:{digest}:{bucket}"
        count = self._increment_redis(key, window)
        if count is None:
            now = time.monotonic()
            with self._lock:
                current, expires = self._memory.get(key, (0, now + window))
                if expires <= now:
                    current, expires = 0, now + window
                current += 1
                self._memory[key] = (current, expires)
                count = current
        if count > max(1, limit):
            retry_after = max(1, window - int(time.time() % window))
            raise AuthRateLimitError(retry_after)

    def clear(self, action: str, identity: str) -> None:
        window = max(10, self.settings.auth_rate_limit_window_seconds)
        bucket = int(time.time() // window)
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:24]
        key = f"commchat:auth-limit:{action}:{digest}:{bucket}"
        client = self._redis_client()
        if client:
            try:
                client.delete(key)
            except Exception:
                self._redis = False
        with self._lock:
            self._memory.pop(key, None)

    def _increment_redis(self, key: str, window: int) -> int | None:
        client = self._redis_client()
        if not client:
            return None
        try:
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, window + 2)
            return count
        except Exception:
            self._redis = False
            return None

    def _redis_client(self):
        if self._redis is False:
            return None
        if self._redis is None:
            try:
                import redis

                client = redis.Redis(
                    host=self.settings.redis_host,
                    port=self.settings.redis_port,
                    password=self.settings.redis_password or None,
                    socket_connect_timeout=0.3,
                    socket_timeout=0.3,
                    decode_responses=True,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._redis = False
        return self._redis or None


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(db: Session, user: User, ip_address: str, user_agent: str) -> dict:
    settings = get_settings()
    raw_refresh = secrets.token_urlsafe(48)
    db.add(RefreshTokenRecord(
        id=uuid.uuid4().hex,
        user_id=user.id,
        token_hash=token_digest(raw_refresh),
        expires_at=datetime.utcnow() + timedelta(days=max(1, settings.refresh_token_days)),
        ip_address=ip_address[:80],
        user_agent=user_agent[:300],
    ))
    db.commit()
    return {
        "access_token": create_access_token(str(user.id)),
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "expires_in": settings.jwt_expire_minutes * 60,
    }


def rotate_refresh_token(db: Session, raw_token: str, ip_address: str, user_agent: str) -> tuple[User, dict] | None:
    row = db.query(RefreshTokenRecord).filter(RefreshTokenRecord.token_hash == token_digest(raw_token)).first()
    if not row or row.revoked_at or row.expires_at <= datetime.utcnow():
        return None
    user = db.query(User).filter(User.id == row.user_id, User.is_active.is_(True)).first()
    if not user:
        return None
    revoked_at = datetime.utcnow()
    updated = db.query(RefreshTokenRecord).filter(
        RefreshTokenRecord.id == row.id,
        RefreshTokenRecord.revoked_at.is_(None),
    ).update({RefreshTokenRecord.revoked_at: revoked_at}, synchronize_session=False)
    if updated != 1:
        db.rollback()
        return None
    db.commit()
    return user, issue_session(db, user, ip_address, user_agent)


def revoke_refresh_token(db: Session, raw_token: str) -> int | None:
    row = db.query(RefreshTokenRecord).filter(RefreshTokenRecord.token_hash == token_digest(raw_token)).first()
    if not row:
        return None
    if not row.revoked_at:
        row.revoked_at = datetime.utcnow()
        db.commit()
    return row.user_id


def revoke_all_refresh_tokens(db: Session, user_id: int) -> None:
    db.query(RefreshTokenRecord).filter(
        RefreshTokenRecord.user_id == user_id,
        RefreshTokenRecord.revoked_at.is_(None),
    ).update({RefreshTokenRecord.revoked_at: datetime.utcnow()}, synchronize_session=False)


def create_password_reset(db: Session, user: User) -> str:
    settings = get_settings()
    now = datetime.utcnow()
    db.query(PasswordResetTokenRecord).filter(
        PasswordResetTokenRecord.user_id == user.id,
        PasswordResetTokenRecord.used_at.is_(None),
    ).update({PasswordResetTokenRecord.used_at: now}, synchronize_session=False)
    raw_token = secrets.token_urlsafe(48)
    db.add(PasswordResetTokenRecord(
        id=uuid.uuid4().hex,
        user_id=user.id,
        token_hash=token_digest(raw_token),
        expires_at=now + timedelta(minutes=max(5, settings.password_reset_minutes)),
    ))
    db.commit()
    return raw_token


def consume_password_reset(db: Session, raw_token: str, new_password: str) -> User | None:
    now = datetime.utcnow()
    row = db.query(PasswordResetTokenRecord).filter(
        PasswordResetTokenRecord.token_hash == token_digest(raw_token),
    ).first()
    if not row or row.used_at or row.expires_at <= now:
        return None
    user = db.query(User).filter(User.id == row.user_id, User.is_active.is_(True)).first()
    if not user:
        return None
    consumed = db.query(PasswordResetTokenRecord).filter(
        PasswordResetTokenRecord.id == row.id,
        PasswordResetTokenRecord.used_at.is_(None),
    ).update({PasswordResetTokenRecord.used_at: now}, synchronize_session=False)
    if consumed != 1:
        db.rollback()
        return None
    user.password_hash = hash_password(new_password)
    revoke_all_refresh_tokens(db, user.id)
    db.commit()
    return user


def audit_auth(
    db: Session,
    *,
    event: str,
    subject: str,
    ip_address: str,
    success: bool,
    user_id: int | None = None,
    detail: str | None = None,
) -> None:
    db.add(AuthAuditRecord(
        user_id=user_id,
        event=event,
        subject=subject[:200],
        ip_address=ip_address[:80],
        success=success,
        detail=(detail or "")[:500] or None,
    ))
    db.commit()


auth_rate_limiter = AuthRateLimiter()
