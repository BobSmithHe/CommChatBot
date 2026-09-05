from __future__ import annotations

import asyncio
import time
import uuid

from app.infra.config import get_settings
from app.platform.database import MemoryExtractionJob, SessionLocal
from .service import memory_service
from app.platform.services.task_runtime import runtime_task_manager


class DurableMemoryJobQueue:
    """Post-response memory enrichment queue; model latency is off the answer path."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._redis = None
        self._retry_after = 0.0

    def enqueue(
        self,
        *,
        task_id: str,
        user_id: int,
        conversation_id: int,
        mode: str,
        user_text: str,
        assistant_text: str,
        project_identity: str | None,
    ) -> str | None:
        settings = memory_service.get_settings(user_id)
        if not settings["enabled"] or not settings["auto_capture"]:
            return None
        job_id = uuid.uuid4().hex
        with SessionLocal() as db:
            existing = db.query(MemoryExtractionJob.id).filter(
                MemoryExtractionJob.task_id == task_id
            ).first()
            if existing:
                return str(existing[0])
            db.add(MemoryExtractionJob(
                id=job_id,
                task_id=task_id,
                user_id=user_id,
                conversation_id=conversation_id,
                mode=mode,
                user_text=user_text[:8000],
                assistant_text=assistant_text[:8000],
                project_identity=(project_identity or "")[:500] or None,
                status="queued",
            ))
            db.commit()
        client = self._client()
        if client:
            try:
                client.rpush(self.settings.memory_job_queue_name, job_id)
            except Exception:
                self._failed()
        return job_id

    def next(self, timeout: int = 2) -> str | None:
        client = self._client()
        if client:
            try:
                item = client.blpop(self.settings.memory_job_queue_name, timeout=max(1, timeout))
                if item:
                    return str(item[1])
            except Exception:
                self._failed()
        with SessionLocal() as db:
            row = db.query(MemoryExtractionJob.id).filter(
                MemoryExtractionJob.status == "queued"
            ).order_by(MemoryExtractionJob.created_at).first()
            return str(row[0]) if row else None

    def wake_recovered(self) -> int:
        with SessionLocal() as db:
            rows = db.query(MemoryExtractionJob).filter(
                MemoryExtractionJob.status == "running"
            ).all()
            for row in rows:
                row.status = "queued"
            ids = [str(row[0]) for row in db.query(MemoryExtractionJob.id).filter(
                MemoryExtractionJob.status == "queued"
            ).all()]
            db.commit()
        client = self._client()
        if client:
            for job_id in ids:
                try:
                    client.rpush(self.settings.memory_job_queue_name, job_id)
                except Exception:
                    self._failed()
                    break
        return len(ids)

    def _client(self):
        if self._redis is False and time.monotonic() < self._retry_after:
            return None
        if self._redis is None or self._redis is False:
            try:
                import redis

                client = redis.Redis(
                    host=self.settings.redis_host,
                    port=self.settings.redis_port,
                    password=self.settings.redis_password or None,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=3,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._failed()
                return None
        return self._redis

    def _failed(self) -> None:
        self._redis = False
        self._retry_after = time.monotonic() + 5


async def execute_memory_job(job_id: str) -> bool:
    with SessionLocal() as db:
        updated = db.query(MemoryExtractionJob).filter(
            MemoryExtractionJob.id == job_id,
            MemoryExtractionJob.status == "queued",
        ).update({
            MemoryExtractionJob.status: "running",
            MemoryExtractionJob.attempts: MemoryExtractionJob.attempts + 1,
            MemoryExtractionJob.error: None,
        }, synchronize_session=False)
        db.commit()
    if not updated:
        return False

    with SessionLocal() as db:
        job = db.query(MemoryExtractionJob).filter(MemoryExtractionJob.id == job_id).first()
        if not job:
            return False
        snapshot = {
            "task_id": job.task_id,
            "user_id": job.user_id,
            "conversation_id": job.conversation_id,
            "mode": job.mode,
            "user_text": job.user_text,
            "assistant_text": job.assistant_text,
            "project_identity": job.project_identity,
            "attempts": job.attempts,
        }
    try:
        from ...services import get_chat_model_provider, get_coding_model_provider

        settings = get_settings()
        coding = snapshot["mode"] == "coding-agent"
        provider = get_coding_model_provider() if coding else get_chat_model_provider()
        model = (
            settings.coding_model_id if coding else settings.chat_model_id
        ) or getattr(provider, "default_model", "") or settings.deepseek_model
        memories, usage = await asyncio.wait_for(
            memory_service.auto_extract(
                provider=provider,
                model=model,
                user_id=snapshot["user_id"],
                user_text=snapshot["user_text"],
                assistant_text=snapshot["assistant_text"],
                conversation_id=snapshot["conversation_id"],
                task_id=snapshot["task_id"],
                project_identity=snapshot["project_identity"],
            ),
            timeout=min(30.0, max(5.0, settings.model_request_timeout_seconds)),
        )
        runtime_task_manager.record_usage(
            snapshot["task_id"],
            str(getattr(provider, "provider_name", type(provider).__name__)),
            model,
            usage,
        )
        if memories:
            runtime_task_manager.append_event(snapshot["task_id"], "memory_updated", {
                "count": len(memories),
                "memories": [
                    {"id": item["id"], "scope": item["scope"], "key": item["key"]}
                    for item in memories
                ],
            })
        with SessionLocal() as db:
            db.query(MemoryExtractionJob).filter(MemoryExtractionJob.id == job_id).update({
                MemoryExtractionJob.status: "completed",
                MemoryExtractionJob.error: None,
            }, synchronize_session=False)
            db.commit()
        return True
    except Exception as exc:
        with SessionLocal() as db:
            row = db.query(MemoryExtractionJob).filter(MemoryExtractionJob.id == job_id).first()
            if row:
                row.error = f"{type(exc).__name__}: {exc}"[:2000]
                row.status = "queued" if row.attempts < 3 else "failed"
                db.commit()
                if row.status == "queued":
                    client = memory_job_queue._client()
                    if client:
                        try:
                            client.rpush(memory_job_queue.settings.memory_job_queue_name, job_id)
                        except Exception:
                            memory_job_queue._failed()
        return False


memory_job_queue = DurableMemoryJobQueue()
