from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.agent_runtime import AgentRuntime, NullRuntimeHost, Tool
from app.agent_runtime.tools import ToolExecutor
from app.providers import ModelResponse, ModelStreamEvent, ToolCall


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

    assert events.count("turn_start") == 2
    assert events.count("turn_end") == 2
    assert events.count("llm_response") == 2
    assert "tool_start" in events
    assert "tool_end" in events
    assert "message_update" in events
    assert runtime.messages[-1].content == "OFDM uses orthogonal subcarriers."
    assert any(message.type == "tool_result" and "knowledge for OFDM" in message.content for message in runtime.messages)


async def narrating_lookup(query: str) -> str:
    return f"found {query}"


class NarratingToolProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        if self.calls == 1:
            yield ModelStreamEvent("content_delta", delta="I will inspect first.")
            yield ModelStreamEvent(
                "response",
                response=ModelResponse(
                    content="I will inspect first.",
                    tool_calls=[ToolCall("call-narrated", "lookup", {"query": "OFDM"})],
                ),
            )
            return
        yield ModelStreamEvent("content_delta", delta="Final answer only.")
        yield ModelStreamEvent("response", response=ModelResponse(content="Final answer only."))


def test_tool_turn_narration_is_not_mixed_into_final_answer():
    runtime = AgentRuntime(
        provider=NarratingToolProvider(),
        model="test-model",
        tools=[Tool(
            name="lookup",
            description="lookup",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            handler=narrating_lookup,
        )],
        system_prompt="test",
        max_turns=2,
    )

    async def collect():
        return [event async for event in runtime.run("question")]

    events = asyncio.run(collect())
    final_chunks = [event.data["content"] for event in events if event.type == "final_answer"]
    llm_text = [event.data["text"] for event in events if event.type == "llm_response"]
    assert final_chunks == ["Final answer only."]
    assert llm_text == ["I will inspect first.", "Final answer only."]


class FakeStreamProvider:
    async def stream_complete(self, messages, tools, model, *, system=None):
        yield ModelStreamEvent("content_delta", delta="stream ")
        yield ModelStreamEvent("content_delta", delta="works")
        yield ModelStreamEvent("response", response=ModelResponse(content="stream works"))


class ThinkingStreamProvider:
    async def stream_complete(self, messages, tools, model, *, system=None):
        yield ModelStreamEvent("thinking_delta", delta="inspect ")
        yield ModelStreamEvent("thinking_delta", delta="the files")
        yield ModelStreamEvent("content_delta", delta="done")
        yield ModelStreamEvent(
            "response",
            response=ModelResponse(
                content="done",
                metadata={"reasoning_content": "inspect the files"},
            ),
        )


def test_agent_runtime_forwards_model_deltas():
    runtime = AgentRuntime(
        provider=FakeStreamProvider(),
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=1,
    )

    async def collect():
        return [event async for event in runtime.run("hello")]

    events = asyncio.run(collect())
    lifecycle = [event for event in events if event.type in {"message_start", "message_update", "message_end"}]
    assistant_events = [
        event for event in lifecycle
        if getattr(event.data.get("message"), "type", None) == "assistant"
    ]
    assert [event.data.get("delta") for event in assistant_events if event.type == "message_update"] == ["stream ", "works"]
    assert [event.type for event in assistant_events] == [
        "message_start", "message_update", "message_update", "message_end",
    ]
    assert len({event.data["message_id"] for event in assistant_events}) == 1
    assert len({id(event.data["message"]) for event in assistant_events}) == 1


def test_agent_runtime_forwards_thinking_deltas_with_message_identity():
    runtime = AgentRuntime(
        provider=ThinkingStreamProvider(),
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=1,
    )

    events = asyncio.run(_collect_events(runtime, "hello"))
    thinking = [event for event in events if event.type == "thinking"]

    assert [event.data["delta"] for event in thinking] == ["inspect ", "the files"]
    assert len({event.data["message_id"] for event in thinking}) == 1
    assert runtime.messages[-1].metadata["reasoning_content"] == "inspect the files"


def test_non_streaming_message_lifecycle_matches_streaming_shape():
    runtime = AgentRuntime(
        provider=InspectResumeProvider(),
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=1,
    )
    events = asyncio.run(_collect_events(runtime, "hello"))
    lifecycle = [
        event for event in events
        if event.type in {"message_start", "message_update", "message_end"}
        and getattr(event.data.get("message"), "type", None) == "assistant"
    ]
    assert [event.type for event in lifecycle] == ["message_start", "message_update", "message_end"]
    assert len({event.data["message_id"] for event in lifecycle}) == 1


class EmptyThenRecoveryProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(content="", metadata={"finish_reason": "length"})
        return ModelResponse(content="Recovered final answer", metadata={"finish_reason": "stop"})


def test_agent_runtime_retries_empty_terminal_response():
    provider = EmptyThenRecoveryProvider()
    runtime = AgentRuntime(
        provider=provider,
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=1,
    )

    async def collect():
        return [event async for event in runtime.run("finish the task")]

    events = asyncio.run(collect())
    llm_response = next(event for event in events if event.type == "llm_response")
    assert provider.calls == 2
    assert llm_response.data["text"] == "Recovered final answer"
    assert llm_response.data["recovered"] is True
    assert runtime.messages[-1].content == "Recovered final answer"


class ParallelProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(content="", tool_calls=[
                ToolCall("parallel-1", "slow_lookup", {"value": "one"}),
                ToolCall("parallel-2", "slow_lookup", {"value": "two"}),
            ])
        return ModelResponse(content="done")


async def slow_lookup(value: str) -> str:
    await asyncio.sleep(0.12)
    return value


def test_parallel_tools_execute_concurrently():
    runtime = AgentRuntime(
        provider=ParallelProvider(),
        model="test-model",
        tools=[Tool(
            name="slow_lookup",
            description="slow",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            handler=slow_lookup,
            execution_mode="parallel",
        )],
        system_prompt="test",
        max_turns=2,
    )
    started = time.monotonic()
    asyncio.run(_collect(runtime))
    assert time.monotonic() - started < 0.21


class ParallelFailureProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.last_messages = []

    async def complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        self.last_messages = list(messages)
        if self.calls == 1:
            return ModelResponse(content="", tool_calls=[
                ToolCall("parallel-ok", "parallel_ok", {}),
                ToolCall("parallel-fail", "parallel_fail", {}),
            ])
        return ModelResponse(content="handled tool failure")


async def parallel_ok() -> str:
    return "ok"


async def parallel_fail() -> str:
    raise ValueError("expected failure")


def test_structured_command_failure_and_timeout_are_tool_errors():
    async def failed_command() -> str:
        return json.dumps({"exit_code": 7, "stdout": "", "stderr": "failed", "timed_out": False})

    async def timed_out_command() -> str:
        return json.dumps({"exit_code": -1, "stdout": "", "stderr": "timeout", "timed_out": True})

    executor = ToolExecutor([
        Tool("failed_command", "failed", {"type": "object"}, failed_command),
        Tool("timed_out_command", "timeout", {"type": "object"}, timed_out_command),
    ])

    async def execute():
        return (
            await executor.execute(ToolCall("failed", "failed_command", {})),
            await executor.execute(ToolCall("timeout", "timed_out_command", {})),
        )

    failed, timed_out = asyncio.run(execute())
    assert failed.is_error is True
    assert failed.metadata == {"truncated": False, "exit_code": 7, "timed_out": False}
    assert timed_out.is_error is True
    assert timed_out.metadata["timed_out"] is True


def test_missing_terminal_error_is_absorbed_by_tool_executor():
    async def missing_terminal() -> str:
        raise ValueError("Terminal not found")

    executor = ToolExecutor([
        Tool("terminal_write", "write terminal", {"type": "object"}, missing_terminal),
    ])
    result = asyncio.run(executor.execute(ToolCall("terminal", "terminal_write", {})))
    assert result.is_error is True
    assert result.content == "ValueError: Terminal not found"
    assert result.metadata["error_type"] == "ValueError"


def test_parallel_tool_exception_becomes_tool_result():
    provider = ParallelFailureProvider()
    runtime = AgentRuntime(
        provider=provider,
        model="test-model",
        tools=[
            Tool("parallel_ok", "ok", {"type": "object"}, parallel_ok, execution_mode="parallel"),
            Tool("parallel_fail", "fail", {"type": "object"}, parallel_fail, execution_mode="parallel"),
        ],
        system_prompt="test",
        max_turns=2,
    )

    async def collect():
        return [event async for event in runtime.run("run both")]

    events = asyncio.run(collect())
    results = [event.data["result"] for event in events if event.type == "tool_end"]
    assert len(results) == 2
    assert next(item for item in results if item.tool_name == "parallel_fail").is_error is True
    assert runtime.messages[-1].content == "handled tool failure"


class InspectResumeProvider:
    def __init__(self) -> None:
        self.messages = []

    async def complete(self, messages, tools, model, *, system=None):
        self.messages = list(messages)
        return ModelResponse(content="resumed")


def test_runtime_resumes_from_checkpoint_without_readding_prompt():
    provider = InspectResumeProvider()
    runtime = AgentRuntime(
        provider=provider,
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=3,
    )
    resume_state = {
        "version": 1,
        "next_turn": 1,
        "messages": [
            {"type": "user", "content": "original prompt"},
            {"type": "assistant", "content": "earlier result"},
        ],
    }
    asyncio.run(_collect_resume(runtime, resume_state, prompt=None))
    assert [message.content for message in provider.messages if message.role == "user"] == ["original prompt"]
    assert runtime.messages[-1].content == "resumed"


def test_runtime_resume_can_append_new_prompt():
    provider = InspectResumeProvider()
    runtime = AgentRuntime(
        provider=provider,
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=3,
    )
    resume_state = {
        "version": 2,
        "next_turn": 1,
        "phase": "before_llm",
        "messages": [{"type": "user", "content": "original prompt"}],
    }
    asyncio.run(_collect_resume(runtime, resume_state, prompt="new follow-up"))
    assert [message.content for message in provider.messages if message.role == "user"] == [
        "original prompt", "new follow-up",
    ]


def test_approval_rejection_is_returned_to_model(monkeypatch):
    provider = FakeProvider()
    executed = []

    async def should_not_run(query: str) -> str:
        executed.append(query)
        return query

    host = NullRuntimeHost()
    monkeypatch.setattr(host, "create_approval", lambda *_args: "approval-1")

    async def reject(_approval_id):
        return False

    monkeypatch.setattr(host, "wait_for_approval", reject)
    runtime = AgentRuntime(
        provider=provider,
        model="test-model",
        tools=[Tool(
            "lookup", "lookup", {"type": "object", "required": ["query"]}, should_not_run,
            capability="execute",
        )],
        system_prompt="test",
        max_turns=2,
        task_id="task-approval",
        host=host,
    )
    events = asyncio.run(_collect_events(runtime, "approve"))
    rejected = next(event.data["result"] for event in events if event.type == "tool_end")
    assert rejected.is_error is True
    assert "rejected" in rejected.content
    assert executed == []


def test_task_cancellation_has_cancelled_stop_without_error(monkeypatch):
    seen_hooks = []
    checks = 0

    async def record(event, payload, cwd=None):
        seen_hooks.append((event, payload.get("reason")))
        return []

    def cancel_on_stream(_task_id):
        nonlocal checks
        checks += 1
        if checks >= 2:
            from app.agent_runtime import TaskCancelled
            raise TaskCancelled("cancelled")

    host = NullRuntimeHost()
    monkeypatch.setattr(host, "run_hooks", record)
    monkeypatch.setattr(host, "ensure_not_cancelled", cancel_on_stream)
    runtime = AgentRuntime(
        provider=FakeStreamProvider(),
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=1,
        task_id="task-cancel",
        host=host,
    )

    with pytest.raises(Exception, match="cancelled") as caught:
        asyncio.run(_collect_events(runtime, "cancel"))
    assert type(caught.value).__name__ == "TaskCancelled"
    assert ("Stop", "cancelled") in seen_hooks
    assert ("Stop", "error") not in seen_hooks


class FatalToolCrash(BaseException):
    pass


class CheckpointProvider:
    def __init__(self, tool_first: bool) -> None:
        self.tool_first = tool_first
        self.calls = 0
        self.seen_messages = []

    async def complete(self, messages, tools, model, *, system=None):
        self.calls += 1
        self.seen_messages = list(messages)
        if self.tool_first and self.calls == 1:
            return ModelResponse(content="", tool_calls=[ToolCall("write-1", "write_once", {})])
        return ModelResponse(content="recovered safely")


def test_checkpoint_before_tool_prevents_automatic_replay(monkeypatch):
    checkpoints = []
    executions = []

    def save(_task_id, state):
        checkpoints.append(json.loads(json.dumps(state)))

    async def crash_after_side_effect():
        executions.append("written")
        raise FatalToolCrash("process died")

    host = NullRuntimeHost()
    monkeypatch.setattr(host, "save_checkpoint", save)
    first = AgentRuntime(
        provider=CheckpointProvider(tool_first=True),
        model="test-model",
        tools=[Tool("write_once", "write", {"type": "object"}, crash_after_side_effect, capability="write")],
        system_prompt="test",
        max_turns=3,
        task_id="checkpoint-task",
        permission_mode="full-access",
        host=host,
    )
    with pytest.raises(FatalToolCrash):
        asyncio.run(_collect_events(first, "write once"))

    checkpoint = checkpoints[-1]
    assert checkpoint["version"] == 2
    assert checkpoint["phase"] == "tools_pending"
    assert checkpoint["pending_tool_calls"][0]["id"] == "write-1"

    resumed_provider = CheckpointProvider(tool_first=False)
    resumed = AgentRuntime(
        provider=resumed_provider,
        model="test-model",
        tools=[Tool("write_once", "write", {"type": "object"}, crash_after_side_effect, capability="write")],
        system_prompt="test",
        max_turns=3,
        task_id="checkpoint-task",
        permission_mode="full-access",
        host=host,
    )
    events = asyncio.run(_collect_events(resumed, None, resume_state=checkpoint))
    recovered_result = next(
        event.data["result"]
        for event in events
        if event.type == "tool_end" and event.data.get("recovered")
    )
    assert recovered_result.is_error is True
    assert recovered_result.metadata["not_replayed"] is True
    assert executions == ["written"]
    assert any(
        message.role == "tool" and "outcome is uncertain" in str(message.content)
        for message in resumed_provider.seen_messages
    )


def test_hooks_cover_session_and_normal_stop(monkeypatch):
    seen = []

    async def record(event, payload, cwd=None):
        seen.append((event, payload.get("reason")))
        return [{"allow": True, "event": event}]

    host = NullRuntimeHost()
    monkeypatch.setattr(host, "run_hooks", record)
    runtime = AgentRuntime(
        provider=FakeStreamProvider(),
        model="test-model",
        tools=[],
        system_prompt="test",
        max_turns=1,
        host=host,
    )
    events = asyncio.run(_collect(runtime))
    assert ("SessionStart", "startup") in seen
    assert ("Stop", "completed") in seen
    assert "hook" in events


async def _collect(runtime: AgentRuntime) -> list[str]:
    return [event.type async for event in runtime.run("What is OFDM?")]


async def _collect_resume(runtime: AgentRuntime, resume_state: dict, prompt: str | None) -> list[str]:
    return [event.type async for event in runtime.run(prompt, resume_state=resume_state)]


async def _collect_events(
    runtime: AgentRuntime,
    prompt: str | None,
    resume_state: dict | None = None,
):
    return [event async for event in runtime.run(prompt, resume_state=resume_state)]
