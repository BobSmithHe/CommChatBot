from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ...agent_runtime import TaskCancelled
from ..database import AgentSubtaskRecord, AgentTask, Conversation, Message, SessionLocal
from ..ports import ConversationAccessPort, MemoryJobQueuePort, ProductGatewayPort
from .task_runtime import runtime_task_manager


async def execute_queued_task(
    task_id: str,
    *,
    gateway: ProductGatewayPort,
    conversations: ConversationAccessPort,
    memory_jobs: MemoryJobQueuePort,
) -> bool:
    """Claim and execute one durable task outside the HTTP request lifecycle."""
    live = runtime_task_manager.activate(task_id)
    if not live:
        return False
    with SessionLocal() as db:
        task = db.query(AgentTask).filter(AgentTask.id == task_id).first()
        if not task:
            return False
        conversation = db.query(Conversation).filter(Conversation.id == task.conversation_id).first()
        if not conversation:
            runtime_task_manager.finish(task_id, "failed", "Conversation no longer exists")
            return False
        try:
            request: dict[str, Any] = json.loads(task.request_json or "{}")
        except (TypeError, json.JSONDecodeError):
            request = {}
        user_id, conversation_id, mode = task.user_id, task.conversation_id, task.mode
        permission_mode = conversation.permission_mode

    workspace_dir = None
    project_identity = None
    if mode == "coding-agent":
        with SessionLocal() as db:
            conversation = db.query(Conversation).filter(Conversation.id == conversation_id).first()
            workspace = conversations.ensure_workspace(db, conversation)
            workspace_dir = str(workspace.root)
            project_identity = workspace.project_identity()

    resume_state = runtime_task_manager.load_checkpoint(task_id)
    runtime_prompt = (
        str(request.get("resume_prompt") or "").strip() or None
        if resume_state
        else str(request.get("model_message") or request.get("message") or "")
    )
    full_answer = ""
    pending_answer: list[str] = []
    pending_answer_chars = 0
    last_answer_flush = time.monotonic()
    trace: list[dict] = []
    pending_thinking: list[str] = []
    pending_thinking_chars = 0
    pending_thinking_message_id = ""
    last_thinking_flush = time.monotonic()

    def flush_thinking() -> None:
        nonlocal pending_thinking_chars, last_thinking_flush
        if not pending_thinking:
            return
        payload = {
            "delta": "".join(pending_thinking),
            "message_id": pending_thinking_message_id,
        }
        runtime_task_manager.append_event(task_id, "thinking", payload)
        trace.append({"event": "thinking", "content": payload})
        pending_thinking.clear()
        pending_thinking_chars = 0
        last_thinking_flush = time.monotonic()

    runtime_task_manager.append_event(task_id, "task", {"task_id": task_id, "status": "running", "resumed": bool(resume_state)})
    try:
        async for event in gateway.stream(
            message=runtime_prompt,
            history=request.get("history") or [],
            mode=mode,
            use_rag=bool(request.get("use_rag")),
            use_web=bool(request.get("use_web")),
            system_context=request.get("system_context"),
            workspace_dir=workspace_dir,
            task_id=task_id,
            user_id=user_id,
            conversation_id=conversation_id,
            permission_mode=permission_mode,
            project_trusted=bool(request.get("project_trusted")),
            resume_state=resume_state,
        ):
            event_type = str(event.get("event") or "status")
            content = event.get("content", "")
            if event_type == "thinking":
                delta = str(content.get("delta") or "") if isinstance(content, dict) else str(content or "")
                message_id = str(content.get("message_id") or "") if isinstance(content, dict) else ""
                if not delta:
                    continue
                if pending_thinking and message_id != pending_thinking_message_id:
                    flush_thinking()
                pending_thinking_message_id = message_id
                pending_thinking.append(delta)
                pending_thinking_chars += len(delta)
                now = time.monotonic()
                if pending_thinking_chars >= 256 or now - last_thinking_flush >= 0.075:
                    flush_thinking()
                continue
            flush_thinking()
            if event_type == "answer":
                delta = str(content or "")
                full_answer += delta
                pending_answer.append(delta)
                pending_answer_chars += len(delta)
                now = time.monotonic()
                if pending_answer_chars >= 256 or now - last_answer_flush >= 0.075:
                    runtime_task_manager.append_event(task_id, "answer", "".join(pending_answer))
                    pending_answer.clear()
                    pending_answer_chars = 0
                    last_answer_flush = now
                continue
            if event_type == "intermediate_answer":
                intermediate = str(content.get("content") or "") if isinstance(content, dict) else str(content or "")
                if intermediate:
                    with SessionLocal() as write_db:
                        write_db.add(Message(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            role="assistant",
                            content=intermediate,
                            trace_json=json.dumps(trace, ensure_ascii=False),
                        ))
                        write_db.commit()
                full_answer = ""
                runtime_task_manager.append_event(task_id, event_type, content)
                trace = []
                continue
            if event_type == "queued_message":
                queued_content = str(content.get("content") or "") if isinstance(content, dict) else ""
                if queued_content:
                    with SessionLocal() as write_db:
                        write_db.add(Message(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            role="user",
                            content=queued_content,
                        ))
                        write_db.commit()
            elif event_type in {
                "turn_start", "turn_end", "llm_response", "status", "result", "sources",
                "thinking", "tool_use", "tool_update", "tool_result", "queued_message", "intermediate_answer",
                "context_compacted", "approval_required", "hook", "error",
                "memory_recalled",
                "memory_extraction_queued",
                "subagent_started",
                "subagent_progress",
                "subagent_finished",
            } and content:
                trace.append({"event": event_type, "content": content})
            if pending_answer:
                runtime_task_manager.append_event(task_id, "answer", "".join(pending_answer))
                pending_answer.clear()
                pending_answer_chars = 0
                last_answer_flush = time.monotonic()
            runtime_task_manager.append_event(task_id, event_type, content)
    except TaskCancelled as exc:
        flush_thinking()
        runtime_task_manager.append_event(task_id, "cancelled", str(exc))
        runtime_task_manager.finish(task_id, "cancelled")
        return True
    except Exception as exc:
        flush_thinking()
        error = f"{type(exc).__name__}: {exc}"
        runtime_task_manager.append_event(task_id, "error", error)
        runtime_task_manager.finish(task_id, "failed", error)
        return True

    flush_thinking()
    if pending_answer:
        runtime_task_manager.append_event(task_id, "answer", "".join(pending_answer))
    if full_answer:
        with SessionLocal() as db:
            # Sub-agent events are published directly by the delegate while it
            # runs. Materialize the durable tree into the assistant trace too,
            # so it remains visible after reconnecting or reopening a chat.
            for subtask in db.query(AgentSubtaskRecord).filter(
                AgentSubtaskRecord.task_id == task_id
            ).order_by(AgentSubtaskRecord.created_at).all():
                trace.extend([
                    {"event": "subagent_started", "content": {
                        "id": subtask.id, "parent_id": subtask.parent_subtask_id,
                        "name": subtask.name, "focus": subtask.focus, "model": subtask.model,
                    }},
                    {"event": "subagent_finished", "content": {
                        "id": subtask.id, "status": subtask.status,
                        "result": subtask.result, "error": subtask.error,
                    }},
                ])
            db.add(Message(
                user_id=user_id,
                conversation_id=conversation_id,
                role="assistant",
                content=full_answer,
                trace_json=json.dumps(trace, ensure_ascii=False),
            ))
            db.commit()
        try:
            job_id = await asyncio.to_thread(
                memory_jobs.enqueue,
                task_id=task_id,
                user_id=user_id,
                conversation_id=conversation_id,
                mode=mode,
                user_text=task.prompt,
                assistant_text=full_answer,
                project_identity=project_identity,
            )
            if job_id:
                runtime_task_manager.append_event(
                    task_id,
                    "memory_extraction_queued",
                    {"job_id": job_id},
                )
        except Exception:
            # Enrichment is best-effort. The response completes independently
            # and the durable job can be retried by the memory worker.
            pass
        runtime_task_manager.finish(task_id, "completed")
    elif runtime_task_manager.cancelled(task_id):
        runtime_task_manager.finish(task_id, "cancelled")
    else:
        error = "Model completed without generating a final answer"
        runtime_task_manager.append_event(task_id, "error", error)
        runtime_task_manager.finish(task_id, "failed", error)
    return True
