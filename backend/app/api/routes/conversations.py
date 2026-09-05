from __future__ import annotations

import asyncio
import subprocess
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...platform.database import Conversation, Message, SessionLocal, User, ensure_anonymous_user, get_db
from ...infra.config import get_settings
from ...infra.security import decode_access_token
from ...extensions.builtin.terminal.session import terminal_manager
from ...extensions.builtin.workspace.language import WorkspaceLanguageService
from ...extensions.builtin.project_context import project_context_loader
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
    ConversationForkRequest,
    ConversationPermissionRequest,
    ConversationTrustRequest,
    WorkspaceDirectoryCreateRequest,
    WorkspaceFileWriteRequest,
    WorkspaceRunRequest,
    TerminalCommandRequest,
    WorkspaceMoveRequest,
    GitCheckpointRequest,
    GitBranchRequest,
    GitRestoreRequest,
    LanguageDocumentRequest,
    LanguagePositionRequest,
    LanguageRenameRequest,
    WorkspaceTrashRestoreRequest,
    WorkspaceImportRequest,
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
        "parent_conversation_id": conv.parent_conversation_id,
        "forked_from_message_id": conv.forked_from_message_id,
        "branch_name": conv.branch_name,
        "project_trusted": bool(conv.project_trusted),
        "title": conv.title,
        "messages": [message_payload(row) for row in rows],
        "has_more": has_more,
    }


@router.post("/{conversation_id}/fork")
def fork_conversation(
    conversation_id: int,
    req: ConversationForkRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    source = require_conversation(db, user.id, conversation_id)
    query = db.query(Message).filter(Message.conversation_id == source.id)
    if req.from_message_id is not None:
        boundary = query.filter(Message.id == req.from_message_id).first()
        if not boundary:
            raise HTTPException(status_code=404, detail="Fork message not found")
        query = query.filter(Message.id <= req.from_message_id)
    source_messages = query.order_by(Message.id).all()
    workspace_id = None
    if source.mode == "coding-agent" and source.workspace_id:
        try:
            workspace_id = workspace_manager.fork(source.workspace_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    branch = Conversation(
        user_id=user.id,
        mode=source.mode,
        workspace_id=workspace_id,
        permission_mode=source.permission_mode,
        parent_conversation_id=source.id,
        forked_from_message_id=req.from_message_id or (source_messages[-1].id if source_messages else None),
        branch_name=req.branch_name.strip(),
        project_trusted=bool(source.project_trusted),
        title=f"{source.title} · {req.branch_name.strip()}",
    )
    db.add(branch)
    db.flush()
    for item in source_messages:
        db.add(Message(
            user_id=user.id,
            conversation_id=branch.id,
            role=item.role,
            content=item.content,
            trace_json=item.trace_json,
            created_at=item.created_at,
        ))
    db.commit()
    db.refresh(branch)
    return conversation_payload(branch, len(source_messages))


@router.get("/{conversation_id}/branches")
def conversation_branches(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    current = require_conversation(db, user.id, conversation_id)
    by_id = {item.id: item for item in db.query(Conversation).filter(Conversation.user_id == user.id).all()}
    root = current
    seen: set[int] = set()
    while root.parent_conversation_id and root.parent_conversation_id in by_id and root.id not in seen:
        seen.add(root.id)
        root = by_id[root.parent_conversation_id]
    included = {root.id}
    changed = True
    while changed:
        changed = False
        for item in by_id.values():
            if item.parent_conversation_id in included and item.id not in included:
                included.add(item.id)
                changed = True
    return {
        "root_id": root.id,
        "active_id": current.id,
        "branches": [conversation_payload(by_id[item_id], 0) for item_id in sorted(included)],
    }


@router.post("/{conversation_id}/branches/{branch_id}/restore")
def restore_conversation_branch(
    conversation_id: int,
    branch_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    tree = conversation_branches(conversation_id, user, db)
    if branch_id not in {item["id"] for item in tree["branches"]}:
        raise HTTPException(status_code=404, detail="Conversation branch not found")
    branch = require_conversation(db, user.id, branch_id)
    return conversation_payload(branch, db.query(Message).filter(Message.conversation_id == branch.id).count())


@router.patch("/{conversation_id}/trust")
def update_project_trust(
    conversation_id: int,
    req: ConversationTrustRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    if conv.mode != "coding-agent":
        raise HTTPException(status_code=409, detail="Project trust is only available in Coding Agent mode")
    conv.project_trusted = req.trusted
    db.commit()
    return {"project_trusted": bool(conv.project_trusted)}


@router.get("/{conversation_id}/project-context")
def get_project_context_status(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    return {
        "project_trusted": bool(conv.project_trusted),
        "discovered": project_context_loader.discover(editor.root),
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


@router.post("/{conversation_id}/workspace/import")
def import_conversation_workspace(
    conversation_id: int,
    req: WorkspaceImportRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    if conv.mode != "coding-agent":
        raise HTTPException(status_code=409, detail="Project import is only available in Coding Agent mode")
    try:
        workspace_id = workspace_manager.import_workspace(req.source, req.kind, req.branch)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    previous = conv.workspace_id
    conv.workspace_id = workspace_id
    db.commit()
    if previous:
        terminal_manager.close_workspace(previous)
    editor = workspace_manager.editor(workspace_id)
    return {"workspace_id": workspace_id, "entries": editor.list_entries(limit=1000)}


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
        return {"path": path, "content": editor.read_text(path), "version": editor.file_version(path)}
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
        if req.expected_version and not req.force:
            try:
                current_version = editor.file_version(req.path)
            except ValueError:
                current_version = None
            if current_version and current_version != req.expected_version:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "File changed outside the editor", "version": current_version},
                )
        result = editor.write_file(req.path, req.content, overwrite=True)
        return {
            "status": "saved",
            "path": req.path,
            "result": result,
            "version": editor.file_version(req.path),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/language/completions")
def workspace_completions(
    conversation_id: int,
    req: LanguagePositionRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"items": WorkspaceLanguageService(editor.root).complete(req.path, req.content, req.line, req.column)}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/language/diagnostics")
def workspace_diagnostics(
    conversation_id: int,
    req: LanguageDocumentRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"items": WorkspaceLanguageService(editor.root).diagnostics(req.path, req.content)}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/language/format")
def workspace_format(
    conversation_id: int,
    req: LanguageDocumentRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"content": WorkspaceLanguageService(editor.root).format(req.path, req.content)}
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/language/definition")
def workspace_definition(
    conversation_id: int,
    req: LanguagePositionRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"items": WorkspaceLanguageService(editor.root).definition(req.path, req.content, req.line, req.column)}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/language/hover")
def workspace_hover(
    conversation_id: int,
    req: LanguagePositionRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"item": WorkspaceLanguageService(editor.root).hover(req.path, req.content, req.line, req.column)}
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/language/references")
def workspace_references(
    conversation_id: int,
    req: LanguagePositionRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"items": WorkspaceLanguageService(editor.root).references(req.path, req.content, req.line, req.column)}
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/language/rename")
def workspace_rename(
    conversation_id: int,
    req: LanguageRenameRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    editor = ensure_conversation_workspace(db, conv)
    try:
        return {"items": WorkspaceLanguageService(editor.root).rename(
            req.path, req.content, req.line, req.column, req.new_name
        )}
    except (ValueError, OSError, TimeoutError) as exc:
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


@router.get("/{conversation_id}/workspace/git/branches")
def workspace_git_branches(
    conversation_id: int,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    try:
        return ensure_conversation_workspace(db, conv).git_branches()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{conversation_id}/workspace/git/branches/switch")
def workspace_git_switch_branch(
    conversation_id: int,
    req: GitBranchRequest,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict:
    conv = require_conversation(db, user.id, conversation_id)
    try:
        result = ensure_conversation_workspace(db, conv).git_switch_branch(req.name, req.create)
        return {"status": "switched", "branch": req.name, "result": result}
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


@router.websocket("/{conversation_id}/workspace/terminals/{terminal_id}/ws")
async def terminal_websocket(websocket: WebSocket, conversation_id: int, terminal_id: str) -> None:
    """Bidirectional raw PTY transport. Authentication is the first frame, never a URL token."""
    await websocket.accept()
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=5)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        await websocket.close(code=4401, reason="Authentication required")
        return

    token = str(hello.get("token") or "") if hello.get("type") == "auth" else ""
    with SessionLocal() as db:
        if token:
            subject = decode_access_token(token)
            user = db.query(User).filter(User.id == int(subject)).first() if subject and subject.isdigit() else None
        elif get_settings().allow_anonymous:
            user = ensure_anonymous_user(db)
        else:
            user = None
        conv = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id if user else False,
        ).first()
        if not user or not user.is_active or not conv or conv.mode != "coding-agent" or not conv.workspace_id:
            await websocket.close(code=4403, reason="Terminal access denied")
            return
        workspace_id = conv.workspace_id

    try:
        terminal_manager.resize(
            workspace_id,
            terminal_id,
            int(hello.get("cols") or 100),
            int(hello.get("rows") or 28),
        )
        initial = terminal_manager.payload(workspace_id, terminal_id)
    except (TypeError, ValueError):
        await websocket.close(code=4404, reason="Terminal not found")
        return

    cursor = int(initial.get("output_offset") or 0)
    initial_output = str(initial.get("output") or "")
    if initial_output:
        await websocket.send_json({"type": "output", "data": initial_output})
    cursor = int(initial.get("cursor") or cursor)
    await websocket.send_json({"type": "ready", "terminal": {**initial, "output": ""}})

    async def send_output() -> None:
        nonlocal cursor
        restart_count = int(initial.get("restart_count") or 0)
        while True:
            chunk, cursor, alive = await asyncio.to_thread(
                terminal_manager.read_since, workspace_id, terminal_id, cursor, 0.25
            )
            if chunk:
                await websocket.send_json({"type": "output", "data": chunk})
            current = terminal_manager.payload(workspace_id, terminal_id)
            current_restart_count = int(current.get("restart_count") or 0)
            if current_restart_count != restart_count:
                restart_count = current_restart_count
                await websocket.send_json({
                    "type": "terminal_restarted",
                    "terminal": {**current, "output": ""},
                })
            if not alive:
                # Ctrl+C recovery may replace the ConPTY bridge between the
                # read and this state check. Only report exit if the newly
                # observed session is still dead.
                if not current.get("running"):
                    await websocket.send_json({"type": "exit"})
                    return

    async def receive_input() -> None:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "input":
                data = str(message.get("data") or "")[:16_384]
                if data:
                    await asyncio.to_thread(terminal_manager.write, workspace_id, terminal_id, data)
            elif message_type == "eof":
                await asyncio.to_thread(terminal_manager.end_of_input, workspace_id, terminal_id)
            elif message_type == "resize":
                cols = int(message.get("cols") or 100)
                rows = int(message.get("rows") or 28)
                await asyncio.to_thread(
                    terminal_manager.resize,
                    workspace_id,
                    terminal_id,
                    cols,
                    rows,
                )
                await websocket.send_json({"type": "resize", "cols": cols, "rows": rows})

    tasks = {asyncio.create_task(send_output()), asyncio.create_task(receive_input())}
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
