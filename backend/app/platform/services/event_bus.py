from __future__ import annotations

import atexit
import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...infra.config import get_settings
from ..database import RuntimeEventRecord, SessionLocal
from .lazy import LazyService


@dataclass(frozen=True)
class RuntimeEventEnvelope:
    event_key: str
    task_id: str
    event_type: str
    content_json: str | None


class RedisRuntimeEventBus:
    """Low-latency Redis Stream transport with MySQL as durable history."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()
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
    """Persist runtime events in short batches through a crash-safe local outbox.

    Database outages must not turn batching into event loss. MySQL writes are
    first recorded in a tiny SQLite outbox; SQLite writes use the same outbox
    as a retry fallback. Successfully committed rows are removed and pending
    rows are replayed automatically after a process restart.
    """

    def __init__(self, settings=None, *, spool_path: Path | None = None) -> None:
        self.settings = settings or get_settings()
        self._queue: queue.Queue[object | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._closed = False
        self._last_error_log = 0.0
        self._spool = _RuntimeEventSpool(
            spool_path or Path(self.settings.data_dir) / "runtime-event-outbox.sqlite3"
        )

    def append(self, event: RuntimeEventEnvelope) -> None:
        # SQLite is primarily the deterministic test/single-process fallback.
        if self._closed:
            raise RuntimeError("Runtime event writer is closed")
        if self.settings.database_backend.casefold() == "sqlite" and self._persist([event]):
            return
        self._spool.put(event)
        self._ensure_thread()
        self._queue.put(object())

    def flush(self, timeout: float = 3.0) -> bool:
        if self._spool.count() == 0:
            return True
        self._ensure_thread()
        deadline = time.monotonic() + max(0.1, timeout)
        self._queue.put(object())
        while time.monotonic() < deadline:
            if self._spool.count() == 0:
                return True
            time.sleep(0.025)
        return self._spool.count() == 0

    def close(self, timeout: float = 3.0) -> None:
        if self._closed:
            return
        flushed = self.flush(timeout=timeout)
        self._closed = True
        self._stop_event.set()
        self._queue.put(None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(0.1, min(timeout, 3.0)))
        if not flushed:
            logging.getLogger(__name__).warning(
                "Runtime event writer closed with %s durable outbox event(s); they will replay on restart",
                self._spool.count(),
            )

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
        max_batch = max(1, self.settings.runtime_event_batch_size)
        flush_seconds = max(0.01, self.settings.runtime_event_flush_ms / 1000)
        retry_delay = flush_seconds
        while not self._closed:
            try:
                item = self._queue.get(timeout=flush_seconds)
            except queue.Empty:
                item = object()
            if item is None:
                continue
            batch = self._spool.take(max_batch)
            if not batch:
                continue
            if self._persist(batch):
                self._spool.delete([event.event_key for event in batch])
                retry_delay = flush_seconds
            else:
                self._stop_event.wait(retry_delay)
                retry_delay = min(5.0, max(flush_seconds, retry_delay * 2))

    def _persist(self, events: list[RuntimeEventEnvelope]) -> bool:
        if not events:
            return True
        try:
            with SessionLocal() as db:
                keys = [item.event_key for item in events]
                existing = {
                    row[0] for row in db.query(RuntimeEventRecord.event_key).filter(
                        RuntimeEventRecord.event_key.in_(keys)
                    ).all()
                }
                db.add_all([
                    RuntimeEventRecord(
                        event_key=item.event_key,
                        task_id=item.task_id,
                        event_type=item.event_type,
                        content_json=item.content_json,
                    )
                    for item in events if item.event_key not in existing
                ])
                db.commit()
            return True
        except Exception:
            now = time.monotonic()
            if now - self._last_error_log >= 5:
                logging.getLogger(__name__).exception("Runtime event batch persistence failed; retrying")
                self._last_error_log = now
            return False


class _RuntimeEventSpool:
    """Small process-safe SQLite outbox shared by local API/worker processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "event_key TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                "event_type TEXT NOT NULL, content_json TEXT)"
            )

    def put(self, event: RuntimeEventEnvelope) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO events(event_key,task_id,event_type,content_json) VALUES (?,?,?,?)",
                (event.event_key, event.task_id, event.event_type, event.content_json),
            )

    def take(self, limit: int) -> list[RuntimeEventEnvelope]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT event_key,task_id,event_type,content_json FROM events ORDER BY rowid LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [RuntimeEventEnvelope(*row) for row in rows]

    def delete(self, event_keys: list[str]) -> None:
        if not event_keys:
            return
        with self._connect() as db:
            db.executemany("DELETE FROM events WHERE event_key=?", ((key,) for key in event_keys))

    def count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)


event_bus = LazyService(RedisRuntimeEventBus)
event_batch_writer = LazyService(RuntimeEventBatchWriter)
atexit.register(event_batch_writer.close_if_created)


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
