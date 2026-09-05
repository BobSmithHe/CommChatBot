from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .tools import Tool

ExtensionHook = Callable[[str, dict[str, Any], str | None], Awaitable[list[dict]] | list[dict]]


@dataclass(frozen=True)
class ExtensionManifest:
    """Stable metadata used before any extension code is activated."""

    id: str
    version: str = "1.0.0"
    modes: frozenset[str] = frozenset({"chatbot", "coding"})
    requires: tuple[str, ...] = ()
    description: str = ""
    schema_version: int = 2
    runtime_api: str = "2"
    permissions: frozenset[str] = frozenset()
    config_schema: Mapping[str, Any] = field(default_factory=lambda: {
        "type": "object", "properties": {}, "additionalProperties": True,
    })


@dataclass(frozen=True)
class AgentProfile:
    """Declarative product capability list; order is deterministic."""

    id: str
    extensions: tuple[str, ...]


@dataclass
class ExtensionContext:
    mode: str
    services: Mapping[str, Any]
    request: Mapping[str, Any] = field(default_factory=dict)
    configs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.services.get(name, default)

    def require(self, name: str) -> Any:
        if name not in self.services:
            raise RuntimeError(f"Extension service is not available: {name}")
        return self.services[name]

    def config_for(self, extension_id: str) -> Mapping[str, Any]:
        return self.configs.get(extension_id, {})


@dataclass
class ExtensionContribution:
    tools: list[Tool] = field(default_factory=list)
    hooks: list[ExtensionHook] = field(default_factory=list)
    prompt_sections: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)


class AgentExtension(Protocol):
    manifest: ExtensionManifest

    async def activate(self, context: ExtensionContext) -> ExtensionContribution: ...

    async def deactivate(self, context: ExtensionContext) -> None: ...


@dataclass
class ActivatedExtensions:
    ids: list[str] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    hooks: list[ExtensionHook] = field(default_factory=list)
    prompt_sections: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)
    _activations: list[tuple[AgentExtension, ExtensionContext]] = field(default_factory=list, repr=False)
    _closed: bool = field(default=False, repr=False)

    async def close(self) -> None:
        """Deactivate activated extensions once, in reverse dependency order."""
        if self._closed:
            return
        self._closed = True
        for extension, context in reversed(self._activations):
            deactivate = getattr(extension, "deactivate", None)
            if deactivate is not None:
                result = deactivate(context)
                if hasattr(result, "__await__"):
                    await result


class ExtensionRegistry:
    """Resolve dependencies and collect extension contributions.

    The registry only coordinates trusted in-process extensions. Project-local
    executable integrations continue to use declarative manifests, Skills, or
    MCP so that project trust and sandbox policies remain enforceable.
    """

    def __init__(self) -> None:
        self._extensions: dict[str, AgentExtension] = {}

    def register(self, extension: AgentExtension) -> "ExtensionRegistry":
        self._validate_manifest(extension.manifest)
        extension_id = extension.manifest.id
        if not extension_id or extension_id in self._extensions:
            raise ValueError(f"Duplicate or empty extension id: {extension_id}")
        self._extensions[extension_id] = extension
        return self

    def unregister(self, extension_id: str) -> bool:
        return self._extensions.pop(extension_id, None) is not None

    def load_entrypoint(self, entrypoint: str) -> AgentExtension:
        """Load a trusted installed extension from ``module:object``."""
        module_name, separator, object_name = entrypoint.partition(":")
        if not separator or not module_name or not object_name:
            raise ValueError("Extension entrypoint must use module:object")
        candidate = getattr(importlib.import_module(module_name), object_name)
        extension = candidate() if isinstance(candidate, type) else candidate
        self.register(extension)
        return extension

    def discover(self, root: str | Path, *, trusted: bool = False) -> list[str]:
        """Discover trusted ``extension.json`` manifests below one install root.

        Project-local executable features should use Skills or MCP. Dynamic
        Python imports are intentionally blocked until the caller establishes
        trust for the extension installation root.
        """
        if not trusted:
            raise PermissionError("Dynamic extension discovery requires a trusted root")
        loaded: list[str] = []
        for path in sorted(Path(root).resolve().rglob("extension.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            entrypoint = str(payload.get("entrypoint") or "")
            extension = self.load_entrypoint(entrypoint)
            declared_id = str(payload.get("id") or extension.manifest.id)
            if declared_id != extension.manifest.id:
                self.unregister(extension.manifest.id)
                raise ValueError(f"Manifest id does not match entrypoint: {declared_id}")
            loaded.append(extension.manifest.id)
        return loaded

    def manifests(self) -> list[ExtensionManifest]:
        return [extension.manifest for extension in self._extensions.values()]

    def resolve(self, profile: AgentProfile, mode: str) -> list[AgentExtension]:
        ordered: list[AgentExtension] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(extension_id: str) -> None:
            if extension_id in visited:
                return
            if extension_id in visiting:
                raise ValueError(f"Extension dependency cycle at: {extension_id}")
            extension = self._extensions.get(extension_id)
            if extension is None:
                raise ValueError(f"Unknown extension in profile {profile.id}: {extension_id}")
            if mode not in extension.manifest.modes:
                raise ValueError(f"Extension {extension_id} does not support mode {mode}")
            visiting.add(extension_id)
            for dependency in extension.manifest.requires:
                visit(dependency)
            visiting.remove(extension_id)
            visited.add(extension_id)
            ordered.append(extension)

        for extension_id in profile.extensions:
            visit(extension_id)
        return ordered

    async def activate(self, profile: AgentProfile, context: ExtensionContext) -> ActivatedExtensions:
        result = ActivatedExtensions()
        tool_names: set[str] = set()
        for extension in self.resolve(profile, context.mode):
            manifest = extension.manifest
            config = dict(context.config_for(manifest.id))
            errors = sorted(Draft202012Validator(manifest.config_schema).iter_errors(config), key=lambda item: list(item.path))
            if errors:
                raise ValueError(f"Invalid config for extension {manifest.id}: {errors[0].message}")
            try:
                contribution = await extension.activate(context)
            except Exception:
                await result.close()
                raise
            result._activations.append((extension, context))
            result.ids.append(extension.manifest.id)
            for tool in contribution.tools:
                if manifest.permissions and tool.capability not in manifest.permissions:
                    await result.close()
                    raise ValueError(
                        f"Extension {manifest.id} contributes undeclared capability: {tool.capability}"
                    )
                if tool.name in tool_names:
                    result.events.append({
                        "event": "extension_warning",
                        "content": {"extension": extension.manifest.id, "reason": f"duplicate tool ignored: {tool.name}"},
                    })
                    continue
                tool_names.add(tool.name)
                result.tools.append(tool)
            result.prompt_sections.extend(section for section in contribution.prompt_sections if section.strip())
            result.hooks.extend(contribution.hooks)
            result.events.extend(contribution.events)
            result.values.update(contribution.values)
        return result

    @staticmethod
    def _validate_manifest(manifest: ExtensionManifest) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*", manifest.id):
            raise ValueError(f"Invalid extension id: {manifest.id}")
        if manifest.schema_version != 2 or manifest.runtime_api != "2":
            raise ValueError(f"Unsupported extension runtime API for {manifest.id}")
        if manifest.id in manifest.requires:
            raise ValueError(f"Extension cannot depend on itself: {manifest.id}")
        allowed = {"read", "write", "execute", "network"}
        unknown = set(manifest.permissions) - allowed
        if unknown:
            raise ValueError(f"Unknown extension permissions for {manifest.id}: {sorted(unknown)}")
        try:
            Draft202012Validator.check_schema(manifest.config_schema)
        except SchemaError as exc:
            raise ValueError(f"Invalid config schema for extension {manifest.id}: {exc.message}") from exc
