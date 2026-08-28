from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...infra.database import AgentTask, Message, SessionLocal, User, get_db
from ...core.runtime_state import TaskCancelled, runtime_task_manager
from ...services import get_chat_attachment_store, get_chat_orchestrator
from ...sse import sse
from ..conversation_utils import conversation_history, ensure_conversation_workspace, get_or_create_conversation
from ..deps import current_user
from ..schemas import ChatRequest

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/attachments")
async def upload_chat_attachment(
    file: UploadFile = File(...),
    _user: User = Depends(current_user),
) -> dict:
    raw = await file.read()
    try:
        return get_chat_attachment_store().add(file.filename or "attachment.txt", raw, _user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stream")
async def chat_stream(req: ChatRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if req.mode != "chatbot" and req.attachment_ids:
        raise HTTPException(status_code=400, detail="Attachments are only available in Chat mode")
    resume_state = None
    resumed = bool(req.resume_task_id)
    if resumed:
        if req.attachment_ids:
            raise HTTPException(status_code=400, detail="Attachments cannot be changed while resuming a task")
        stored_task = db.query(AgentTask).filter(
            AgentTask.id == req.resume_task_id,
            AgentTask.user_id == user.id,
        ).first()
        if not stored_task:
            raise HTTPException(status_code=404, detail="Task not found")
        if stored_task.mode != req.mode:
            raise HTTPException(status_code=409, detail="Task belongs to a different mode")
        resume_state = runtime_task_manager.load_checkpoint(stored_task.id)
        if not resume_state:
            raise HTTPException(status_code=409, detail="Task has no resumable checkpoint")
        task = runtime_task_manager.resume(stored_task.id)
        if not task:
            raise HTTPException(status_code=409, detail="Task is not resumable")
        conv = get_or_create_conversation(db, user.id, stored_task.conversation_id, stored_task.mode)
        model_message = stored_task.prompt
        history: list[dict] = []
    else:
        try:
            attachments = get_chat_attachment_store().load_many(req.attachment_ids, user_id=user.id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        model_message = req.message
        stored_message = req.message
        if attachments:
            names = ", ".join(item["name"] for item in attachments)
            stored_message += f"\n\n📎 {names}"
            attachment_context = "\n\n".join(
                f"Attached file: {item['name']}\n{item['content']}" for item in attachments
            )
            model_message = (
                f"{req.message}\n\nThe user attached the following files. "
                f"Use them as context for this request:\n\n{attachment_context}"
            )
        conv = get_or_create_conversation(db, user.id, req.conversation_id, req.mode)
        history = conversation_history(db, conv.id)
        db.add(Message(user_id=user.id, conversation_id=conv.id, role="user", content=stored_message))
        if conv.title == "New Conversation":
            conv.title = req.message[:40] + ("..." if len(req.message) > 40 else "")
        db.commit()
        task = runtime_task_manager.create(
            user_id=user.id,
            conversation_id=conv.id,
            mode=req.mode,
            prompt=req.message,
        )
    workspace_dir = None
    if conv.mode == "coding-agent":
        workspace_dir = str(ensure_conversation_workspace(db, conv).root)
    conv_id = conv.id
    user_id = user.id

    async def events():
        full_answer = ""
        runtime_trace: list[dict] = []
        yield sse("task", {"task_id": task.id, "status": "running", "resumed": resumed})
        runtime_task_manager.append_event(task.id, "task", {"status": "running", "resumed": resumed})
        yield sse("status", f"Connected. mode={req.mode}")
        try:
            async for event in get_chat_orchestrator().stream(
                message=model_message,
                history=history,
                mode=req.mode,
                use_rag=req.use_rag,
                use_web=req.use_web,
                system_context=req.system_context,
                workspace_dir=workspace_dir,
                task_id=task.id,
                permission_mode=conv.permission_mode,
                resume_state=resume_state,
            ):
                if event.get("event") == "answer":
                    full_answer += str(event.get("content") or "")
                elif event.get("event") in {"status", "result", "sources", "approval_required", "hook"} and event.get("content"):
                    runtime_trace.append(
                        {"event": str(event.get("event")), "content": event.get("content")}
                    )
                if event.get("event") != "answer":
                    runtime_task_manager.append_event(task.id, str(event.get("event", "status")), event.get("content"))
                yield sse(event.get("event", "status"), event.get("content", ""))
        except TaskCancelled as exc:
            runtime_task_manager.append_event(task.id, "cancelled", str(exc))
            yield sse("cancelled", str(exc))
        except asyncio.CancelledError:
            runtime_task_manager.finish(task.id, "interrupted", "Client disconnected")
            raise
        except Exception as exc:
            runtime_task_manager.append_event(task.id, "error", f"{type(exc).__name__}: {exc}")
            runtime_task_manager.finish(task.id, "failed", f"{type(exc).__name__}: {exc}")
            yield sse("status", f"Request failed: {type(exc).__name__}: {exc}")
        if full_answer:
            runtime_task_manager.append_event(task.id, "answer", full_answer)
            with SessionLocal() as write_db:
                write_db.add(
                    Message(
                        user_id=user_id,
                        conversation_id=conv_id,
                        role="assistant",
                        content=full_answer,
                        trace_json=json.dumps(runtime_trace, ensure_ascii=False),
                    )
                )
                write_db.commit()
        if runtime_task_manager.cancelled(task.id):
            runtime_task_manager.finish(task.id, "cancelled")
        elif full_answer:
            runtime_task_manager.finish(task.id, "completed")
        else:
            runtime_task_manager.finish(task.id, "failed", "No answer was generated")
        yield sse("done")

    return StreamingResponse(events(), media_type="text/event-stream")
