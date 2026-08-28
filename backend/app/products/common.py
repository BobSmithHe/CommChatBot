from __future__ import annotations

from ..packages.agent import AgentMessage


def history_to_agent_messages(history: list[dict]) -> list[AgentMessage]:
    messages: list[AgentMessage] = []
    for item in history[-20:]:
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
    if event.type == "tool_start":
        return {"event": "status", "content": f"Calling {event.data.get('name')}: {event.data.get('input')}"}
    if event.type == "tool_end":
        result = event.data.get("result")
        return {"event": "result", "content": f"{result.tool_name}: {result.content[:1200]}"}
    if event.type == "message_update":
        return {"event": "answer", "content": event.data.get("delta", "")}
    if event.type == "error":
        return {"event": "status", "content": event.data.get("error", "Agent error")}
    if event.type == "approval_required":
        return {"event": "approval_required", "content": event.data}
    if event.type == "hook":
        return {"event": "hook", "content": event.data}
    return None
