from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from ..providers import ToolCall


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
        try:
            Draft202012Validator.check_schema(tool.input_schema or {"type": "object"})
            validator = Draft202012Validator(tool.input_schema or {"type": "object"})
        except SchemaError as exc:
            return ToolResult(
                call.id,
                call.name,
                f"Invalid tool schema: {exc.message}",
                is_error=True,
                metadata={"error_type": "SchemaError"},
            )
        errors = sorted(validator.iter_errors(call.input), key=lambda item: list(item.absolute_path))
        if errors:
            details = []
            for error in errors[:8]:
                path = ".".join(str(item) for item in error.absolute_path) or "$"
                details.append(f"{path}: {error.message}")
            return ToolResult(
                call.id,
                call.name,
                "Tool input validation failed: " + "; ".join(details),
                is_error=True,
                metadata={"error_type": "ValidationError", "validation_errors": details},
            )
        try:
            result = tool.handler(**call.input)
            if inspect.isawaitable(result):
                result = await result
            text = str(result)
            metadata: dict[str, Any] = {"truncated": len(text) > 50000}
            is_error = False
            try:
                structured = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                structured = None
            if isinstance(structured, dict) and "exit_code" in structured:
                exit_code = int(structured.get("exit_code", -1))
                timed_out = bool(structured.get("timed_out", False))
                metadata.update({"exit_code": exit_code, "timed_out": timed_out})
                is_error = timed_out or exit_code != 0
            return ToolResult(
                call.id,
                call.name,
                text[:50000],
                is_error=is_error,
                metadata=metadata,
            )
        except Exception as exc:
            return ToolResult(
                call.id,
                call.name,
                f"{type(exc).__name__}: {exc}",
                is_error=True,
                metadata={"error_type": type(exc).__name__},
            )
