from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent_runtime import Tool
from app.extensions.builtin.workspace import WorkspaceEditor
from app.platform.services.lazy import LazyService

from .skills import ProjectSkillRuntime


@dataclass
class ProjectContextBundle:
    text: str = ""
    tools: list[Tool] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)


class ProjectContextLoader:
    """Discover trusted, project-scoped instructions, skills and declarative tools."""

    MAX_CONTEXT_CHARS = 24_000
    MAX_SKILLS = 12
    MAX_EXTENSIONS = 20

    def __init__(self, skills: ProjectSkillRuntime | None = None) -> None:
        self.skills = skills or ProjectSkillRuntime()

    def load(self, workspace: WorkspaceEditor, *, trusted: bool) -> ProjectContextBundle:
        root = workspace.root.resolve()
        discovered = self.discover(root)
        if not trusted:
            return ProjectContextBundle(discovered=discovered)

        sections: list[str] = []
        for candidate in (root / "AGENTS.md", root / ".commchat" / "AGENTS.md"):
            content = self._read_text(candidate)
            if content:
                sections.append(f"Project instructions ({candidate.relative_to(root)}):\n{content}")

        settings_path = root / ".commchat" / "settings.json"
        settings = self._read_json(settings_path)
        if isinstance(settings, dict) and str(settings.get("instructions") or "").strip():
            sections.append("Project settings instructions:\n" + str(settings["instructions"]).strip())

        skills = self.skills.discover(workspace)
        enabled_skills = set(settings.get("enabled_skills") or []) if isinstance(settings, dict) else set()
        if enabled_skills:
            skills = [item for item in skills if item.name in enabled_skills]
        catalog = self.skills.catalog_text(skills[: self.MAX_SKILLS])
        if catalog:
            sections.append(catalog)

        tools = self._load_extensions(root, workspace, settings)
        tools.extend(self.skills.tools(workspace, skills[: self.MAX_SKILLS]))
        text = "\n\n".join(sections)
        if len(text) > self.MAX_CONTEXT_CHARS:
            text = text[: self.MAX_CONTEXT_CHARS] + "\n[project context truncated]"
        return ProjectContextBundle(text=text, tools=tools, discovered=discovered)

    def discover(self, root: Path) -> list[str]:
        candidates = [root / "AGENTS.md", root / ".commchat" / "AGENTS.md", root / ".commchat" / "settings.json"]
        for pattern in (".agents/skills/*/SKILL.md", ".commchat/skills/*/SKILL.md", ".pi/skills/*/SKILL.md", ".commchat/extensions/*.json"):
            candidates.extend(root.glob(pattern))
        return [str(path.relative_to(root)).replace("\\", "/") for path in candidates if path.is_file()]

    def extension_overrides(self, workspace: WorkspaceEditor, *, trusted: bool) -> list[str]:
        """Read profile filters only after the project has been trusted."""
        if not trusted:
            return []
        settings = self._read_json(workspace.root.resolve() / ".commchat" / "settings.json")
        values = settings.get("agent_extensions", []) if isinstance(settings, dict) else []
        if not isinstance(values, list):
            return []
        return [str(item).strip() for item in values[:100] if str(item).strip()]

    def _load_extensions(self, root: Path, workspace: WorkspaceEditor, settings: Any) -> list[Tool]:
        if isinstance(settings, dict) and settings.get("extensions_enabled") is False:
            return []
        tools: list[Tool] = []
        extension_dir = root / ".commchat" / "extensions"
        if not extension_dir.is_dir():
            return tools
        for path in sorted(extension_dir.glob("*.json"))[: self.MAX_EXTENSIONS]:
            manifest = self._read_json(path)
            if not isinstance(manifest, dict):
                continue
            name = str(manifest.get("name") or "").strip()
            argv_template = manifest.get("argv")
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{1,63}", name) or not isinstance(argv_template, list):
                continue
            schema = manifest.get("input_schema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            capability = str(manifest.get("capability") or "execute")
            if capability not in {"read", "write", "execute", "network"}:
                capability = "execute"
            execution_mode = str(manifest.get("execution_mode") or "sequential")
            if execution_mode not in {"sequential", "parallel"}:
                execution_mode = "sequential"

            timeout = max(1, min(int(manifest.get("timeout") or 30), 300))

            async def handler(_template=tuple(str(item) for item in argv_template), _timeout=timeout, **kwargs):
                argv: list[str] = []
                for token in _template:
                    match = re.fullmatch(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", token)
                    if not match:
                        argv.append(token)
                        continue
                    value = kwargs.get(match.group(1), "")
                    if isinstance(value, list):
                        argv.extend(str(item) for item in value)
                    elif isinstance(value, (dict, tuple)):
                        argv.append(json.dumps(value, ensure_ascii=False))
                    else:
                        argv.append(str(value))
                return await workspace.run_command(argv=argv, directory="", timeout=_timeout)

            tools.append(Tool(
                name=name,
                description=str(manifest.get("description") or f"Project extension from {path.name}"),
                input_schema=schema,
                handler=handler,
                execution_mode=execution_mode,
                capability=capability,
            ))
        return tools

    @staticmethod
    def _read_text(path: Path, *, limit: int = 12_000) -> str:
        try:
            if path.is_file() and path.stat().st_size <= 1_000_000:
                return path.read_text(encoding="utf-8", errors="replace")[:limit].strip()
        except OSError:
            pass
        return ""

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            if path.is_file() and path.stat().st_size <= 200_000:
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        return None


project_context_loader = LazyService(ProjectContextLoader)
