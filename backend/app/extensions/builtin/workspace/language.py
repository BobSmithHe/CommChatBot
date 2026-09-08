from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .lsp import LspUnavailable, lsp_manager


class WorkspaceLanguageService:
    """Workspace-scoped language intelligence used by Monaco providers."""

    def __init__(self, root: str | Path, *, manager=None) -> None:
        self.root = Path(root).resolve()
        self.manager = manager or lsp_manager

    def complete(self, path: str, content: str, line: int, column: int) -> list[dict]:
        target = self._resolve(path)
        if target.suffix.lower() != ".py":
            return []
        try:
            items = self.manager.session(self.root).completion(path, content, line, column)
            if items:
                return items
        except (LspUnavailable, OSError, TimeoutError, ValueError):
            pass
        import jedi

        script = jedi.Script(
            code=content,
            path=str(target),
            project=jedi.Project(path=str(self.root)),
        )
        items = []
        for completion in script.complete(max(1, line), max(0, column - 1))[:100]:
            items.append({
                "label": completion.name,
                "insert_text": completion.complete,
                "kind": completion.type,
                "detail": completion.description,
                "documentation": completion.docstring(raw=True, fast=True)[:4000],
            })
        return items

    def diagnostics(self, path: str, content: str) -> list[dict]:
        target = self._resolve(path)
        if target.suffix.lower() != ".py":
            return []
        lsp_items: list[dict] = []
        try:
            lsp_items = self.manager.session(self.root).diagnostics(path, content)
        except (LspUnavailable, OSError, TimeoutError, ValueError):
            pass
        command = self._ruff_command()
        if command:
            result = subprocess.run(
                [*command, "check", "--output-format", "json", "--stdin-filename", str(target), "-"],
                cwd=self.root,
                input=content,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                shell=False,
            )
            try:
                entries = json.loads(result.stdout or "[]")
            except ValueError:
                entries = []
            ruff_items = [
                {
                    "message": item.get("message") or "Python diagnostic",
                    "code": item.get("code"),
                    "severity": "error" if str(item.get("code") or "").startswith(("E", "F")) else "warning",
                    "start_line": max(1, int((item.get("location") or {}).get("row") or 1)),
                    "start_column": max(1, int((item.get("location") or {}).get("column") or 1)),
                    "end_line": max(1, int((item.get("end_location") or {}).get("row") or 1)),
                    "end_column": max(1, int((item.get("end_location") or {}).get("column") or 1)),
                }
                for item in entries
            ]
            seen = {(item["start_line"], item["start_column"], item["message"]) for item in ruff_items}
            return ruff_items + [
                item for item in lsp_items
                if (item["start_line"], item["start_column"], item["message"]) not in seen
            ]
        try:
            ast.parse(content, filename=str(target))
            return []
        except SyntaxError as exc:
            return [{
                "message": exc.msg,
                "code": "SyntaxError",
                "severity": "error",
                "start_line": exc.lineno or 1,
                "start_column": exc.offset or 1,
                "end_line": exc.end_lineno or exc.lineno or 1,
                "end_column": exc.end_offset or (exc.offset or 1) + 1,
            }]

    def format(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if target.suffix.lower() != ".py":
            return content
        command = self._ruff_command()
        if not command:
            return content
        result = subprocess.run(
            [*command, "format", "--stdin-filename", str(target), "-"],
            cwd=self.root,
            input=content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            shell=False,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "Python formatter failed")
        return result.stdout

    def definition(self, path: str, content: str, line: int, column: int) -> list[dict]:
        target = self._resolve(path)
        if target.suffix.lower() != ".py":
            return []
        try:
            items = self.manager.session(self.root).definition(path, content, line, column)
            if items:
                return items
        except (LspUnavailable, OSError, TimeoutError, ValueError):
            pass
        import jedi

        script = jedi.Script(
            code=content,
            path=str(target),
            project=jedi.Project(path=str(self.root)),
        )
        results = []
        for definition in script.goto(
            max(1, line), max(0, column - 1), follow_imports=True, follow_builtin_imports=False
        ):
            module_path = Path(definition.module_path).resolve() if definition.module_path else target
            try:
                relative = module_path.relative_to(self.root).as_posix()
            except ValueError:
                continue
            try:
                target_content = content if module_path == target else module_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            results.append({
                "path": relative,
                "line": max(1, int(definition.line or 1)),
                "column": max(1, int(definition.column or 0) + 1),
                "content": target_content,
            })
        return results[:20]

    def hover(self, path: str, content: str, line: int, column: int) -> dict | None:
        self._resolve(path)
        return self.manager.session(self.root).hover(path, content, line, column)

    def references(self, path: str, content: str, line: int, column: int) -> list[dict]:
        self._resolve(path)
        return self.manager.session(self.root).references(path, content, line, column)

    def rename(self, path: str, content: str, line: int, column: int, new_name: str) -> list[dict]:
        self._resolve(path)
        return self.manager.session(self.root).rename(path, content, line, column, new_name)

    def _resolve(self, path: str) -> Path:
        if not path or "\x00" in path:
            raise ValueError("A workspace-relative file path is required")
        candidate = (self.root / path.replace("\\", "/")).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path escapes the workspace") from exc
        return candidate

    @staticmethod
    def _ruff_command() -> list[str] | None:
        # Calling Ruff through the active interpreter is reliable when uvicorn
        # is launched by absolute Python path and the environment's Scripts
        # directory is therefore absent from PATH.
        if importlib.util.find_spec("ruff") is not None:
            return [sys.executable, "-m", "ruff"]
        executable = shutil.which("ruff")
        return [executable] if executable else None
