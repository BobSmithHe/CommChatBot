from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..core.workspace import ConversationWorkspaceManager, WorkspaceEditor
from ..core.context import context_manager
from ..infra.database import Conversation, Message


workspace_manager = ConversationWorkspaceManager()


def get_or_create_conversation(
    db: Session,
    user_id: int,
    conversation_id: int | None,
    mode: str,
) -> Conversation:
    if conversation_id is not None:
        conv = db.query(Conversation).filter(Conversation.id == conversation_id, Conversation.user_id == user_id).first()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conv.mode != mode:
            raise HTTPException(status_code=409, detail="Conversation belongs to a different mode")
        if conv.mode == "coding-agent":
            ensure_conversation_workspace(db, conv)
        return conv
    workspace_id = workspace_manager.create() if mode == "coding-agent" else None
    conv = Conversation(user_id=user_id, mode=mode, workspace_id=workspace_id, title="New Conversation")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def require_conversation(db: Session, user_id: int, conversation_id: int) -> Conversation:
    conv = db.query(Conversation).filter(Conversation.id == conversation_id, Conversation.user_id == user_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


def ensure_conversation_workspace(db: Session, conv: Conversation) -> WorkspaceEditor:
    if conv.mode != "coding-agent":
        raise HTTPException(status_code=400, detail="Workspace is only available in Coding Agent mode")
    if not conv.workspace_id:
        conv.workspace_id = workspace_manager.create()
        db.commit()
        db.refresh(conv)
    return workspace_manager.editor(conv.workspace_id)


def conversation_history(db: Session, conversation_id: int) -> list[dict]:
    return context_manager.prepare(db, conversation_id)


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
