from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..bootstrap import get_container
from ..platform.database import Conversation, Message
from ..platform.services.conversations import (
    ConversationModeConflict,
    ConversationNotFound,
    WorkspaceUnavailable,
)
from ..platform.ports import WorkspaceHandle


def get_or_create_conversation(
    db: Session,
    user_id: int,
    conversation_id: int | None,
    mode: str,
) -> Conversation:
    try:
        return get_container().conversations.get_or_create(db, user_id, conversation_id, mode)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConversationModeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def require_conversation(db: Session, user_id: int, conversation_id: int) -> Conversation:
    try:
        return get_container().conversations.require(db, user_id, conversation_id)
    except ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def ensure_conversation_workspace(db: Session, conv: Conversation) -> WorkspaceHandle:
    try:
        return get_container().conversations.ensure_workspace(db, conv)
    except WorkspaceUnavailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def conversation_history(db: Session, conversation_id: int) -> list[dict]:
    return get_container().conversations.history(db, conversation_id)


def conversation_payload(conv: Conversation, message_count: int) -> dict:
    return {
        "id": conv.id,
        "mode": conv.mode,
        "workspace_id": conv.workspace_id,
        "permission_mode": getattr(conv, "permission_mode", "workspace-write"),
        "is_archived": bool(getattr(conv, "is_archived", False)),
        "parent_conversation_id": getattr(conv, "parent_conversation_id", None),
        "forked_from_message_id": getattr(conv, "forked_from_message_id", None),
        "branch_name": getattr(conv, "branch_name", "main") or "main",
        "project_trusted": bool(getattr(conv, "project_trusted", False)),
        "title": conv.title,
        "message_count": message_count,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
    }


def message_payload(message: Message) -> dict:
    try:
        trace = json.loads(message.trace_json) if message.trace_json else []
    except (TypeError, ValueError):
        trace = []
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "trace": trace,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }
