from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class LLMMessage:
    role: str
    content: Any


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    content: Any
    tool_calls: list[ToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelStreamEvent:
    type: str
    delta: str = ""
    response: ModelResponse | None = None


class ModelProvider(Protocol):
    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[Any],
        model: str,
        *,
        system: str | None = None,
    ) -> ModelResponse:
        ...
