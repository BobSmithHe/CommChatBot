from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ...infra.config import get_settings
from ...platform.database import AgentTask, Message, RuntimeEventRecord, SessionLocal, User, get_db
from ...platform.services.task_runtime import runtime_task_manager
from ...platform.services.task_queue import task_queue
from ...platform.services.event_bus import event_bus
from ...services import get_chat_attachment_store
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
        try:
            request_payload = json.loads(stored_task.request_json or "{}")
        except (TypeError, json.JSONDecodeError):
            request_payload = {}
        request_payload.setdefault("model_message", model_message)
        request_payload.setdefault("history", history)
        request_payload.setdefault("use_rag", req.use_rag)
        request_payload.setdefault("use_web", req.use_web)
        request_payload.setdefault("system_context", req.system_context)
        candidate_prompt = req.message.strip()
        request_payload["resume_prompt"] = (
            candidate_prompt if candidate_prompt != stored_task.prompt.strip() else None
        )
        stored_task.request_json = json.dumps(request_payload, ensure_ascii=False)
        db.commit()
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
        request_payload = {
            "model_message": model_message,
            "history": history,
            "use_rag": req.use_rag,
            "use_web": req.use_web,
            "system_context": req.system_context,
            "project_trusted": bool(conv.project_trusted),
        }
        task = runtime_task_manager.create(
            user_id=user.id,
            conversation_id=conv.id,
            mode=req.mode,
            prompt=req.message,
            status="queued",
            request=request_payload,
        )
    if conv.mode == "coding-agent":
        ensure_conversation_workspace(db, conv)

    task_queue.enqueue(task.id)

    async def events():
        yield sse("task", {"task_id": task.id, "status": "running", "resumed": resumed})
        db_cursor = 0
        stream_cursor = "0-0"
        idle_ticks = 0
        error_sent = False
        seen_event_keys: set[str] = set()
        terminal_statuses = {"completed", "failed", "cancelled", "interrupted"}
        while True:
            streamed = await asyncio.to_thread(
                event_bus.read,
                task.id,
                stream_cursor,
                block_ms=750,
                count=200,
            )
            serialized: list[tuple[str, str, object]] = []
            if streamed is not None:
                stream_cursor, bus_events = streamed
                serialized = [
                    (str(item.get("event_key") or ""), str(item.get("event") or "status"), item.get("content"))
                    for item in bus_events
                ]
            else:
                # Redis is optional. The durable database remains a complete
                # compatibility fallback during an outage.
                with SessionLocal() as read_db:
                    rows = read_db.query(RuntimeEventRecord).filter(
                        RuntimeEventRecord.task_id == task.id,
                        RuntimeEventRecord.id > db_cursor,
                    ).order_by(RuntimeEventRecord.id).limit(200).all()
                    serialized = [
                        (str(row.event_key or f"db-{row.id}"), row.event_type, row.content_json)
                        for row in rows
                    ]
                if rows:
                    db_cursor = max(db_cursor, max(row.id for row in rows))

            emitted = False
            for event_key, event_type, raw_content in serialized:
                if event_key and event_key in seen_event_keys:
                    continue
                if event_key:
                    seen_event_keys.add(event_key)
                if streamed is None:
                    try:
                        content = json.loads(raw_content) if raw_content is not None else ""
                    except json.JSONDecodeError:
                        content = raw_content or ""
                else:
                    content = raw_content
                if event_type == "error":
                    error_sent = True
                yield sse(event_type, content)
                emitted = True

            if emitted:
                continue
            with SessionLocal() as read_db:
                current = read_db.query(AgentTask.status, AgentTask.error).filter(
                    AgentTask.id == task.id
                ).first()
            status = current[0] if current else "failed"
            task_error = current[1] if current else "Task record no longer exists"
            if status in terminal_statuses:
                if status == "failed" and not error_sent:
                    yield sse("error", task_error or "Task failed")
                break
            idle_ticks += 1
            if idle_ticks % 100 == 0:
                yield ": keep-alive\n\n"
            if streamed is None:
                await asyncio.sleep(max(0.05, get_settings().task_event_poll_ms / 1000))
        yield sse("done")

    return StreamingResponse(events(), media_type="text/event-stream")
