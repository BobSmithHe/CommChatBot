from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError

from ..infra.config import get_settings
from ..infra.database import (
    AgentTask,
    ApprovalRequestRecord,
    ModelUsageRecord,
    RuntimeEventRecord,
    SessionLocal,
    TaskMailboxMessage,
)
from ..infra.integrations import get_external_integrations


TaskStatus = Literal["running", "waiting-approval", "completed", "failed", "cancelled", "interrupted"]


class TaskCancelled(RuntimeError):
    pass


@dataclass
class LiveTask:
    id: str
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_cancel_check: float = 0.0
    cancelled_in_store: bool = False


@dataclass
class PendingApproval:
    id: str
    task_id: str
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[bool]


class RuntimeTaskManager:
    """Process-local controls backed by durable task/event rows."""

    def __init__(self) -> None:
        self._tasks: dict[str, LiveTask] = {}
        self._approvals: dict[str, PendingApproval] = {}

    def create(
        self, *, user_id: int, conversation_id: int, mode: str, prompt: str,
        status: str = "running", request: dict[str, Any] | None = None,
    ) -> LiveTask:
        task = LiveTask(uuid.uuid4().hex)
        self._tasks[task.id] = task
        with SessionLocal() as db:
            db.add(AgentTask(
                id=task.id,
                user_id=user_id,
                conversation_id=conversation_id,
                mode=mode,
                prompt=prompt,
                status=status,
                request_json=json.dumps(request, ensure_ascii=False, default=str) if request else None,
            ))
            db.commit()
        integrations = get_external_integrations()
        integrations.cache_task(task.id, {"status": status, "conversation_id": conversation_id, "mode": mode})
        integrations.audit_task(task_id=task.id, status=status, conversation_id=conversation_id, mode=mode)
        integrations.observe_task(task_id=task.id, name="task-start", input=prompt, metadata={"mode": mode, "conversation_id": conversation_id})
        return task

    def activate(self, task_id: str) -> LiveTask | None:
        with SessionLocal() as db:
            updated = db.query(AgentTask).filter(
                AgentTask.id == task_id, AgentTask.status == "queued"
            ).update({AgentTask.status: "running", AgentTask.error: None}, synchronize_session=False)
            db.commit()
        if not updated:
            return None
        task = LiveTask(task_id)
        self._tasks[task_id] = task
        return task

    def append_event(self, task_id: str, event_type: str, content: Any = None) -> None:
        with SessionLocal() as db:
            db.add(RuntimeEventRecord(
                task_id=task_id,
                event_type=event_type,
                content_json=json.dumps(content, ensure_ascii=False, default=str) if content is not None else None,
            ))
            db.commit()
        get_external_integrations().append_task_event(task_id, event_type, content)

    def enqueue_message(self, task_id: str, kind: str, content: str) -> dict[str, Any] | None:
        """Persist a steering/follow-up message for consumption at a turn boundary."""
        normalized_kind = kind.strip().casefold()
        if normalized_kind not in {"steering", "follow_up"} or not content.strip():
            return None
        with SessionLocal() as db:
            task = db.query(AgentTask).filter(AgentTask.id == task_id).first()
            if not task or task.status not in {"queued", "running", "waiting-approval"}:
                return None
            row = TaskMailboxMessage(
                task_id=task_id,
                kind=normalized_kind,
                content=content.strip(),
                status="pending",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return {"id": row.id, "kind": row.kind, "content": row.content}

    def consume_messages(self, task_id: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        """Claim pending mailbox rows in durable creation order."""
        try:
            with SessionLocal() as db:
                rows = (
                    db.query(TaskMailboxMessage)
                    .filter(
                        TaskMailboxMessage.task_id == task_id,
                        TaskMailboxMessage.status == "pending",
                        TaskMailboxMessage.kind.in_(kinds),
                    )
                    .order_by(TaskMailboxMessage.id)
                    .all()
                )
                now = datetime.utcnow()
                result = []
                for row in rows:
                    row.status = "consumed"
                    row.consumed_at = now
                    result.append({"id": row.id, "kind": row.kind, "content": row.content})
                if rows:
                    db.commit()
                return result
        except SQLAlchemyError:
            return []

    def record_usage(self, task_id: str | None, provider: str, model: str, usage: dict[str, Any]) -> None:
        if not task_id or not usage:
            return
        try:
            with SessionLocal() as db:
                task = db.query(AgentTask).filter(AgentTask.id == task_id).first()
                if not task:
                    return
                db.add(ModelUsageRecord(
                    task_id=task_id,
                    conversation_id=task.conversation_id,
                    provider=provider,
                    model=model,
                    input_tokens=max(0, int(usage.get("input_tokens") or 0)),
                    output_tokens=max(0, int(usage.get("output_tokens") or 0)),
                    cache_read_tokens=max(0, int(usage.get("cache_read_tokens") or 0)),
                    cache_write_tokens=max(0, int(usage.get("cache_write_tokens") or 0)),
                    cost_usd=max(0.0, float(usage.get("cost_usd") or 0.0)),
                ))
                db.commit()
        except SQLAlchemyError:
            return

    def save_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        encoded = json.dumps(checkpoint, ensure_ascii=False, default=str)
        with SessionLocal() as db:
            row = db.query(AgentTask).filter(AgentTask.id == task_id).first()
            if row:
                row.checkpoint_json = encoded
                row.updated_at = datetime.utcnow()
                db.commit()

    def load_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        with SessionLocal() as db:
            row = db.query(AgentTask).filter(AgentTask.id == task_id).first()
            if not row or not row.checkpoint_json:
                return None
            try:
                return json.loads(row.checkpoint_json)
            except (TypeError, json.JSONDecodeError):
                return None

    def resume(self, task_id: str) -> LiveTask | None:
        with SessionLocal() as db:
            row = db.query(AgentTask).filter(AgentTask.id == task_id).first()
            if not row or row.status not in {"failed", "cancelled", "interrupted"} or not row.checkpoint_json:
                return None
            row.status = "queued"
            row.error = None
            row.updated_at = datetime.utcnow()
            db.commit()
        task = LiveTask(task_id)
        self._tasks[task_id] = task
        integrations = get_external_integrations()
        integrations.cache_task(task_id, {"status": "queued", "resumed": True})
        integrations.audit_task(task_id=task_id, status="queued")
        integrations.observe_task(task_id=task_id, name="task-resume", metadata={"resumed": True})
        return task

    def finish(self, task_id: str, status: TaskStatus, error: str | None = None) -> None:
        with SessionLocal() as db:
            row = db.query(AgentTask).filter(AgentTask.id == task_id).first()
            if row:
                row.status = status
                row.error = error
                row.updated_at = datetime.utcnow()
                db.commit()
        self._tasks.pop(task_id, None)
        integrations = get_external_integrations()
        integrations.cache_task(task_id, {"status": status, "error": error})
        integrations.audit_task(task_id=task_id, status=status, error=error)
        integrations.observe_task(task_id=task_id, name="task-finish", output={"status": status}, error=error)

    def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task:
            task.cancel_event.set()
        with SessionLocal() as db:
            row = db.query(AgentTask).filter(AgentTask.id == task_id).first()
            if not row:
                return False
            if row.status in {"completed", "failed", "cancelled"}:
                return False
            row.status = "cancelled"
            row.updated_at = datetime.utcnow()
            db.commit()
        for approval in list(self._approvals.values()):
            if approval.task_id == task_id and not approval.future.done():
                approval.loop.call_soon_threadsafe(approval.future.set_result, False)
        return True

    def cancelled(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task and task.cancel_event.is_set():
            return True
        now = time.monotonic()
        if task and now - task.last_cancel_check < 0.25:
            return task.cancelled_in_store
        with SessionLocal() as db:
            row = db.query(AgentTask.status).filter(AgentTask.id == task_id).first()
            cancelled = bool(row and row[0] in {"cancelled", "cancel-requested"})
        if task:
            task.last_cancel_check = now
            task.cancelled_in_store = cancelled
        return cancelled

    def ensure_not_cancelled(self, task_id: str | None) -> None:
        if task_id and self.cancelled(task_id):
            raise TaskCancelled("Task cancelled by user")

    def create_approval(self, task_id: str, tool_name: str, tool_input: dict, capability: str) -> str:
        approval_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        pending = PendingApproval(approval_id, task_id, loop, loop.create_future())
        self._approvals[approval_id] = pending
        with SessionLocal() as db:
            task = db.query(AgentTask).filter(AgentTask.id == task_id).first()
            if task:
                task.status = "waiting-approval"
            db.add(ApprovalRequestRecord(
                id=approval_id,
                task_id=task_id,
                tool_name=tool_name,
                tool_input_json=json.dumps(tool_input, ensure_ascii=False),
                capability=capability,
                status="pending",
            ))
            db.commit()
        return approval_id

    async def wait_for_approval(self, approval_id: str) -> bool:
        pending = self._approvals.get(approval_id)
        if not pending:
            return False
        deadline = asyncio.get_running_loop().time() + max(5, get_settings().approval_timeout_seconds)
        try:
            while asyncio.get_running_loop().time() < deadline:
                if pending.future.done():
                    return bool(pending.future.result())
                with SessionLocal() as db:
                    row = db.query(ApprovalRequestRecord.status).filter(ApprovalRequestRecord.id == approval_id).first()
                if row and row[0] != "pending":
                    return row[0] == "approved"
                await asyncio.sleep(0.25)
            self.resolve_approval(approval_id, False, status="expired")
            return False
        finally:
            self._approvals.pop(approval_id, None)

    def resolve_approval(self, approval_id: str, approved: bool, *, status: str | None = None) -> bool:
        pending = self._approvals.get(approval_id)
        with SessionLocal() as db:
            row = db.query(ApprovalRequestRecord).filter(ApprovalRequestRecord.id == approval_id).first()
            if not row or row.status != "pending":
                return False
            row.status = status or ("approved" if approved else "rejected")
            row.resolved_at = datetime.utcnow()
            task = db.query(AgentTask).filter(AgentTask.id == row.task_id).first()
            if task and task.status == "waiting-approval":
                task.status = "running"
            db.commit()
        if pending and not pending.future.done():
            pending.loop.call_soon_threadsafe(pending.future.set_result, approved)
        return True

    @staticmethod
    def permission_action(permission_mode: str, capability: str) -> Literal["allow", "approve", "deny"]:
        if capability == "read":
            return "allow"
        if permission_mode == "full-access":
            return "allow"
        if permission_mode == "read-only":
            return "deny"
        if capability == "write":
            return "allow"
        return "approve"


runtime_task_manager = RuntimeTaskManager()
