from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.packages.agent import AgentRuntime, Tool
from app.packages.ai import ModelResponse, ModelStreamEvent, ToolCall


class FakeProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages = []

    async def complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.calls == 1:
            return ModelResponse(content="", tool_calls=[ToolCall("call-1", "lookup", {"query": "OFDM"})])
        return ModelResponse(content="OFDM uses orthogonal subcarriers.")


async def lookup(query: str) -> str:
    return f"knowledge for {query}"


def test_agent_runtime_executes_tool_then_answers():
    runtime = AgentRuntime(
        provider=FakeProvider(),
        model="test-model",
        tools=[
            Tool(
                name="lookup",
                description="Lookup knowledge.",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                handler=lookup,
            )
        ],
        system_prompt="test",
        max_turns=3,
    )

    events = asyncio.run(_collect(runtime))

    assert "tool_start" in events
    assert "tool_end" in events
    assert "message_update" in events
    assert runtime.messages[-1].content == "OFDM uses orthogonal subcarriers."
    assert any(message.type == "tool_result" and "knowledge for OFDM" in message.content for message in runtime.messages)


class FakeStreamProvider:
    async def stream_complete(self, messages, tools, model, *, system=None):
        yield ModelStreamEvent("content_delta", delta="stream ")
        yield ModelStreamEvent("content_delta", delta="works")
        yield ModelStreamEvent("response", response=ModelResponse(content="stream works"))


def test_agent_runtime_forwards_model_deltas():
    runtime = AgentRuntime(
        provider=FakeStreamProvider(),
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=1,
    )

    async def collect():
        return [event.data.get("delta") async for event in runtime.run("hello") if event.type == "message_update"]

    assert asyncio.run(collect()) == ["stream ", "works"]


async def _collect(runtime: AgentRuntime) -> list[str]:
    return [event.type async for event in runtime.run("What is OFDM?")]
