from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..ai import ToolCall


ToolHandler = Callable[..., Awaitable[str] | str]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    execution_mode: Literal["sequential", "parallel"] = "sequential"
    capability: Literal["read", "write", "execute", "network"] = "read"


@dataclass
class ToolResult:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolExecutor:
    def __init__(self, tools: list[Tool]) -> None:
        self.tools = {tool.name: tool for tool in tools}

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, f"Unknown tool: {call.name}", is_error=True)
        missing = [item for item in tool.input_schema.get("required", []) if item not in call.input]
        if missing:
            return ToolResult(call.id, call.name, "Missing required arguments: " + ", ".join(missing), is_error=True)
        try:
            result = tool.handler(**call.input)
            if inspect.isawaitable(result):
                result = await result
            text = str(result)
            return ToolResult(call.id, call.name, text[:50000], metadata={"truncated": len(text) > 50000})
        except Exception as exc:
            return ToolResult(call.id, call.name, f"{type(exc).__name__}: {exc}", is_error=True)
