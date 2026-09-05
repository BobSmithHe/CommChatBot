from __future__ import annotations

import time

from ..infra.config import get_settings
from ..infra.database import AgentTask, SessionLocal


class DurableTaskQueue:
    """Redis wake-up queue with the database as the durable source of truth."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._redis = None

    def enqueue(self, task_id: str) -> None:
        client = self._client()
        if client:
            try:
                client.rpush(self.settings.task_queue_name, task_id)
            except Exception:
                self._redis = False

    def next(self, timeout: int = 2) -> str | None:
        client = self._client()
        if client:
            try:
                item = client.blpop(self.settings.task_queue_name, timeout=max(1, timeout))
                if item:
                    return str(item[1])
            except Exception:
                self._redis = False
        with SessionLocal() as db:
            row = db.query(AgentTask.id).filter(AgentTask.status == "queued").order_by(AgentTask.created_at).first()
            return str(row[0]) if row else None

    def wake_recovered(self) -> int:
        with SessionLocal() as db:
            ids = [str(row[0]) for row in db.query(AgentTask.id).filter(AgentTask.status == "queued").all()]
        for task_id in ids:
            self.enqueue(task_id)
        return len(ids)

    def wait_idle(self, seconds: float = 0.25) -> None:
        time.sleep(max(0.05, seconds))

    def _client(self):
        if self._redis is False:
            return None
        if self._redis is None:
            try:
                import redis

                client = redis.Redis(
                    host=self.settings.redis_host,
                    port=self.settings.redis_port,
                    password=self.settings.redis_password or None,
                    decode_responses=True,
                    socket_connect_timeout=1,
                    socket_timeout=3,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._redis = False
        return self._redis or None


task_queue = DurableTaskQueue()
