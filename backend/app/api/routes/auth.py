from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from ...platform.services.auth import (
    AuthRateLimitError,
    audit_auth,
    auth_rate_limiter,
    consume_password_reset,
    create_password_reset,
    issue_session,
    revoke_refresh_token,
    rotate_refresh_token,
)
from ...infra.config import get_settings
from ...platform.database import AuthAuditRecord, User, get_db
from ...infra.security import hash_password, verify_password
from ..deps import current_user
from ..schemas import (
    LoginRequest,
    LogoutRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RefreshTokenRequest,
    RegisterRequest,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
def register(req: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    _rate_limit("register", ip, get_settings().registration_rate_limit)
    exists = db.query(User).filter((User.username == req.username) | (User.email == req.email)).first()
    if exists:
        audit_auth(db, event="register", subject=req.username, ip_address=ip, success=False, detail="duplicate")
        raise HTTPException(status_code=400, detail="Username or email already exists")
    user = User(username=req.username, email=req.email, password_hash=hash_password(req.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    audit_auth(db, event="register", subject=user.username, ip_address=ip, success=True, user_id=user.id)
    return {"id": user.id, "username": user.username, "email": user.email, "is_active": user.is_active}


@router.post("/login")
def login(req: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    rate_key = f"{ip}:{req.username}"
    _rate_limit("login", rate_key, get_settings().login_rate_limit)
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not user.is_active or not verify_password(req.password, user.password_hash):
        audit_auth(
            db,
            event="login",
            subject=req.username,
            ip_address=ip,
            success=False,
            user_id=user.id if user else None,
            detail="invalid_credentials",
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")
    auth_rate_limiter.clear("login", rate_key)
    session = issue_session(db, user, ip, request.headers.get("user-agent", ""))
    audit_auth(db, event="login", subject=user.username, ip_address=ip, success=True, user_id=user.id)
    return session


@router.post("/refresh")
def refresh(req: RefreshTokenRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    _rate_limit("refresh", ip, 30)
    rotated = rotate_refresh_token(db, req.refresh_token, ip, request.headers.get("user-agent", ""))
    if not rotated:
        audit_auth(db, event="refresh", subject="unknown", ip_address=ip, success=False, detail="invalid_token")
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user, session = rotated
    audit_auth(db, event="refresh", subject=user.username, ip_address=ip, success=True, user_id=user.id)
    return session


@router.post("/logout")
def logout(req: LogoutRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    user_id = revoke_refresh_token(db, req.refresh_token)
    user = db.query(User).filter(User.id == user_id).first() if user_id else None
    audit_auth(
        db,
        event="logout",
        subject=user.username if user else "unknown",
        ip_address=ip,
        success=bool(user_id),
        user_id=user_id,
    )
    return {"status": "logged-out"}


@router.post("/password/forgot")
def forgot_password(req: PasswordForgotRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    _rate_limit("password-forgot", f"{ip}:{req.identity}", get_settings().password_reset_rate_limit)
    user = db.query(User).filter((User.username == req.identity) | (User.email == req.identity)).first()
    response = {"status": "accepted", "message": "If the account exists, a reset token has been issued."}
    if user and user.id != 1:
        raw_token = create_password_reset(db, user)
        audit_auth(db, event="password-reset-request", subject=user.username, ip_address=ip, success=True, user_id=user.id)
        if get_settings().password_reset_debug and ip in {"127.0.0.1", "::1", "testclient"}:
            response["reset_token"] = raw_token
    else:
        audit_auth(db, event="password-reset-request", subject=req.identity, ip_address=ip, success=False)
    return response


@router.post("/password/reset")
def reset_password(req: PasswordResetRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    ip = _client_ip(request)
    _rate_limit("password-reset", ip, get_settings().password_reset_rate_limit)
    user = consume_password_reset(db, req.reset_token, req.new_password)
    if not user:
        audit_auth(db, event="password-reset", subject="unknown", ip_address=ip, success=False, detail="invalid_token")
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    audit_auth(db, event="password-reset", subject=user.username, ip_address=ip, success=True, user_id=user.id)
    return {"status": "password-reset"}


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "username": user.username, "email": user.email}


@router.get("/audit")
def auth_audit(
    limit: int = Query(default=30, ge=1, le=100),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    rows = db.query(AuthAuditRecord).filter(
        AuthAuditRecord.user_id == user.id
    ).order_by(AuthAuditRecord.id.desc()).limit(limit).all()
    return {
        "events": [
            {
                "id": row.id,
                "event": row.event,
                "success": row.success,
                "ip_address": row.ip_address,
                "detail": row.detail,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


def _client_ip(request: Request) -> str:
    # Do not trust X-Forwarded-For unless a trusted reverse-proxy layer has
    # already normalised the peer address. Otherwise clients can bypass the
    # authentication rate limiter by supplying an arbitrary header.
    return request.client.host if request.client else "unknown"


def _rate_limit(action: str, identity: str, limit: int) -> None:
    try:
        auth_rate_limiter.check(action, identity, limit)
    except AuthRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
