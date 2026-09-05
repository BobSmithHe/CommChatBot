from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.extensions.support.execution import CodeExecutor
from app.extensions.builtin.rag import LocalRagStore
from app.extensions.builtin.workspace import WorkspaceEditor
from app.providers import ModelResponse, OpenAICompatibleProvider, ToolCall
from app.agent_runtime import AgentEvent, ToolResult
from app.bootstrap import get_container
from app.products import ChatbotMode, CodingAgentMode, ProductGateway
from app.products.common import map_agent_event
from app.products.profiles import ProductRuntimeConfig
from app.extensions.tool_catalog import ProductToolRegistry


class FakeProvider:
    def __init__(self):
        self.tool_names = set()

    async def complete(self, messages, tools, model, *, system=None):
        self.tool_names.update(tool.name for tool in tools)
        return ModelResponse(content="Test response")


class DelegatingProvider:
    def __init__(self):
        self.parent_calls = 0
        self.delegate_tool_names = set()

    async def complete(self, messages, tools, model, *, system=None):
        if "bounded read-only coding sub-agent" in str(system):
            self.delegate_tool_names = {tool.name for tool in tools}
            return ModelResponse(content="Independent review complete")
        self.parent_calls += 1
        if self.parent_calls == 1:
            return ModelResponse(content="", tool_calls=[
                ToolCall("plan-1", "update_plan", {
                    "steps": [{"description": "Inspect independently", "status": "in_progress"}],
                }),
                ToolCall("delegate-1", "delegate_task", {
                    "task": "Inspect the workspace structure",
                }),
            ])
        return ModelResponse(content="Parent completed")


def test_gateway_routes_chatbot_mode(tmp_path):
    events = asyncio.run(_collect(_gateway(tmp_path), mode="chatbot"))
    assert any(event["event"] == "status" and "Generating answer" in event["content"] for event in events)
    assert any(event["event"] == "answer" for event in events)


def test_gateway_routes_coding_agent_mode(tmp_path):
    events = asyncio.run(_collect(_gateway(tmp_path), mode="coding-agent"))
    assert any(event["event"] == "status" and "CodingAgent AgentRuntime started" in event["content"] for event in events)
    assert any(event["event"] == "turn_start" for event in events)
    assert any(event["event"] == "turn_end" for event in events)
    assert any(event["event"] == "llm_response" for event in events)
    assert any(event["event"] == "answer" for event in events)


def test_coding_agent_exposes_planning_delegation_and_memory_tools(tmp_path):
    provider = FakeProvider()
    rag = LocalRagStore(str(tmp_path / "index.json"))
    registry = ProductToolRegistry(rag, CodeExecutor(timeout=2), WorkspaceEditor(tmp_path))
    gateway = _build_gateway(provider, rag, registry)

    asyncio.run(_collect(gateway, mode="coding-agent"))

    assert {
        "update_plan", "delegate_task", "spawn_agent", "send_agent", "wait_agent",
        "interrupt_agent", "list_agents", "read_project_memory", "remember_project",
    } <= provider.tool_names


def test_coding_agent_executes_plan_and_bounded_read_only_subagent(tmp_path):
    provider = DelegatingProvider()
    rag = LocalRagStore(str(tmp_path / "index.json"))
    registry = ProductToolRegistry(rag, CodeExecutor(timeout=2), WorkspaceEditor(tmp_path))
    gateway = _build_gateway(provider, rag, registry)

    events = asyncio.run(_collect(gateway, mode="coding-agent"))

    results = "\n".join(
        str((item.get("content") or {}).get("output") or "")
        for item in events
        if item["event"] == "tool_result"
    )
    assert "Inspect independently" in results
    assert "Independent review complete" in results
    assert {"write_file", "apply_patch", "run_command"}.isdisjoint(provider.delegate_tool_names)


def test_agent_event_mapping_preserves_structured_tool_pair_and_full_output():
    started = map_agent_event(AgentEvent("tool_start", {
        "id": "call-1", "name": "read_file", "input": {"path": "main.py"},
    }))
    long_output = "x" * 5000
    ended = map_agent_event(AgentEvent("tool_end", {
        "result": ToolResult("call-1", "read_file", long_output, metadata={"truncated": False}),
    }))

    assert started == {
        "event": "tool_use",
        "content": {
            "id": "call-1", "name": "read_file", "input": {"path": "main.py"}, "recovered": False,
        },
    }
    assert ended["event"] == "tool_result"
    assert ended["content"]["id"] == "call-1"
    assert ended["content"]["output"] == long_output


def test_deepseek_thinking_controls_and_tool_reasoning_round_trip() -> None:
    provider = object.__new__(OpenAICompatibleProvider)
    provider.base_url = "https://api.deepseek.com/v1"
    provider.thinking_mode = "disabled"
    provider.reasoning_effort = "low"
    assert provider._thinking_options("deepseek-v4-flash") == {
        "extra_body": {"thinking": {"type": "disabled"}},
    }

    provider.thinking_mode = "enabled"
    assert provider._thinking_options("deepseek-v4-flash")["reasoning_effort"] == "low"
    message = provider.assistant_tool_content(
        "",
        [ToolCall("call-1", "read_file", {"path": "main.py"})],
        "internal reasoning",
    )
    assert message["reasoning_content"] == "internal reasoning"


def _gateway(tmp_path: Path) -> ProductGateway:
    provider = FakeProvider()
    rag = LocalRagStore(str(tmp_path / "index.json"))
    tools = ProductToolRegistry(rag, CodeExecutor(timeout=2), WorkspaceEditor(tmp_path))
    return _build_gateway(provider, rag, tools)


def _build_gateway(provider, rag, tools) -> ProductGateway:
    container = get_container()
    config = ProductRuntimeConfig(
        chat_model_id="test", coding_model_id="test", fallback_model_id="test",
        max_agent_turns=4, context_window=32_768, max_output_tokens=4_096,
    )
    services = dict(container.extension_services)
    return ProductGateway(
        chatbot=ChatbotMode(
            provider=provider, rag=rag, tools=tools, extensions=container.extension_registry,
            runtime_host=container.runtime_host, extension_services=services, config=config,
        ),
        coding_agent=CodingAgentMode(
            provider=provider, tools=tools, extensions=container.extension_registry,
            runtime_host=container.runtime_host, extension_services=services, config=config,
            workspace_factory=WorkspaceEditor,
        ),
    )


async def _collect(gateway: ProductGateway, mode: str) -> list[dict]:
    events = []
    async for event in gateway.stream(
        message="What is OFDM?",
        history=[],
        mode=mode,
        use_rag=False,
        use_web=False,
    ):
        events.append(event)
    return events
