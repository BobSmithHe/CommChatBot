from __future__ import annotations

import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...infra.database import Conversation, Message, User, get_db
from ...core.workspace.terminal import terminal_manager
from ...sse import sse
from ..conversation_utils import (
    conversation_payload,
    ensure_conversation_workspace,
    message_payload,
    require_conversation,
    workspace_manager,
)
from ..deps import current_user
from ..schemas import (
    ConversationCreateRequest,
    ConversationPermissionRequest,
    WorkspaceDirectoryCreateRequest,
    WorkspaceFileWriteRequest,
    WorkspaceRunRequest,
    TerminalCommandRequest,
    WorkspaceMoveRequest,
    GitCheckpointRequest,
    GitRestoreRequest,
    WorkspaceTrashRestoreRequest,
)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("")
def create_conversation(
    req: ConversationCreateRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    workspace_id = workspace_manager.create() if req.mode == "coding-agent" else None
    conv = Conversation(user_id=user.id, mode=req.mode, workspace_id=workspace_id, title="New Conversation")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conversation_payload(conv, 0)


@router.get("")
def list_conversations(
    mode: Literal["chatbot", "coding-agent"] | None = None,
    archived: bool = False,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = (
        db.query(Conversation, func.count(Message.id).label("message_count"))
        .outerjoin(Message, Message.conversation_id == Conversation.id)
        .filter(Conversation.user_id == user.id)
        .filter(Conversation.is_archived == archived)
    )
    if mode is not None:
        query = query.filter(Conversation.mode == mode)
    rows = query.group_by(Conversation.id).order_by(Conversation.updated_at.desc(), Conversation.id.desc()).all()
    return [conversation_payload(conv, int(count or 0)) for conv, count in rows]


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: int,
    limit: int = 50,
    before: int | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    query = db.query(Message).filter(Message.conversation_id == conv.id)
    if before:
        query = query.filter(Message.id < before)
    rows = query.order_by(Message.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return {
        "id": conv.id,
        "mode": conv.mode,
        "workspace_id": conv.workspace_id,
        "permission_mode": conv.permission_mode,
        "title": conv.title,
        "messages": [message_payload(row) for row in rows],
        "has_more": has_more,
    }


@router.patch("/{conversation_id}/permission")
def update_conversation_permission(
    conversation_id: int,
    req: ConversationPermissionRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    conv.permission_mode = req.permission_mode
    db.commit()
    return {"permission_mode": conv.permission_mode}


@router.get("/{conversation_id}/workspace/files")
def list_workspace_files(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    listing = editor.list_files(limit=500)
    return {
        "workspace_id": conv.workspace_id,
        "entries": editor.list_entries(limit=1000),
        "files": [] if listing == "No files found." else listing.splitlines(),
    }


@router.get("/{conversation_id}/workspace/file")
def read_workspace_file(
    conversation_id: int,
    path: str = Query(min_length=1, max_length=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"path": path, "content": editor.read_text(path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/file")
def write_workspace_file(
    conversation_id: int,
    req: WorkspaceFileWriteRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        result = editor.write_file(req.path, req.content, overwrite=True)
        return {"status": "saved", "path": req.path, "result": result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/directory")
def create_workspace_directory(
    conversation_id: int,
    req: WorkspaceDirectoryCreateRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"status": "created", "path": req.path, "result": editor.create_directory(req.path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{conversation_id}/workspace/path")
def delete_workspace_path(
    conversation_id: int,
    path: str = Query(min_length=1, max_length=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"status": "deleted", "path": path, "result": editor.delete_path(path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/move")
def move_workspace_path(
    conversation_id: int,
    req: WorkspaceMoveRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {
            "status": "moved",
            "source": req.source,
            "target": req.target,
            "result": editor.move_path(req.source, req.target),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{conversation_id}/workspace/trash")
def list_workspace_trash(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    return {"items": ensure_conversation_workspace(db, conv).list_trash()}


@router.post("/{conversation_id}/workspace/trash/restore")
def restore_workspace_trash(
    conversation_id: int,
    req: WorkspaceTrashRestoreRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    try:
        return {"status": "restored", "result": ensure_conversation_workspace(db, conv).restore_trash(req.trash_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{conversation_id}/workspace/git/status")
def workspace_git_status(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    return ensure_conversation_workspace(db, conv).git_status()


@router.get("/{conversation_id}/workspace/git/diff")
def workspace_git_diff(
    conversation_id: int,
    path: str | None = Query(default=None, max_length=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    try:
        return {"diff": ensure_conversation_workspace(db, conv).git_diff(path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/git/checkpoint")
def workspace_git_checkpoint(
    conversation_id: int,
    req: GitCheckpointRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    try:
        return {"status": "checkpointed", "result": ensure_conversation_workspace(db, conv).git_checkpoint(req.message)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/git/restore")
def workspace_git_restore(
    conversation_id: int,
    req: GitRestoreRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    try:
        return {"status": "restored", "result": ensure_conversation_workspace(db, conv).git_restore(req.path)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/run")
async def run_workspace_file(
    conversation_id: int,
    req: WorkspaceRunRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        result = await editor.run_python_file(req.path)
        return {"success": result["exit_code"] == 0, **result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{conversation_id}/workspace/terminals")
def list_workspace_terminals(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    ensure_conversation_workspace(db, conv)
    return {"terminals": terminal_manager.list(conv.workspace_id)}


@router.post("/{conversation_id}/workspace/terminals")
def create_workspace_terminal(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    return terminal_manager.create(conv.workspace_id, editor.root)


@router.post("/{conversation_id}/workspace/terminals/{terminal_id}/execute")
def execute_terminal_command(
    conversation_id: int,
    terminal_id: str,
    req: TerminalCommandRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    ensure_conversation_workspace(db, conv)
    try:
        return terminal_manager.execute(conv.workspace_id, terminal_id, req.command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/terminals/{terminal_id}/execute/stream")
async def stream_terminal_command(
    conversation_id: int,
    terminal_id: str,
    req: TerminalCommandRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    conv = require_conversation(db, user.id, conversation_id)
    ensure_conversation_workspace(db, conv)
    try:
        initial = terminal_manager.payload(conv.workspace_id, terminal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    async def events():
        offset = len(initial.get("output", ""))
        future = asyncio.create_task(
            asyncio.to_thread(terminal_manager.execute, conv.workspace_id, terminal_id, req.command)
        )
        while not future.done():
            await asyncio.sleep(0.08)
            current = terminal_manager.payload(conv.workspace_id, terminal_id)
            output = current.get("output", "")
            if len(output) > offset:
                yield sse("output", output[offset:])
                offset = len(output)
        try:
            result = await future
            output = result.get("output", "")
            if len(output) > offset:
                yield sse("output", output[offset:])
            yield sse("terminal", result)
        except Exception as exc:
            yield sse("error", f"{type(exc).__name__}: {exc}")
        yield sse("done")

    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/{conversation_id}/workspace/terminals/{terminal_id}/interrupt")
def interrupt_terminal_command(
    conversation_id: int,
    terminal_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    ensure_conversation_workspace(db, conv)
    try:
        return {"status": "interrupted", "terminal": terminal_manager.interrupt(conv.workspace_id, terminal_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{conversation_id}/workspace/terminals/{terminal_id}")
def close_workspace_terminal(
    conversation_id: int,
    terminal_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    ensure_conversation_workspace(db, conv)
    try:
        terminal_manager.close(conv.workspace_id, terminal_id)
        return {"status": "closed"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    if conv.workspace_id:
        terminal_manager.close_workspace(conv.workspace_id)
    conv.is_archived = True
    db.commit()
    return {"status": "archived"}


@router.post("/{conversation_id}/restore")
def restore_conversation(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    conv.is_archived = False
    db.commit()
    return {"status": "restored"}
