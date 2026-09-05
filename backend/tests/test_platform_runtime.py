from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

from app.platform.services.event_bus import RedisRuntimeEventBus, RuntimeEventBatchWriter, make_event
from app.platform.services.scheduler import next_run_at
from app.extensions.builtin.mcp import MCPClientRuntime, MCPServerConfig
from app.platform.services.notifications import validate_endpoint
from app.extensions.builtin.project_context import ProjectContextLoader
from app.extensions.builtin.workspace.editor import WorkspaceEditor
from app.agent_runtime.tools import ToolExecutor
from app.providers import ToolCall


class FakeRedisStream:
    def __init__(self) -> None:
        self.rows = []

    def xadd(self, _key, fields, **_kwargs):
        stream_id = f"{len(self.rows) + 1}-0"
        self.rows.append((stream_id, fields))
        return stream_id

    def xread(self, streams, **_kwargs):
        cursor = next(iter(streams.values()))
        cursor_number = int(cursor.split("-", 1)[0])
        rows = [row for row in self.rows if int(row[0].split("-", 1)[0]) > cursor_number]
        return [("events", rows)] if rows else []


def test_redis_event_bus_preserves_structured_events_and_cursor() -> None:
    bus = RedisRuntimeEventBus()
    bus._redis = FakeRedisStream()
    event = make_event("a" * 32, "tool_update", {"stage": "running", "progress": 0.5})
    bus.publish(event)
    cursor, rows = bus.read("a" * 32)

    assert cursor == "1-0"
    assert rows == [{
        "event_key": event.event_key, "event": "tool_update",
        "content": {"stage": "running", "progress": 0.5},
    }]


def test_runtime_event_writer_batches_and_flushes() -> None:
    writer = RuntimeEventBatchWriter()
    writer.settings = SimpleNamespace(
        database_backend="mysql", runtime_event_batch_size=10, runtime_event_flush_ms=10_000,
    )
    persisted = []
    def persist(events):
        persisted.extend(events)
        return True
    writer._persist = persist
    writer.append(make_event("a" * 32, "status", "one"))
    writer.append(make_event("a" * 32, "status", "two"))

    assert writer.flush(timeout=2)
    writer.close()
    assert [item.event_type for item in persisted] == ["status", "status"]


def test_skill_runtime_progressive_activation_and_resource_access(tmp_path) -> None:
    root = tmp_path / ".agents" / "skills" / "review"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: review\ndescription: Review safely\n---\nFull private instructions", encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "rules.md").write_text("Rule A", encoding="utf-8")
    bundle = ProjectContextLoader().load(WorkspaceEditor(tmp_path), trusted=True)
    assert "Review safely" in bundle.text
    assert "Full private instructions" not in bundle.text
    executor = ToolExecutor(bundle.tools)
    skill = asyncio.run(executor.execute(ToolCall("1", "read_skill", {"name": "review"})))
    resource = asyncio.run(executor.execute(ToolCall("2", "read_skill_resource", {
        "name": "review", "path": "references/rules.md",
    })))
    assert "Full private instructions" in skill.content
    assert resource.content == "Rule A"


def test_mcp_config_supports_stdio_http_and_environment_expansion(tmp_path, monkeypatch) -> None:
    config = tmp_path / ".commchat" / "mcp.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"servers": {
        "local": {"command": "python", "args": ["server.py"], "env": {"TOKEN": "${MCP_TEST_TOKEN}"}},
        "remote": {"url": "https://mcp.example.test", "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"}},
    }}), encoding="utf-8")
    monkeypatch.setenv("MCP_TEST_TOKEN", "secret")
    servers = MCPClientRuntime()._configs(WorkspaceEditor(tmp_path))
    assert {item.name for item in servers} == {"local", "remote"}
    assert servers[0].env["TOKEN"] == "secret"
    assert servers[1].headers["Authorization"] == "Bearer secret"


def test_rrule_is_evaluated_in_the_requested_timezone() -> None:
    result = next_run_at(
        interval_seconds=3600, rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
        timezone="Asia/Shanghai", after=datetime(2026, 1, 1, 0, 0, 0),
    )
    assert result == datetime(2026, 1, 1, 1, 0, 0)


def test_notification_endpoint_validation(monkeypatch) -> None:
    assert validate_endpoint("email", "ops@example.test") == "ops@example.test"
    assert validate_endpoint("webhook", "https://hooks.example.test/notify").startswith("https://")
    with __import__("pytest").raises(ValueError):
        validate_endpoint("webhook", "http://127.0.0.1/hook")


def test_mcp_requests_reuse_one_long_lived_session(tmp_path) -> None:
    runtime = MCPClientRuntime()
    opened = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="one")])

    @asynccontextmanager
    async def fake_session(_server, _cwd):
        opened.append(True)
        yield Session()

    runtime._session = fake_session

    async def scenario():
        server = MCPServerConfig(name="fake", url="https://example.test/mcp")
        first = await runtime._list(server, tmp_path)
        second = await runtime._list(server, tmp_path)
        await runtime.close()
        return first, second

    first, second = asyncio.run(scenario())
    assert first[0].name == second[0].name == "one"
    assert len(opened) == 1
