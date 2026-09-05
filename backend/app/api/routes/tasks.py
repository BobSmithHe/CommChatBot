from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...platform.services.task_runtime import runtime_task_manager
from ...platform.database import (
    AgentSubtaskRecord,
    AgentTask,
    ApprovalRequestRecord,
    ModelUsageRecord,
    RuntimeEventRecord,
    User,
    get_db,
)
from ..deps import current_user
from ..schemas import ApprovalDecisionRequest, TaskMessageRequest


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _require_task(db: Session, user_id: int, task_id: str) -> AgentTask:
    task = db.query(AgentTask).filter(AgentTask.id == task_id, AgentTask.user_id == user_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _payload(task: AgentTask) -> dict:
    return {
        "id": task.id,
        "conversation_id": task.conversation_id,
        "mode": task.mode,
        "prompt": task.prompt,
        "status": task.status,
        "error": task.error,
        "has_checkpoint": bool(task.checkpoint_json),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _subtask_payload(item: AgentSubtaskRecord) -> dict:
    return {
        "id": item.id,
        "task_id": item.task_id,
        "parent_id": item.parent_subtask_id,
        "name": item.name,
        "focus": item.focus,
        "status": item.status,
        "model": item.model,
        "has_checkpoint": bool(item.checkpoint_json),
        "result": item.result,
        "error": item.error,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


@router.get("")
def list_tasks(
    conversation_id: int | None = Query(default=None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = db.query(AgentTask).filter(AgentTask.user_id == user.id)
    if conversation_id is not None:
        query = query.filter(AgentTask.conversation_id == conversation_id)
    return [_payload(item) for item in query.order_by(AgentTask.created_at.desc()).limit(50).all()]


@router.get("/{task_id}")
def get_task(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    task = _require_task(db, user.id, task_id)
    events = db.query(RuntimeEventRecord).filter(RuntimeEventRecord.task_id == task.id).order_by(RuntimeEventRecord.id).all()
    approvals = db.query(ApprovalRequestRecord).filter(ApprovalRequestRecord.task_id == task.id).order_by(ApprovalRequestRecord.created_at).all()
    usage_rows = db.query(ModelUsageRecord).filter(ModelUsageRecord.task_id == task.id).all()
    subtasks = db.query(AgentSubtaskRecord).filter(
        AgentSubtaskRecord.task_id == task.id
    ).order_by(AgentSubtaskRecord.created_at).all()
    return {
        **_payload(task),
        "events": [
            {
                "id": event.id,
                "event": event.event_type,
                "content": json.loads(event.content_json) if event.content_json else None,
            }
            for event in events
        ],
        "approvals": [
            {
                "id": item.id,
                "tool_name": item.tool_name,
                "input": json.loads(item.tool_input_json),
                "capability": item.capability,
                "status": item.status,
            }
            for item in approvals
        ],
        "usage": {
            "input_tokens": sum(item.input_tokens for item in usage_rows),
            "output_tokens": sum(item.output_tokens for item in usage_rows),
            "cache_read_tokens": sum(item.cache_read_tokens for item in usage_rows),
            "cache_write_tokens": sum(item.cache_write_tokens for item in usage_rows),
            "cost_usd": round(sum(item.cost_usd for item in usage_rows), 8),
        },
        "subtasks": [_subtask_payload(item) for item in subtasks],
    }


@router.get("/{task_id}/subtasks")
def list_subtasks(
    task_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    _require_task(db, user.id, task_id)
    rows = db.query(AgentSubtaskRecord).filter(
        AgentSubtaskRecord.task_id == task_id
    ).order_by(AgentSubtaskRecord.created_at).all()
    return [_subtask_payload(item) for item in rows]


@router.get("/{task_id}/subtasks/{subtask_id}")
def get_subtask(
    task_id: str, subtask_id: str,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    _require_task(db, user.id, task_id)
    item = db.query(AgentSubtaskRecord).filter(
        AgentSubtaskRecord.task_id == task_id, AgentSubtaskRecord.id == subtask_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Sub-agent not found")
    return _subtask_payload(item)


@router.post("/{task_id}/subtasks/{subtask_id}/messages")
def enqueue_subtask_message(
    task_id: str, subtask_id: str, req: TaskMessageRequest,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    _require_task(db, user.id, task_id)
    queued = runtime_task_manager.enqueue_subtask_message(task_id, subtask_id, req.kind, req.content)
    if not queued:
        raise HTTPException(status_code=409, detail="Sub-agent is not accepting messages")
    return {"status": "queued", "agent_id": subtask_id, **queued}


@router.post("/{task_id}/subtasks/{subtask_id}/interrupt")
def interrupt_subtask(
    task_id: str, subtask_id: str,
    user: User = Depends(current_user), db: Session = Depends(get_db),
) -> dict:
    _require_task(db, user.id, task_id)
    if not runtime_task_manager.cancel_subtask(task_id, subtask_id):
        raise HTTPException(status_code=404, detail="Sub-agent not found")
    return {"id": subtask_id, "status": "cancelled"}


@router.post("/{task_id}/messages")
def enqueue_task_message(
    task_id: str,
    req: TaskMessageRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_task(db, user.id, task_id)
    queued = runtime_task_manager.enqueue_message(task_id, req.kind, req.content)
    if not queued:
        raise HTTPException(status_code=409, detail="Task is not accepting queued messages")
    runtime_task_manager.append_event(task_id, "message_queued", {"id": queued["id"], "kind": queued["kind"]})
    return {"status": "queued", **queued}


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    _require_task(db, user.id, task_id)
    return {"status": "cancelled" if runtime_task_manager.cancel(task_id) else "not-running"}


@router.post("/{task_id}/resume")
def resume_task(task_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    task = _require_task(db, user.id, task_id)
    if task.status not in {"failed", "cancelled", "interrupted"}:
        raise HTTPException(status_code=409, detail="Only failed, cancelled, or interrupted tasks can be resumed")
    if not task.checkpoint_json:
        raise HTTPException(status_code=409, detail="Task has no resumable checkpoint")
    return {
        "task_id": task.id,
        "conversation_id": task.conversation_id,
        "mode": task.mode,
        "message": task.prompt,
        "resume_from_checkpoint": True,
    }


@router.post("/{task_id}/approvals/{approval_id}")
async def resolve_approval(
    task_id: str,
    approval_id: str,
    req: ApprovalDecisionRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_task(db, user.id, task_id)
    approval = db.query(ApprovalRequestRecord).filter(
        ApprovalRequestRecord.id == approval_id,
        ApprovalRequestRecord.task_id == task_id,
    ).first()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if not runtime_task_manager.resolve_approval(approval_id, req.approved):
        raise HTTPException(status_code=409, detail="Approval request is no longer pending")
    return {"status": "approved" if req.approved else "rejected"}
