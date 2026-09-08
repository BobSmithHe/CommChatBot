from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ...bootstrap import get_container
from ...extensions.builtin.memory import MEMORY_SCOPES, MemoryTarget
from ...platform.database import AgentTask, Conversation, User, get_db
from ..conversation_utils import ensure_conversation_workspace
from ..deps import current_user
from ..schemas import MemoryCreateRequest, MemorySettingsRequest, MemoryUpdateRequest


router = APIRouter(prefix="/api/memories", tags=["memories"])


def _memory():
    return get_container().memory_service


@router.get("/settings")
def memory_settings(user: User = Depends(current_user)) -> dict:
    return _memory().get_settings(user.id)


@router.patch("/settings")
def update_memory_settings(
    req: MemorySettingsRequest,
    user: User = Depends(current_user),
) -> dict:
    return _memory().update_settings(user.id, req.model_dump(exclude_none=True))


@router.get("")
def list_memories(
    scope: str | None = Query(default=None),
    query: str = Query(default="", max_length=500),
    conversation_id: int | None = Query(default=None, ge=1),
    task_id: str | None = Query(default=None, min_length=32, max_length=32),
    include_inactive: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    if scope and scope not in MEMORY_SCOPES:
        raise HTTPException(status_code=400, detail="Invalid memory scope")
    target_id = None
    if scope == "user":
        target_id = _memory().target("user", user_id=user.id).target_id
    elif scope == "conversation" and conversation_id:
        target_id = _resolve_target(
            db, user.id, scope, conversation_id=conversation_id, task_id=task_id
        ).target_id
    elif scope == "project" and conversation_id:
        target_id = _resolve_target(
            db, user.id, scope, conversation_id=conversation_id, task_id=task_id
        ).target_id
    elif scope == "task" and task_id:
        target_id = _resolve_target(
            db, user.id, scope, conversation_id=conversation_id, task_id=task_id
        ).target_id
    return {
        "memories": _memory().list(
            user.id,
            scope=scope,
            target_id=target_id,
            query=query,
            include_inactive=include_inactive,
            limit=limit,
        )
    }


@router.get("/recall")
def recall_memories(
    query: str = Query(min_length=1, max_length=2000),
    conversation_id: int | None = Query(default=None, ge=1),
    task_id: str | None = Query(default=None, min_length=32, max_length=32),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    project_identity = _project_identity(db, user.id, conversation_id) if conversation_id else None
    if conversation_id:
        _conversation(db, user.id, conversation_id)
    if task_id:
        _task(db, user.id, task_id)
    return {
        "memories": _memory().recall(
            user_id=user.id,
            query=query,
            conversation_id=conversation_id,
            task_id=task_id,
            project_identity=project_identity,
        )
    }


@router.post("")
def create_memory(
    req: MemoryCreateRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    target = _resolve_target(
        db,
        user.id,
        req.scope,
        conversation_id=req.conversation_id,
        task_id=req.task_id,
    )
    expires_at = datetime.utcnow() + timedelta(days=req.ttl_days) if req.ttl_days else None
    try:
        return _memory().upsert(
            user_id=user.id,
            target=target,
            key=req.key,
            value=req.value,
            category=req.category,
            importance=req.importance,
            confidence=req.confidence,
            source_type="manual",
            source_task_id=req.task_id,
            expires_at=expires_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{memory_id}")
def update_memory(
    memory_id: str,
    req: MemoryUpdateRequest,
    user: User = Depends(current_user),
) -> dict:
    existing = _memory().get(user.id, memory_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Memory not found")
    ttl_days = req.ttl_days
    expires_at = (
        datetime.utcnow() + timedelta(days=ttl_days)
        if ttl_days
        else (None if ttl_days == 0 else _parse_datetime(existing["expires_at"]))
    )
    try:
        return _memory().upsert(
            user_id=user.id,
            target=MemoryTarget(existing["scope"], existing["target_id"]),
            key=req.key or existing["key"],
            value=req.value or existing["value"],
            category=req.category or existing["category"],
            importance=req.importance if req.importance is not None else existing["importance"],
            confidence=req.confidence if req.confidence is not None else existing["confidence"],
            source_type="manual-edit",
            source_message_id=existing["source_message_id"],
            source_task_id=existing["source_task_id"],
            expires_at=expires_at,
            metadata=existing["metadata"],
            memory_id=memory_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("")
def clear_memories(
    scope: str | None = Query(default=None),
    user: User = Depends(current_user),
) -> dict:
    if scope and scope not in MEMORY_SCOPES:
        raise HTTPException(status_code=400, detail="Invalid memory scope")
    return {"deleted": _memory().clear(user.id, scope)}


@router.delete("/{memory_id}")
def delete_memory(memory_id: str, user: User = Depends(current_user)) -> dict:
    if not _memory().delete(user.id, memory_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted"}


def _resolve_target(
    db: Session,
    user_id: int,
    scope: str,
    *,
    conversation_id: int | None,
    task_id: str | None,
) -> MemoryTarget:
    if scope == "user":
        return _memory().target("user", user_id=user_id)
    if scope == "conversation":
        conversation = _conversation(db, user_id, conversation_id)
        return _memory().target(scope, user_id=user_id, target_value=conversation.id)
    if scope == "task":
        task = _task(db, user_id, task_id)
        return _memory().target(scope, user_id=user_id, target_value=task.id)
    if scope == "project":
        identity = _project_identity(db, user_id, conversation_id)
        if not identity:
            raise HTTPException(status_code=400, detail="Project memory requires a Coding conversation")
        return _memory().target(scope, user_id=user_id, target_value=identity)
    raise HTTPException(status_code=400, detail="Invalid memory scope")


def _conversation(db: Session, user_id: int, conversation_id: int | None) -> Conversation:
    row = db.query(Conversation).filter(
        Conversation.id == conversation_id,
        Conversation.user_id == user_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return row


def _task(db: Session, user_id: int, task_id: str | None) -> AgentTask:
    row = db.query(AgentTask).filter(
        AgentTask.id == task_id,
        AgentTask.user_id == user_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return row


def _project_identity(db: Session, user_id: int, conversation_id: int | None) -> str | None:
    if not conversation_id:
        return None
    conversation = _conversation(db, user_id, conversation_id)
    if conversation.mode != "coding-agent":
        return None
    return ensure_conversation_workspace(db, conversation).project_identity()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
