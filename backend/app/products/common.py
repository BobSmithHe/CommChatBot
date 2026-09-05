from __future__ import annotations

from ..agent_runtime import AgentMessage


def history_to_agent_messages(history: list[dict]) -> list[AgentMessage]:
    messages: list[AgentMessage] = []
    for item in history:
        role = item.get("role")
        content = str(item.get("content") or "")
        if not content:
            continue
        if role == "assistant":
            messages.append(AgentMessage.assistant(content))
        elif role == "system":
            messages.append(AgentMessage.system(content))
        elif role == "user":
            messages.append(AgentMessage.user(content))
    return messages


def map_agent_event(event) -> dict | None:
    if event.type in {"turn_start", "turn_end"}:
        return {"event": event.type, "content": event.data}
    if event.type == "llm_response":
        return {"event": "llm_response", "content": event.data}
    if event.type == "thinking":
        return {"event": "thinking", "content": event.data}
    if event.type == "tool_start":
        return {
            "event": "tool_use",
            "content": {
                "id": event.data.get("id"),
                "name": event.data.get("name"),
                "input": event.data.get("input") or {},
                "recovered": bool(event.data.get("recovered")),
            },
        }
    if event.type == "tool_end":
        result = event.data.get("result")
        return {
            "event": "tool_result",
            "content": {
                "id": result.tool_call_id,
                "name": result.tool_name,
                "output": result.content,
                "is_error": result.is_error,
                "metadata": result.metadata,
                "recovered": bool(event.data.get("recovered")),
            },
        }
    if event.type == "tool_update":
        return {"event": "tool_update", "content": event.data}
    if event.type == "queued_message":
        return {"event": "queued_message", "content": event.data}
    if event.type == "intermediate_answer":
        return {"event": "intermediate_answer", "content": event.data}
    if event.type == "context_compacted":
        return {"event": "context_compacted", "content": event.data}
    if event.type == "message_update":
        if event.data.get("final"):
            return {"event": "answer", "content": event.data.get("delta", "")}
        return None
    if event.type == "final_answer":
        return {"event": "answer", "content": event.data.get("content", "")}
    if event.type == "status" and isinstance(event.data, dict):
        return {"event": "status", "content": event.data}
    if event.type == "error":
        return {"event": "status", "content": event.data.get("error", "Agent error")}
    if event.type == "approval_required":
        return {"event": "approval_required", "content": event.data}
    if event.type == "hook":
        return {"event": "hook", "content": event.data}
    return None
