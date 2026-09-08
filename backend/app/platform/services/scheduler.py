from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from ..database import Conversation, Message, ScheduledAutomationRecord, SessionLocal
from ..ports import ConversationAccessPort
from .task_runtime import RuntimeTaskManager, runtime_task_manager
from .task_queue import DurableTaskQueue, task_queue


def next_run_at(*, interval_seconds: int, rrule: str | None, timezone: str, after: datetime | None = None) -> datetime:
    """Return the next UTC-naive run time for an interval or RFC 5545 RRULE."""
    after_utc = after or datetime.utcnow()
    if not rrule:
        return after_utc + timedelta(seconds=max(60, interval_seconds))
    try:
        zone = ZoneInfo(timezone or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone}") from exc
    local_after = after_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(zone)
    try:
        rule = rrulestr(rrule.strip(), dtstart=local_after)
        occurrence = rule.after(local_after, inc=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid RRULE: {exc}") from exc
    if occurrence is None:
        raise ValueError("RRULE has no future occurrence")
    if occurrence.tzinfo is None:
        occurrence = occurrence.replace(tzinfo=zone)
    return occurrence.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


class AutomationScheduler:
    """Durable interval/RRULE scheduler; executions reuse the Agent task queue."""

    def __init__(
        self,
        conversations: ConversationAccessPort,
        *,
        task_manager: RuntimeTaskManager | None = None,
        queue: DurableTaskQueue | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self.conversations = conversations
        self.task_manager = task_manager or runtime_task_manager
        self.queue = queue or task_queue
        self.poll_seconds = max(0.5, poll_seconds)

    def tick(self) -> int:
        now = datetime.utcnow()
        with SessionLocal() as db:
            due = db.query(ScheduledAutomationRecord).filter(
                ScheduledAutomationRecord.status == "active",
                ScheduledAutomationRecord.next_run_at <= now,
            ).order_by(ScheduledAutomationRecord.next_run_at).limit(20).all()
            snapshots = []
            for row in due:
                snapshot = (row.id, row.user_id, row.conversation_id, row.prompt)
                next_status = "active"
                try:
                    following = next_run_at(
                        interval_seconds=row.interval_seconds,
                        rrule=row.rrule,
                        timezone=row.timezone or "UTC",
                        after=now,
                    )
                except ValueError:
                    # An exhausted finite RRULE executes its final due occurrence,
                    # then becomes paused instead of silently becoming an interval.
                    following = now
                    next_status = "paused"
                updated = db.query(ScheduledAutomationRecord).filter(
                    ScheduledAutomationRecord.id == row.id,
                    ScheduledAutomationRecord.status == "active",
                    ScheduledAutomationRecord.next_run_at <= now,
                ).update({
                    ScheduledAutomationRecord.last_run_at: now,
                    ScheduledAutomationRecord.next_run_at: following,
                    ScheduledAutomationRecord.status: next_status,
                }, synchronize_session=False)
                if updated:
                    snapshots.append(snapshot)
            db.commit()
        created = 0
        for automation_id, user_id, conversation_id, prompt in snapshots:
            try:
                with SessionLocal() as db:
                    conversation = db.query(Conversation).filter(
                        Conversation.id == conversation_id,
                        Conversation.user_id == user_id,
                    ).first()
                    if not conversation:
                        continue
                    mode = conversation.mode
                    project_trusted = bool(conversation.project_trusted)
                    history = self.conversations.history(db, conversation_id)
                    db.add(Message(user_id=user_id, conversation_id=conversation_id, role="user", content=prompt))
                    db.commit()
                    request = {
                        "model_message": prompt, "history": history, "use_rag": conversation.mode == "chatbot",
                        "use_web": False, "system_context": "This run was started by a scheduled automation.",
                        "project_trusted": project_trusted, "automation_id": automation_id,
                    }
                task = self.task_manager.create(
                    user_id=user_id, conversation_id=conversation_id, mode=mode,
                    prompt=prompt, status="queued", request=request,
                )
                with SessionLocal() as db:
                    row = db.query(ScheduledAutomationRecord).filter(ScheduledAutomationRecord.id == automation_id).first()
                    if row:
                        row.last_task_id = task.id
                        db.commit()
                self.queue.enqueue(task.id)
                created += 1
            except Exception:
                continue
        return created

    async def run(self) -> None:
        while True:
            await asyncio.to_thread(self.tick)
            await asyncio.sleep(self.poll_seconds)
