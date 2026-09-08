from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlparse

import httpx

from ...infra.config import get_settings
from ..database import (
    NotificationDeliveryRecord,
    NotificationEndpointRecord,
    NotificationRecord,
    SessionLocal,
)
from .lazy import LazyService


def validate_endpoint(kind: str, target: str) -> str:
    value = target.strip()
    if kind == "email":
        if "@" not in value or len(value) > 320:
            raise ValueError("Invalid email destination")
        return value
    if kind != "webhook":
        raise ValueError("Notification endpoint kind must be webhook or email")
    parsed = urlparse(value)
    allowed = {"https"}
    if get_settings().notification_allow_http:
        allowed.add("http")
    if parsed.scheme.casefold() not in allowed or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Webhook must be an HTTPS URL")
    return value


def enqueue_notification_deliveries(db, notification: NotificationRecord) -> int:
    """Create durable outbox rows in the same transaction as a notification."""
    endpoints = db.query(NotificationEndpointRecord).filter(
        NotificationEndpointRecord.user_id == notification.user_id,
        NotificationEndpointRecord.enabled.is_(True),
    ).all()
    for endpoint in endpoints:
        db.add(NotificationDeliveryRecord(
            id=uuid.uuid4().hex,
            notification_id=notification.id,
            endpoint_id=endpoint.id,
            status="queued",
            next_attempt_at=datetime.utcnow(),
        ))
    return len(endpoints)


class NotificationDispatcher:
    """Durable webhook/email outbox with bounded exponential retry."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()

    async def tick(self) -> int:
        now = datetime.utcnow()
        with SessionLocal() as db:
            db.query(NotificationDeliveryRecord).filter(
                NotificationDeliveryRecord.status == "sending",
                NotificationDeliveryRecord.updated_at <= now - timedelta(minutes=5),
            ).update({
                NotificationDeliveryRecord.status: "retry",
                NotificationDeliveryRecord.next_attempt_at: now,
                NotificationDeliveryRecord.last_error: "Recovered interrupted notification delivery",
                NotificationDeliveryRecord.updated_at: now,
            }, synchronize_session=False)
            db.commit()
            rows = db.query(NotificationDeliveryRecord).filter(
                NotificationDeliveryRecord.status.in_(["queued", "retry"]),
                NotificationDeliveryRecord.next_attempt_at <= now,
            ).order_by(NotificationDeliveryRecord.next_attempt_at).limit(20).all()
            ids = [row.id for row in rows]
        delivered = 0
        for delivery_id in ids:
            if await self._deliver(delivery_id):
                delivered += 1
        return delivered

    async def _deliver(self, delivery_id: str) -> bool:
        with SessionLocal() as db:
            claimed = db.query(NotificationDeliveryRecord).filter(
                NotificationDeliveryRecord.id == delivery_id,
                NotificationDeliveryRecord.status.in_(["queued", "retry"]),
            ).update({
                NotificationDeliveryRecord.status: "sending",
                NotificationDeliveryRecord.attempts: NotificationDeliveryRecord.attempts + 1,
                NotificationDeliveryRecord.updated_at: datetime.utcnow(),
            }, synchronize_session=False)
            db.commit()
            if not claimed:
                return False
            delivery = db.query(NotificationDeliveryRecord).filter(
                NotificationDeliveryRecord.id == delivery_id,
            ).first()
            endpoint = db.query(NotificationEndpointRecord).filter(
                NotificationEndpointRecord.id == delivery.endpoint_id,
                NotificationEndpointRecord.enabled.is_(True),
            ).first()
            notification = db.query(NotificationRecord).filter(
                NotificationRecord.id == delivery.notification_id,
            ).first()
            if not endpoint or not notification:
                delivery.status = "failed"
                delivery.last_error = "Notification endpoint or notification no longer exists"
                db.commit()
                return False
            payload = {
                "id": notification.id, "kind": notification.kind,
                "title": notification.title, "content": notification.content,
                "task_id": notification.task_id,
                "created_at": notification.created_at.isoformat() if notification.created_at else None,
            }
            kind, target, secret = endpoint.kind, endpoint.target, endpoint.secret or ""
        try:
            if kind == "webhook":
                await self._send_webhook(target, secret, payload)
            else:
                await asyncio.to_thread(self._send_email, target, payload)
        except (httpx.HTTPError, OSError, smtplib.SMTPException, ValueError) as exc:
            with SessionLocal() as db:
                row = db.query(NotificationDeliveryRecord).filter(NotificationDeliveryRecord.id == delivery_id).first()
                if row:
                    maximum = max(1, self.settings.notification_max_attempts)
                    row.status = "failed" if row.attempts >= maximum else "retry"
                    row.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(3600, 2 ** row.attempts * 5))
                    row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                    db.commit()
            return False
        with SessionLocal() as db:
            row = db.query(NotificationDeliveryRecord).filter(NotificationDeliveryRecord.id == delivery_id).first()
            if row:
                row.status = "delivered"
                row.delivered_at = datetime.utcnow()
                row.last_error = None
                db.commit()
        return True

    @staticmethod
    async def _send_webhook(target: str, secret: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "CommChatBot-Notifier/1"}
        if secret:
            headers["X-CommChat-Signature-256"] = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256,
            ).hexdigest()
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.post(target, content=body, headers=headers)
        response.raise_for_status()

    @staticmethod
    def _send_email(target: str, payload: dict) -> None:
        settings = get_settings()
        if not settings.smtp_host or not settings.smtp_from:
            raise ValueError("SMTP_HOST and SMTP_FROM are required for email notifications")
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = target
        message["Subject"] = str(payload["title"])
        message.set_content(str(payload["content"]))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as client:
            if settings.smtp_starttls:
                client.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(message)

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(max(0.5, self.settings.notification_poll_seconds))


notification_dispatcher = LazyService(NotificationDispatcher)
