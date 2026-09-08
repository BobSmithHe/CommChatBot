from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...bootstrap import get_container
from ...platform.services.scheduler import next_run_at
from ...platform.services.notifications import enqueue_notification_deliveries, validate_endpoint
from ...infra.config import get_settings
from ...platform.database import (
    Conversation, GitHubSubscriptionRecord, GitHubWebhookEventRecord,
    NotificationDeliveryRecord, NotificationEndpointRecord, NotificationRecord,
    RemoteJobRecord, RemoteRunnerRecord, ScheduledAutomationRecord, User, get_db,
)
from ..deps import current_user
from ..schemas import (
    AutomationCreateRequest, AutomationUpdateRequest, RemoteJobCompleteRequest, RemoteJobLeaseRequest,
    GitHubInlineReviewRequest, GitHubSubscriptionRequest,
    NotificationEndpointCreateRequest, NotificationEndpointUpdateRequest,
    RemoteJobCreateRequest, RemoteRunnerHeartbeatRequest,
)

router = APIRouter(prefix="/api/platform", tags=["platform"])


def _automation_payload(row: ScheduledAutomationRecord) -> dict:
    return {
        "id": row.id, "conversation_id": row.conversation_id, "name": row.name,
        "prompt": row.prompt, "interval_seconds": row.interval_seconds, "status": row.status,
        "rrule": row.rrule, "timezone": row.timezone or "UTC",
        "next_run_at": row.next_run_at.isoformat() if row.next_run_at else None,
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_task_id": row.last_task_id, "notification_policy": row.notification_policy,
    }


@router.get("/automations")
def list_automations(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(ScheduledAutomationRecord).filter(
        ScheduledAutomationRecord.user_id == user.id
    ).order_by(ScheduledAutomationRecord.created_at.desc()).all()
    return [_automation_payload(row) for row in rows]


@router.post("/automations")
def create_automation(req: AutomationCreateRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    conversation = db.query(Conversation).filter(
        Conversation.id == req.conversation_id, Conversation.user_id == user.id,
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        next_at = next_run_at(
            interval_seconds=req.interval_seconds, rrule=req.rrule,
            timezone=req.timezone, after=datetime.utcnow(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = ScheduledAutomationRecord(
        id=uuid.uuid4().hex, user_id=user.id, conversation_id=conversation.id,
        name=req.name, prompt=req.prompt, interval_seconds=req.interval_seconds,
        rrule=req.rrule or None, timezone=req.timezone,
        notification_policy=req.notification_policy, next_run_at=next_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _automation_payload(row)


@router.patch("/automations/{automation_id}")
def update_automation(automation_id: str, req: AutomationUpdateRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.query(ScheduledAutomationRecord).filter(
        ScheduledAutomationRecord.id == automation_id, ScheduledAutomationRecord.user_id == user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Automation not found")
    values = {
        key: value for key, value in req.model_dump(exclude_unset=True).items()
        if value is not None or key == "rrule"
    }
    for key, value in values.items():
        setattr(row, key, value)
    if {"interval_seconds", "rrule", "timezone"} & values.keys():
        try:
            row.next_run_at = next_run_at(
                interval_seconds=row.interval_seconds, rrule=row.rrule,
                timezone=row.timezone or "UTC", after=datetime.utcnow(),
            )
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return _automation_payload(row)


@router.delete("/automations/{automation_id}")
def delete_automation(automation_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    deleted = db.query(ScheduledAutomationRecord).filter(
        ScheduledAutomationRecord.id == automation_id, ScheduledAutomationRecord.user_id == user.id,
    ).delete(synchronize_session=False)
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="Automation not found")
    return {"deleted": True}


@router.post("/automations/{automation_id}/run")
def run_automation_now(automation_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.query(ScheduledAutomationRecord).filter(
        ScheduledAutomationRecord.id == automation_id, ScheduledAutomationRecord.user_id == user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Automation not found")
    row.status = "active"
    row.next_run_at = datetime.utcnow()
    db.commit()
    created = get_container().automation_scheduler.tick()
    db.refresh(row)
    return {"queued": created > 0, "task_id": row.last_task_id}


@router.get("/notifications")
def list_notifications(unread_only: bool = Query(default=False), user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    query = db.query(NotificationRecord).filter(NotificationRecord.user_id == user.id)
    if unread_only:
        query = query.filter(NotificationRecord.read_at.is_(None))
    return [{
        "id": row.id, "kind": row.kind, "title": row.title, "content": row.content,
        "task_id": row.task_id, "read": row.read_at is not None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    } for row in query.order_by(NotificationRecord.created_at.desc()).limit(100).all()]


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.query(NotificationRecord).filter(
        NotificationRecord.id == notification_id, NotificationRecord.user_id == user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.read_at = datetime.utcnow()
    db.commit()
    return {"read": True}


def _endpoint_payload(row: NotificationEndpointRecord) -> dict:
    return {
        "id": row.id, "kind": row.kind, "name": row.name, "target": row.target,
        "enabled": row.enabled, "has_secret": bool(row.secret),
    }


@router.get("/notification-endpoints")
def list_notification_endpoints(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(NotificationEndpointRecord).filter(
        NotificationEndpointRecord.user_id == user.id,
    ).order_by(NotificationEndpointRecord.created_at).all()
    return [_endpoint_payload(row) for row in rows]


@router.post("/notification-endpoints")
def create_notification_endpoint(req: NotificationEndpointCreateRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    try:
        target = validate_endpoint(req.kind, req.target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row = NotificationEndpointRecord(
        id=uuid.uuid4().hex, user_id=user.id, kind=req.kind,
        name=req.name, target=target, secret=req.secret or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _endpoint_payload(row)


@router.patch("/notification-endpoints/{endpoint_id}")
def update_notification_endpoint(endpoint_id: str, req: NotificationEndpointUpdateRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.query(NotificationEndpointRecord).filter(
        NotificationEndpointRecord.id == endpoint_id, NotificationEndpointRecord.user_id == user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification endpoint not found")
    values = req.model_dump(exclude_none=True)
    if "target" in values:
        try:
            values["target"] = validate_endpoint(row.kind, values["target"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    for key, value in values.items():
        setattr(row, key, value or None if key == "secret" else value)
    db.commit()
    db.refresh(row)
    return _endpoint_payload(row)


@router.delete("/notification-endpoints/{endpoint_id}")
def delete_notification_endpoint(endpoint_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    row = db.query(NotificationEndpointRecord).filter(
        NotificationEndpointRecord.id == endpoint_id, NotificationEndpointRecord.user_id == user.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notification endpoint not found")
    db.query(NotificationDeliveryRecord).filter(NotificationDeliveryRecord.endpoint_id == row.id).delete(synchronize_session=False)
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.get("/notification-deliveries")
def list_notification_deliveries(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(NotificationDeliveryRecord, NotificationEndpointRecord).join(
        NotificationEndpointRecord, NotificationEndpointRecord.id == NotificationDeliveryRecord.endpoint_id,
    ).filter(NotificationEndpointRecord.user_id == user.id).order_by(
        NotificationDeliveryRecord.created_at.desc(),
    ).limit(100).all()
    return [{
        "id": delivery.id, "endpoint": endpoint.name, "kind": endpoint.kind,
        "status": delivery.status, "attempts": delivery.attempts,
        "last_error": delivery.last_error,
    } for delivery, endpoint in rows]


@router.get("/remote/runners")
def list_remote_runners(_user: User = Depends(current_user)) -> list[dict]:
    return get_container().remote_execution_service.online_runners()


@router.post("/remote/jobs")
def create_remote_job(req: RemoteJobCreateRequest, user: User = Depends(current_user)) -> dict:
    try:
        job_id = get_container().remote_execution_service.create_job(
            user_id=user.id, runner_id=req.runner_id,
            command={"argv": req.argv, "timeout": req.timeout},
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_id": job_id, "status": "queued"}


@router.get("/remote/jobs")
def list_remote_jobs(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(RemoteJobRecord).filter(RemoteJobRecord.user_id == user.id).order_by(RemoteJobRecord.created_at.desc()).limit(100).all()
    return [{
        "id": row.id, "runner_id": row.runner_id, "task_id": row.task_id,
        "status": row.status, "exit_code": row.exit_code, "output": row.output,
        "attempts": row.attempts, "cancel_requested": row.cancel_requested,
        "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
    } for row in rows]


def _require_runner_token(token: str | None) -> None:
    configured = get_settings().remote_runner_token
    if not configured or not token or not secrets.compare_digest(configured, token):
        raise HTTPException(status_code=401, detail="Invalid runner token")


@router.post("/remote/runner/heartbeat")
def runner_heartbeat(req: RemoteRunnerHeartbeatRequest, x_runner_token: str | None = Header(default=None), db: Session = Depends(get_db)) -> dict:
    _require_runner_token(x_runner_token)
    runner_id = req.runner_id or uuid.uuid4().hex
    row = db.query(RemoteRunnerRecord).filter(RemoteRunnerRecord.id == runner_id).first()
    if not row:
        row = RemoteRunnerRecord(id=runner_id, name=req.name)
        db.add(row)
    row.name = req.name
    row.capabilities_json = json.dumps(req.capabilities, ensure_ascii=False)
    row.status = "online"
    row.last_seen_at = datetime.utcnow()
    db.commit()
    return {"runner_id": runner_id, "heartbeat_seconds": 30}


@router.post("/remote/runner/jobs/claim")
def claim_remote_job(runner_id: str, x_runner_token: str | None = Header(default=None), db: Session = Depends(get_db)) -> dict:
    _require_runner_token(x_runner_token)
    now = datetime.utcnow()
    max_attempts = max(1, get_settings().remote_job_max_attempts)
    # Do not leave poison jobs permanently in ``running`` after every runner
    # assigned to them has disappeared.
    db.query(RemoteJobRecord).filter(
        RemoteJobRecord.runner_id == runner_id,
        RemoteJobRecord.status == "running",
        RemoteJobRecord.lease_expires_at < now,
        RemoteJobRecord.attempts >= max_attempts,
    ).update({
        RemoteJobRecord.status: "failed",
        RemoteJobRecord.output: "Remote runner lease expired too many times",
        RemoteJobRecord.exit_code: -1,
        RemoteJobRecord.finished_at: now,
        RemoteJobRecord.lease_expires_at: None,
    }, synchronize_session=False)
    db.commit()
    row = db.query(RemoteJobRecord).filter(
        RemoteJobRecord.runner_id == runner_id,
        RemoteJobRecord.attempts < max_attempts,
        or_(
            RemoteJobRecord.status == "queued",
            and_(
                RemoteJobRecord.status == "running",
                RemoteJobRecord.lease_expires_at.is_not(None),
                RemoteJobRecord.lease_expires_at < now,
            ),
        ),
    ).order_by(RemoteJobRecord.created_at).first()
    if not row:
        return {"job": None}
    previous_status = row.status
    previous_lease = row.lease_token
    previous_attempts = int(row.attempts or 0)
    lease_token = secrets.token_urlsafe(32)
    lease_expires_at = now + timedelta(seconds=45)
    claim_query = db.query(RemoteJobRecord).filter(
        RemoteJobRecord.id == row.id,
        RemoteJobRecord.status == previous_status,
        RemoteJobRecord.lease_token == previous_lease if previous_lease is not None else RemoteJobRecord.lease_token.is_(None),
    )
    if previous_status == "running":
        # A heartbeat may have renewed the lease after the candidate SELECT.
        # Re-check expiry in the compare-and-swap so a live runner cannot have
        # its job stolen by a concurrent claim request.
        claim_query = claim_query.filter(RemoteJobRecord.lease_expires_at < now)
    updated = claim_query.update({
        RemoteJobRecord.status: "running",
        RemoteJobRecord.lease_token: lease_token,
        RemoteJobRecord.lease_expires_at: lease_expires_at,
        RemoteJobRecord.attempts: RemoteJobRecord.attempts + 1,
        RemoteJobRecord.cancel_requested: False,
        RemoteJobRecord.started_at: now,
    }, synchronize_session=False)
    db.commit()
    if not updated:
        return {"job": None}
    return {"job": {
        "id": row.id, "lease_token": lease_token,
        "lease_seconds": 45, "attempt": previous_attempts + 1,
        **json.loads(row.command_json),
    }}


@router.post("/remote/runner/jobs/{job_id}/heartbeat")
def heartbeat_remote_job(
    job_id: str, req: RemoteJobLeaseRequest,
    x_runner_token: str | None = Header(default=None), db: Session = Depends(get_db),
) -> dict:
    _require_runner_token(x_runner_token)
    row = db.query(RemoteJobRecord).filter(
        RemoteJobRecord.id == job_id,
        RemoteJobRecord.runner_id == req.runner_id,
        RemoteJobRecord.lease_token == req.lease_token,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Remote job lease not found")
    if row.status != "running" or row.cancel_requested:
        return {"status": row.status, "action": "cancel"}
    if row.lease_expires_at is None or row.lease_expires_at < datetime.utcnow():
        raise HTTPException(status_code=409, detail="Remote job lease expired")
    heartbeat_at = datetime.utcnow()
    row.lease_expires_at = heartbeat_at + timedelta(seconds=45)
    db.query(RemoteRunnerRecord).filter(RemoteRunnerRecord.id == req.runner_id).update({
        RemoteRunnerRecord.last_seen_at: heartbeat_at,
        RemoteRunnerRecord.status: "online",
    }, synchronize_session=False)
    db.commit()
    return {"status": "running", "action": "continue", "lease_seconds": 45}


@router.post("/remote/runner/jobs/{job_id}/complete")
def complete_remote_job(job_id: str, req: RemoteJobCompleteRequest, x_runner_token: str | None = Header(default=None), db: Session = Depends(get_db)) -> dict:
    _require_runner_token(x_runner_token)
    row = db.query(RemoteJobRecord).filter(
        RemoteJobRecord.id == job_id,
        RemoteJobRecord.runner_id == req.runner_id,
        RemoteJobRecord.lease_token == req.lease_token,
        RemoteJobRecord.status == "running",
        RemoteJobRecord.lease_expires_at >= datetime.utcnow(),
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Running remote job not found")
    if row.cancel_requested:
        raise HTTPException(status_code=409, detail="Remote job was cancelled")
    row.output = req.output
    row.exit_code = req.exit_code
    row.status = "completed" if req.exit_code == 0 else "failed"
    row.finished_at = datetime.utcnow()
    row.lease_expires_at = None
    db.query(RemoteRunnerRecord).filter(RemoteRunnerRecord.id == req.runner_id).update({
        RemoteRunnerRecord.last_seen_at: row.finished_at,
        RemoteRunnerRecord.status: "online",
    }, synchronize_session=False)
    db.commit()
    return {"status": row.status}


@router.get("/github/subscriptions")
def list_github_subscriptions(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(GitHubSubscriptionRecord).filter(
        GitHubSubscriptionRecord.user_id == user.id,
    ).order_by(GitHubSubscriptionRecord.repository).all()
    return [{"id": row.id, "repository": row.repository, "enabled": row.enabled} for row in rows]


@router.post("/github/subscriptions")
def create_github_subscription(req: GitHubSubscriptionRequest, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    repository = req.repository.casefold()
    existing = db.query(GitHubSubscriptionRecord).filter(
        GitHubSubscriptionRecord.user_id == user.id,
        GitHubSubscriptionRecord.repository == repository,
    ).first()
    if existing:
        existing.enabled = True
        db.commit()
        return {"id": existing.id, "repository": existing.repository, "enabled": True}
    row = GitHubSubscriptionRecord(id=uuid.uuid4().hex, user_id=user.id, repository=repository)
    db.add(row)
    db.commit()
    return {"id": row.id, "repository": row.repository, "enabled": True}


@router.delete("/github/subscriptions/{subscription_id}")
def delete_github_subscription(subscription_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    deleted = db.query(GitHubSubscriptionRecord).filter(
        GitHubSubscriptionRecord.id == subscription_id,
        GitHubSubscriptionRecord.user_id == user.id,
    ).delete(synchronize_session=False)
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="GitHub subscription not found")
    return {"deleted": True}


@router.post("/github/reviews")
async def submit_github_review(req: GitHubInlineReviewRequest, _user: User = Depends(current_user)) -> dict:
    try:
        result = await get_container().github_integration.submit_review(
            req.repository, number=req.number, body=req.body, event=req.event,
            commit_id=req.commit_id, comments=req.comments,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return json.loads(result)


@router.post("/github/webhook")
async def github_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    secret = get_settings().github_webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="GitHub webhook is not configured")
    body = await request.body()
    if len(body) > 5_000_000:
        raise HTTPException(status_code=413, detail="GitHub webhook payload is too large")
    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")
    delivery_id = request.headers.get("X-GitHub-Delivery") or hashlib.sha256(body).hexdigest()
    event_type = request.headers.get("X-GitHub-Event", "unknown")[:80]
    if db.query(GitHubWebhookEventRecord.id).filter(
        GitHubWebhookEventRecord.delivery_id == delivery_id,
    ).first():
        return {"accepted": True, "duplicate": True}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    repository = str(payload.get("repository", {}).get("full_name") or "").casefold()
    action = str(payload.get("action") or "")[:80]
    db.add(GitHubWebhookEventRecord(
        id=uuid.uuid4().hex, delivery_id=delivery_id[:100], event_type=event_type,
        repository=repository or None, action=action or None,
        payload_json=json.dumps(payload, ensure_ascii=False),
    ))
    subscriptions = db.query(GitHubSubscriptionRecord).filter(
        GitHubSubscriptionRecord.repository == repository,
        GitHubSubscriptionRecord.enabled.is_(True),
    ).all() if repository else []
    number = payload.get("pull_request", {}).get("number") or payload.get("number")
    for subscription in subscriptions:
        notification = NotificationRecord(
            id=uuid.uuid4().hex, user_id=subscription.user_id, kind="github",
            title=f"GitHub {event_type}: {repository}",
            content=f"{action or 'event'}" + (f" · PR #{number}" if number else ""),
        )
        db.add(notification)
        enqueue_notification_deliveries(db, notification)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"accepted": True, "duplicate": True}
    return {"accepted": True, "notifications": len(subscriptions)}
