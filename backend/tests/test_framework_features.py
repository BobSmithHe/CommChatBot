from __future__ import annotations

import asyncio
import json

from app.agent_runtime.context import compact_llm_messages
from app.extensions.builtin.project_context import ProjectContextLoader
from app.extensions.builtin.workspace.editor import WorkspaceEditor
from app.agent_runtime import AgentRuntime, AgentSessionConfig, NullRuntimeHost, Tool, create_agent_session
from app.agent_runtime.tools import ToolExecutor
from app.providers import LLMMessage, ModelResponse, ProviderRegistry, ToolCall


def test_tool_executor_validates_complete_json_schema() -> None:
    called = []

    def handler(count: int) -> str:
        called.append(count)
        return "ok"

    tool = Tool(
        "count",
        "count",
        {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1}},
            "required": ["count"],
            "additionalProperties": False,
        },
        handler,
    )
    result = asyncio.run(ToolExecutor([tool]).execute(ToolCall("1", "count", {"count": "1", "extra": True})))

    assert result.is_error is True
    assert result.metadata["error_type"] == "ValidationError"
    assert called == []


class ToolProgressProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse("", [ToolCall("call-1", "echo", {"value": "ok"})])
        return ModelResponse("done")


def test_runtime_emits_tool_progress_event() -> None:
    runtime = AgentRuntime(
        provider=ToolProgressProvider(),
        model="test",
        tools=[Tool("echo", "echo", {"type": "object", "properties": {"value": {"type": "string"}}}, lambda value: value)],
        system_prompt="test",
        max_turns=2,
    )

    async def collect():
        return [event async for event in runtime.run("go")]

    events = asyncio.run(collect())
    assert any(event.type == "tool_update" and event.data["stage"] == "running" for event in events)
    assert any(event.type == "tool_update" and event.data["stage"] == "completed" for event in events)


class FollowUpProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        return ModelResponse("first" if self.calls == 1 else "second")


def test_follow_up_extends_runtime_at_turn_boundary(monkeypatch) -> None:
    provider = FollowUpProvider()
    polls = 0

    def consume(_task_id, kinds):
        nonlocal polls
        polls += 1
        if "follow_up" in kinds and polls == 2:
            return [{"id": 1, "kind": "follow_up", "content": "continue"}]
        return []

    host = NullRuntimeHost()
    monkeypatch.setattr(host, "consume_messages", consume)
    runtime = AgentRuntime(
        provider=provider,
        model="test",
        tools=[],
        system_prompt="test",
        max_turns=1,
        task_id="a" * 32,
        host=host,
    )

    async def collect():
        return [event async for event in runtime.run("start")]

    events = asyncio.run(collect())
    assert provider.calls == 2
    assert any(event.type == "queued_message" for event in events)
    assert [event.data["delta"] for event in events if event.type == "message_update"][-1] == "second"


class SummaryProvider:
    async def complete(self, messages, tools, model, *, system=None):
        return ModelResponse(
            "## Goal\nKeep facts\n## Next Steps\nContinue",
            metadata={"usage": {"input_tokens": 20, "output_tokens": 10}},
        )


def test_llm_structured_context_compaction() -> None:
    messages = [LLMMessage("user" if index % 2 == 0 else "assistant", "x" * 1200) for index in range(10)]
    result = asyncio.run(compact_llm_messages(messages, provider=SummaryProvider(), model="test", max_tokens=1200))

    assert result.compacted is True
    assert result.summary.startswith("## Goal")
    assert result.messages[0].role == "system"
    assert result.tokens_after < result.tokens_before


def test_trusted_project_context_discovers_skills_and_extensions(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("Use pytest.", encoding="utf-8")
    skill = tmp_path / ".agents" / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Review changed files.", encoding="utf-8")
    extensions = tmp_path / ".commchat" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "status.json").write_text(json.dumps({
        "name": "project_status",
        "description": "Show status",
        "argv": ["git", "status", "--short"],
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "capability": "read",
    }), encoding="utf-8")
    loader = ProjectContextLoader()
    editor = WorkspaceEditor(tmp_path)

    untrusted = loader.load(editor, trusted=False)
    trusted = loader.load(editor, trusted=True)

    assert "AGENTS.md" in untrusted.discovered
    assert untrusted.text == ""
    assert "Use pytest." in trusted.text
    assert "review: Review changed files." in trusted.text
    assert "Review changed files." not in trusted.text.split("Available project Skills", 1)[0]
    assert [tool.name for tool in trusted.tools] == [
        "project_status", "list_skills", "read_skill", "list_skill_resources",
        "read_skill_resource", "run_skill_script",
    ]


def test_provider_catalog_and_programmatic_sdk() -> None:
    registry = ProviderRegistry()
    ids = {item["id"] for item in registry.list_public()}
    assert {"deepseek", "anthropic"}.issubset(ids)
    provider = FollowUpProvider()
    session = create_agent_session(AgentSessionConfig(provider=provider, model="test", system_prompt="test"))
    assert session.runtime.model == "test"
