from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta

from app.platform.database import RemoteJobRecord, RemoteRunnerRecord, SessionLocal
from app.infra.config import get_settings
from app.agent_runtime import Tool
from app.platform.services.lazy import LazyService


class RemoteExecutionService:
    """Durable broker for explicitly registered, pull-based remote runners."""

    ONLINE_SECONDS = 90

    def online_runners(self) -> list[dict]:
        cutoff = datetime.utcnow() - timedelta(seconds=self.ONLINE_SECONDS)
        with SessionLocal() as db:
            rows = db.query(RemoteRunnerRecord).filter(
                RemoteRunnerRecord.status == "online",
                RemoteRunnerRecord.last_seen_at >= cutoff,
            ).order_by(RemoteRunnerRecord.name).all()
            return [{
                "id": row.id, "name": row.name,
                "capabilities": json.loads(row.capabilities_json or "{}"),
                "last_seen_at": row.last_seen_at.isoformat(),
            } for row in rows]

    def create_job(self, *, user_id: int, runner_id: str, command: dict, task_id: str | None = None) -> str:
        online = {row["id"]: row for row in self.online_runners()}
        runner = online.get(runner_id)
        if runner is None:
            raise ValueError("Remote runner is offline or not found")
        if get_settings().remote_require_isolation and not runner["capabilities"].get("isolated"):
            raise ValueError("Remote runner does not advertise an isolated sandbox")
        job_id = uuid.uuid4().hex
        with SessionLocal() as db:
            db.add(RemoteJobRecord(
                id=job_id, user_id=user_id, runner_id=runner_id, task_id=task_id,
                command_json=json.dumps(command, ensure_ascii=False), status="queued",
            ))
            db.commit()
        return job_id

    async def wait(self, job_id: str, timeout: int) -> str:
        deadline = asyncio.get_running_loop().time() + max(1, min(timeout, 3600))
        while asyncio.get_running_loop().time() < deadline:
            with SessionLocal() as db:
                row = db.query(RemoteJobRecord).filter(RemoteJobRecord.id == job_id).first()
                if not row:
                    raise ValueError("Remote job not found")
                if row.status in {"completed", "failed", "cancelled"}:
                    return json.dumps({
                        "job_id": row.id, "status": row.status, "exit_code": row.exit_code,
                        "output": row.output or "", "timed_out": False,
                    }, ensure_ascii=False)
            await asyncio.sleep(0.5)
        with SessionLocal() as db:
            row = db.query(RemoteJobRecord).filter(RemoteJobRecord.id == job_id).first()
            if row and row.status in {"queued", "running"}:
                row.status = "cancelled"
                row.cancel_requested = True
                row.finished_at = datetime.utcnow()
                db.commit()
        return json.dumps({"job_id": job_id, "exit_code": -1, "timed_out": True, "output": "Remote job timed out"})

    def tools(self, *, user_id: int | None, task_id: str | None) -> list[Tool]:
        def list_remote_runners() -> str:
            return json.dumps(self.online_runners(), ensure_ascii=False)

        async def run_remote_command(runner_id: str, argv: list[str], timeout: int = 300) -> str:
            if user_id is None:
                raise ValueError("Remote execution requires an authenticated user")
            if not argv or not all(isinstance(item, str) and item for item in argv):
                raise ValueError("argv must be a non-empty string array")
            job_id = self.create_job(
                user_id=user_id, runner_id=runner_id,
                command={"argv": argv, "timeout": max(1, min(timeout, 3600))}, task_id=task_id,
            )
            return await self.wait(job_id, timeout + 10)

        return [
            Tool("list_remote_runners", "List remote execution runners that recently sent a heartbeat.", {
                "type": "object", "properties": {},
            }, list_remote_runners),
            Tool("run_remote_command", "Run one argv-based command on a registered remote runner and wait for its durable result.", {
                "type": "object", "properties": {
                    "runner_id": {"type": "string"},
                    "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
                }, "required": ["runner_id", "argv"],
            }, run_remote_command, capability="execute"),
        ]


remote_execution_service = LazyService(RemoteExecutionService)
