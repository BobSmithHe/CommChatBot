from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.agent_runtime import Tool
from app.extensions.builtin.workspace import WorkspaceEditor


@dataclass(frozen=True)
class SkillDescriptor:
    name: str
    description: str
    root: Path
    instruction_file: Path


class ProjectSkillRuntime:
    """Progressive, project-scoped Skill discovery and execution.

    Only metadata is added to the model context. A model explicitly activates a
    Skill by reading its complete SKILL.md and may then inspect resources or run
    scripts from that Skill. All paths remain inside the selected Skill root and
    script execution still goes through the existing workspace sandbox.
    """

    MAX_SKILLS = 32
    MAX_RESOURCE_CHARS = 100_000

    def discover(self, workspace: WorkspaceEditor) -> list[SkillDescriptor]:
        roots = (
            workspace.root / ".agents" / "skills",
            workspace.root / ".commchat" / "skills",
            workspace.root / ".pi" / "skills",
        )
        result: list[SkillDescriptor] = []
        seen: set[str] = set()
        for skills_root in roots:
            if not skills_root.is_dir():
                continue
            candidates = list(skills_root.glob("*/SKILL.md"))
            if (skills_root / "SKILL.md").is_file():
                candidates.append(skills_root / "SKILL.md")
            for instruction_file in sorted(candidates):
                name, description = self._metadata(instruction_file)
                safe_name = name or instruction_file.parent.name
                if safe_name in seen or not re.fullmatch(r"[\w.-]{1,80}", safe_name):
                    continue
                seen.add(safe_name)
                result.append(SkillDescriptor(
                    name=safe_name,
                    description=description or f"Project Skill {safe_name}",
                    root=instruction_file.parent.resolve(),
                    instruction_file=instruction_file.resolve(),
                ))
                if len(result) >= self.MAX_SKILLS:
                    return result
        return result

    def catalog_text(self, skills: list[SkillDescriptor]) -> str:
        if not skills:
            return ""
        lines = ["Available project Skills (use read_skill before following one):"]
        lines.extend(f"- {item.name}: {item.description}" for item in skills)
        return "\n".join(lines)

    def tools(self, workspace: WorkspaceEditor, skills: list[SkillDescriptor]) -> list[Tool]:
        by_name = {item.name: item for item in skills}

        def list_skills() -> str:
            return json.dumps([
                {"name": item.name, "description": item.description}
                for item in skills
            ], ensure_ascii=False)

        def read_skill(name: str) -> str:
            skill = self._require(by_name, name)
            return self._read(skill.instruction_file)

        def list_skill_resources(name: str) -> str:
            skill = self._require(by_name, name)
            rows = []
            for path in skill.root.rglob("*"):
                if path.is_file() and path != skill.instruction_file:
                    rows.append(str(path.relative_to(skill.root)).replace("\\", "/"))
                if len(rows) >= 500:
                    break
            return json.dumps(rows, ensure_ascii=False)

        def read_skill_resource(name: str, path: str) -> str:
            skill = self._require(by_name, name)
            target = self._inside(skill.root, path)
            return self._read(target)

        async def run_skill_script(name: str, path: str, args: list[str] | None = None, timeout: int = 60) -> str:
            skill = self._require(by_name, name)
            target = self._inside(skill.root, path)
            if not target.is_file() or "scripts" not in target.relative_to(skill.root).parts:
                raise ValueError("Only files inside a Skill scripts/ directory may be executed")
            suffix = target.suffix.casefold()
            launchers = {".py": "python", ".js": "node", ".ps1": "powershell", ".sh": "bash"}
            launcher = launchers.get(suffix)
            if not launcher:
                raise ValueError(f"Unsupported Skill script type: {suffix}")
            relative = str(target.relative_to(workspace.root)).replace("\\", "/")
            result = await workspace.run_command(
                argv=[launcher, relative, *[str(item) for item in (args or [])]],
                directory="",
                timeout=max(1, min(int(timeout), 300)),
            )
            return json.dumps(result, ensure_ascii=False, default=str)

        return [
            Tool("list_skills", "List project Skills and their descriptions.", {"type": "object", "properties": {}}, list_skills),
            Tool("read_skill", "Activate a project Skill by reading its complete SKILL.md.", {
                "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
            }, read_skill),
            Tool("list_skill_resources", "List supporting resources in a project Skill.", {
                "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
            }, list_skill_resources),
            Tool("read_skill_resource", "Read a referenced file inside a project Skill.", {
                "type": "object", "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                "required": ["name", "path"],
            }, read_skill_resource),
            Tool("run_skill_script", "Run a script supplied by an activated project Skill in the existing workspace sandbox.", {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}, "path": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 300},
                },
                "required": ["name", "path"],
            }, run_skill_script, capability="execute"),
        ] if skills else []

    @staticmethod
    def _require(skills: dict[str, SkillDescriptor], name: str) -> SkillDescriptor:
        skill = skills.get(name)
        if not skill:
            raise ValueError(f"Skill not found: {name}")
        return skill

    @staticmethod
    def _inside(root: Path, relative: str) -> Path:
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Skill resource escapes its root") from exc
        return candidate

    def _read(self, path: Path) -> str:
        if not path.is_file():
            raise ValueError(f"Skill resource not found: {path.name}")
        if path.stat().st_size > self.MAX_RESOURCE_CHARS * 4:
            raise ValueError("Skill resource is too large")
        return path.read_text(encoding="utf-8", errors="replace")[: self.MAX_RESOURCE_CHARS]

    @staticmethod
    def _metadata(path: Path) -> tuple[str, str]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            return "", ""
        name = path.parent.name
        description = ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            header = text[3:end] if end >= 0 else ""
            for line in header.splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "name" and value.strip():
                    name = value.strip().strip("'\"")
                elif key.strip() == "description":
                    description = value.strip().strip("'\"")
        if not description:
            for line in text.splitlines():
                clean = line.strip().lstrip("#").strip()
                if clean and not clean.startswith("---"):
                    description = clean[:300]
                    break
        return name, description


skill_runtime = ProjectSkillRuntime()
