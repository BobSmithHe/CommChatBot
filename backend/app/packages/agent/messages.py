from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid

from ..ai import LLMMessage


@dataclass
class AgentMessage:
    type: str
    content: Any
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def user(cls, content: str) -> "AgentMessage":
        return cls("user", content)

    @classmethod
    def assistant(cls, content: Any) -> "AgentMessage":
        return cls("assistant", content)

    @classmethod
    def system(cls, content: str) -> "AgentMessage":
        return cls("system", content)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "is_error": self.is_error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentMessage":
        return cls(
            id=str(payload.get("id") or uuid.uuid4().hex),
            type=str(payload.get("type") or "user"),
            content=payload.get("content"),
            tool_call_id=payload.get("tool_call_id"),
            tool_name=payload.get("tool_name"),
            is_error=bool(payload.get("is_error", False)),
            metadata=dict(payload.get("metadata") or {}),
        )


def convert_to_llm(messages: list[AgentMessage]) -> list[LLMMessage]:
    converted: list[LLMMessage] = []
    for message in messages:
        if message.type == "tool_result":
            converted.append(LLMMessage("tool", {
                "tool_call_id": message.tool_call_id,
                "content": message.content,
                "is_error": message.is_error,
            }))
        elif message.type in {"user", "assistant", "system"}:
            converted.append(LLMMessage(message.type, message.content))
    return converted
