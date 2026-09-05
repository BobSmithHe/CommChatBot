from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ...infra.config import get_settings
from ..database import RuntimeEventRecord, SessionLocal


@dataclass(frozen=True)
class RuntimeEventEnvelope:
    event_key: str
    task_id: str
    event_type: str
    content_json: str | None


class RedisRuntimeEventBus:
    """Low-latency Redis Stream transport with MySQL as durable history."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._redis = None
        self._retry_after = 0.0

    def publish(self, event: RuntimeEventEnvelope) -> str | None:
        client = self._client()
        if not client:
            return None
        try:
            return str(client.xadd(
                self._key(event.task_id),
                {
                    "event_key": event.event_key,
                    "event_type": event.event_type,
                    "content_json": event.content_json or "",
                },
                maxlen=max(200, self.settings.runtime_event_stream_maxlen),
                approximate=True,
            ))
        except Exception:
            self._mark_failed()
            return None

    def read(
        self,
        task_id: str,
        cursor: str = "0-0",
        *,
        block_ms: int = 1000,
        count: int = 200,
    ) -> tuple[str, list[dict[str, Any]]] | None:
        client = self._client()
        if not client:
            return None
        try:
            response = client.xread(
                {self._key(task_id): cursor},
                count=max(1, min(count, 500)),
                block=max(1, block_ms),
            )
        except Exception:
            self._mark_failed()
            return None
        events: list[dict[str, Any]] = []
        next_cursor = cursor
        for _stream, rows in response or []:
            for stream_id, fields in rows:
                next_cursor = str(stream_id)
                raw = fields.get("content_json") or ""
                try:
                    content = json.loads(raw) if raw else ""
                except json.JSONDecodeError:
                    content = raw
                events.append({
                    "event_key": fields.get("event_key") or "",
                    "event": fields.get("event_type") or "status",
                    "content": content,
                })
        return next_cursor, events

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
                    socket_timeout=2,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._mark_failed()
                return None
        return self._redis

    def _mark_failed(self) -> None:
        self._redis = False
        self._retry_after = time.monotonic() + 5

    def _key(self, task_id: str) -> str:
        return f"{self.settings.task_queue_name}:events:{task_id}"


class RuntimeEventBatchWriter:
    """Persist runtime events in short batches without blocking Agent turns."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._queue: queue.Queue[RuntimeEventEnvelope | threading.Event] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    def append(self, event: RuntimeEventEnvelope) -> None:
        # SQLite is primarily the deterministic test/single-process fallback.
        if self.settings.database_backend.casefold() == "sqlite":
            self._persist([event])
            return
        self._ensure_thread()
        self._queue.put(event)

    def flush(self, timeout: float = 3.0) -> bool:
        if not self._thread:
            return True
        marker = threading.Event()
        self._queue.put(marker)
        return marker.wait(timeout=max(0.1, timeout))

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="runtime-event-writer",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        batch: list[RuntimeEventEnvelope] = []
        max_batch = max(1, self.settings.runtime_event_batch_size)
        flush_seconds = max(0.01, self.settings.runtime_event_flush_ms / 1000)
        while not self._closed:
            try:
                item = self._queue.get(timeout=flush_seconds)
            except queue.Empty:
                item = None
            if isinstance(item, RuntimeEventEnvelope):
                batch.append(item)
            marker = item if isinstance(item, threading.Event) else None
            if batch and (len(batch) >= max_batch or item is None or marker is not None):
                if self._persist(batch):
                    batch.clear()
                else:
                    # Keep the batch in memory and retry. Redis continues to
                    # serve live readers while the durable store recovers.
                    time.sleep(min(1.0, flush_seconds * 2))
                    if marker is not None:
                        self._queue.put(marker)
                        marker = None
            if marker is not None:
                marker.set()

    @staticmethod
    def _persist(events: list[RuntimeEventEnvelope]) -> bool:
        if not events:
            return True
        try:
            with SessionLocal() as db:
                db.add_all([
                    RuntimeEventRecord(
                        event_key=item.event_key,
                        task_id=item.task_id,
                        event_type=item.event_type,
                        content_json=item.content_json,
                    )
                    for item in events
                ])
                db.commit()
            return True
        except Exception:
            logging.getLogger(__name__).exception("Runtime event batch persistence failed; retrying")
            return False


event_bus = RedisRuntimeEventBus()
event_batch_writer = RuntimeEventBatchWriter()
atexit.register(event_batch_writer.close)


def make_event(task_id: str, event_type: str, content: Any = None) -> RuntimeEventEnvelope:
    return RuntimeEventEnvelope(
        event_key=uuid.uuid4().hex,
        task_id=task_id,
        event_type=event_type,
        content_json=(
            json.dumps(content, ensure_ascii=False, default=str)
            if content is not None
            else None
        ),
    )
