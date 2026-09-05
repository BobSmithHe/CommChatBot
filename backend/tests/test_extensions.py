from __future__ import annotations

import asyncio

import pytest

from app.bootstrap import get_container
from app.agent_runtime import (
    AgentProfile, AgentRuntime, ExtensionContext, ExtensionContribution, ExtensionManifest,
    ExtensionRegistry, Tool,
)
from app.providers import ModelResponse
from app.products.profiles import CHAT_PROFILE, CODING_PROFILE, apply_profile_overrides


class FakeExtension:
    def __init__(self, extension_id: str, *, requires=(), modes=frozenset({"coding"}), tool="") -> None:
        self.manifest = ExtensionManifest(id=extension_id, requires=requires, modes=modes)
        self.tool = tool

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        order = context.require("order")
        order.append(self.manifest.id)
        tools = [Tool(self.tool, self.tool, {"type": "object"}, lambda: "ok")] if self.tool else []
        return ExtensionContribution(
            tools=tools,
            prompt_sections=[self.manifest.id],
            values={self.manifest.id: True},
        )


def test_registry_resolves_dependencies_once_and_in_order() -> None:
    registry = ExtensionRegistry()
    registry.register(FakeExtension("base"))
    registry.register(FakeExtension("feature", requires=("base",)))
    order = []

    result = asyncio.run(registry.activate(
        AgentProfile("test", ("feature", "base")),
        ExtensionContext(mode="coding", services={"order": order}),
    ))

    assert order == ["base", "feature"]
    assert result.ids == order
    assert result.prompt_sections == order
    assert result.values == {"base": True, "feature": True}


def test_registry_rejects_unknown_mode_and_dependency_cycle() -> None:
    registry = ExtensionRegistry().register(FakeExtension("chat-only", modes=frozenset({"chatbot"})))
    with pytest.raises(ValueError, match="does not support mode"):
        registry.resolve(AgentProfile("coding", ("chat-only",)), "coding")

    cyclic = ExtensionRegistry()
    cyclic.register(FakeExtension("one", requires=("two",)))
    cyclic.register(FakeExtension("two", requires=("one",)))
    with pytest.raises(ValueError, match="dependency cycle"):
        cyclic.resolve(AgentProfile("cycle", ("one",)), "coding")


def test_duplicate_tool_is_deterministically_ignored_with_warning() -> None:
    registry = ExtensionRegistry()
    registry.register(FakeExtension("first", tool="same"))
    registry.register(FakeExtension("second", tool="same"))
    result = asyncio.run(registry.activate(
        AgentProfile("test", ("first", "second")),
        ExtensionContext(mode="coding", services={"order": []}),
    ))

    assert [tool.name for tool in result.tools] == ["same"]
    assert result.events[0]["event"] == "extension_warning"


def test_builtin_profiles_are_mode_isolated_and_complete() -> None:
    registry = get_container().extension_registry
    chat = registry.resolve(CHAT_PROFILE, "chatbot")
    coding = registry.resolve(CODING_PROFILE, "coding")

    assert {item.manifest.id for item in chat} == {"chat.rag", "chat.web-search", "chat.memory"}
    assert {item.manifest.id for item in coding} >= {
        "coding.workspace", "coding.project-context", "coding.plan", "coding.memory",
        "coding.subagents", "coding.mcp", "coding.github", "coding.remote-execution",
        "coding.diagnostics", "coding.git", "coding.terminal",
    }
    assert not {item.manifest.id for item in chat} & {item.manifest.id for item in coding}


def test_profile_overrides_support_modifiers_and_explicit_replacement() -> None:
    modified = apply_profile_overrides(CODING_PROFILE, "-coding.github,+coding.github,-coding.mcp")
    replacement = apply_profile_overrides(CHAT_PROFILE, "chat.memory")

    assert modified.extensions[-1] == "coding.github"
    assert "coding.mcp" not in modified.extensions
    assert replacement.extensions == ("chat.memory",)


def test_untrusted_project_cannot_change_extension_profile(tmp_path) -> None:
    from app.extensions.builtin.project_context import ProjectContextLoader
    from app.extensions.builtin.workspace.editor import WorkspaceEditor

    config = tmp_path / ".commchat" / "settings.json"
    config.parent.mkdir()
    config.write_text('{"agent_extensions":["-coding.github"]}', encoding="utf-8")
    loader = ProjectContextLoader()

    assert loader.extension_overrides(WorkspaceEditor(tmp_path), trusted=False) == []
    assert loader.extension_overrides(WorkspaceEditor(tmp_path), trusted=True) == ["-coding.github"]


def test_extension_hook_participates_in_runtime_lifecycle() -> None:
    seen = []

    async def hook(event, _payload, _cwd):
        seen.append(event)
        return [{"event": event, "allow": True}]

    class Provider:
        async def complete(self, messages, tools, model, *, system=None):
            return ModelResponse("done")

    runtime = AgentRuntime(
        provider=Provider(), model="test", tools=[], system_prompt="test", max_turns=1,
        hook_handlers=[hook],
    )

    async def collect():
        return [event async for event in runtime.run("go")]

    events = asyncio.run(collect())
    assert seen == ["SessionStart", "Stop"]
    assert [event.type for event in events].count("hook") == 2


def test_extension_v2_validates_config_permissions_and_deactivates_once(tmp_path) -> None:
    lifecycle = []

    class LifecycleExtension:
        manifest = ExtensionManifest(
            id="test.lifecycle", permissions=frozenset({"read"}),
            config_schema={
                "type": "object", "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"], "additionalProperties": False,
            },
        )

        async def activate(self, context):
            lifecycle.append(("activate", context.config_for(self.manifest.id)["enabled"]))
            return ExtensionContribution(tools=[Tool(
                "inspect", "inspect", {"type": "object"}, lambda: "ok", capability="read",
            )])

        async def deactivate(self, _context):
            lifecycle.append(("deactivate", True))

    registry = ExtensionRegistry().register(LifecycleExtension())
    context = ExtensionContext(
        mode="coding", services={}, configs={"test.lifecycle": {"enabled": True}},
    )
    activated = asyncio.run(registry.activate(AgentProfile("test", ("test.lifecycle",)), context))
    asyncio.run(activated.close())
    asyncio.run(activated.close())
    assert lifecycle == [("activate", True), ("deactivate", True)]

    invalid = ExtensionContext(
        mode="coding", services={}, configs={"test.lifecycle": {"enabled": "yes"}},
    )
    with pytest.raises(ValueError, match="Invalid config"):
        asyncio.run(registry.activate(AgentProfile("test", ("test.lifecycle",)), invalid))
    with pytest.raises(PermissionError, match="trusted root"):
        registry.discover(tmp_path)


def test_extension_v2_rejects_undeclared_tool_capability() -> None:
    class UnsafeExtension:
        manifest = ExtensionManifest(id="test.unsafe", permissions=frozenset({"read"}))

        async def activate(self, _context):
            return ExtensionContribution(tools=[Tool(
                "write", "write", {"type": "object"}, lambda: "ok", capability="write",
            )])

    registry = ExtensionRegistry().register(UnsafeExtension())
    with pytest.raises(ValueError, match="undeclared capability"):
        asyncio.run(registry.activate(
            AgentProfile("test", ("test.unsafe",)), ExtensionContext(mode="coding", services={}),
        ))
