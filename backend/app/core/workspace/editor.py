from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ...infra.config import get_settings


EXCLUDED_PARTS = {".git", ".commchat-trash", ".idea", ".pytest_cache", "__pycache__", "node_modules", "dist", "build", "data"}
MAX_FILE_BYTES = 1_000_000
SAFE_COMMANDS = {
    "pytest": None,
    "pytest.exe": None,
    "rg": None,
    "rg.exe": None,
    "npm": {"run", "test"},
    "npm.cmd": {"run", "test"},
    "git": {"status", "diff", "log", "show"},
    "git.exe": {"status", "diff", "log", "show"},
}


class WorkspaceEditor:
    """Bounded text-file operations for the coding product mode."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root if root is not None else get_settings().workspace_dir
        self.root = Path(configured).resolve()

    def list_files(self, directory: str = ".", pattern: str = "*", limit: int = 200) -> str:
        target = self._resolve(directory)
        if not target.is_dir():
            raise ValueError(f"Not a directory: {directory}")
        results: list[str] = []
        for path in target.rglob(pattern or "*"):
            if not path.is_file() or self._excluded(path):
                continue
            results.append(path.relative_to(self.root).as_posix())
            if len(results) >= max(1, min(limit, 500)):
                break
        return "\n".join(sorted(results)) or "No files found."

    def list_entries(self, directory: str = ".", limit: int = 500) -> list[dict[str, str]]:
        """Return files and directories, including empty directories, for the editor tree."""
        target = self._resolve(directory)
        if not target.is_dir():
            raise ValueError(f"Not a directory: {directory}")
        entries: list[dict[str, str]] = []
        for path in target.rglob("*"):
            if self._excluded(path) or (not path.is_file() and not path.is_dir()):
                continue
            entries.append(
                {
                    "path": path.relative_to(self.root).as_posix(),
                    "type": "directory" if path.is_dir() else "file",
                }
            )
            if len(entries) >= max(1, min(limit, 1000)):
                break
        return sorted(entries, key=lambda item: (item["path"].casefold(), item["type"] != "directory"))

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        target = self._require_text_file(path)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        stop = min(len(lines), end_line if end_line is not None else start + 499)
        if start > len(lines) and lines:
            raise ValueError(f"start_line exceeds file length ({len(lines)})")
        return "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, stop + 1))

    def read_text(self, path: str) -> str:
        """Read raw UTF-8 text for the integrated editor."""
        return self._require_text_file(path).read_text(encoding="utf-8", errors="replace")

    def search_files(
        self,
        query: str,
        directory: str = ".",
        pattern: str = "*",
        max_results: int = 50,
    ) -> str:
        if not query:
            raise ValueError("query must not be empty")
        target = self._resolve(directory)
        if not target.is_dir():
            raise ValueError(f"Not a directory: {directory}")
        needle = query.casefold()
        matches: list[str] = []
        cap = max(1, min(max_results, 200))
        for path in target.rglob(pattern or "*"):
            if not path.is_file() or self._excluded(path) or path.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            relative = path.relative_to(self.root).as_posix()
            for line_number, line in enumerate(lines, start=1):
                if needle in line.casefold():
                    matches.append(f"{relative}:{line_number}: {line[:500]}")
                    if len(matches) >= cap:
                        return "\n".join(matches)
        return "\n".join(matches) or "No matches found."

    def write_file(self, path: str, content: str, overwrite: bool = False) -> str:
        target = self._resolve(path)
        if target.exists() and not overwrite:
            raise ValueError("File already exists; use replace_in_file or set overwrite=true")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote {len(content.encode('utf-8'))} bytes to {target.relative_to(self.root).as_posix()}"

    def create_directory(self, path: str) -> str:
        target = self._resolve(path)
        if target == self.root:
            raise ValueError("A directory path is required")
        if target.exists():
            raise ValueError("Path already exists")
        target.mkdir(parents=True)
        return f"Created directory {target.relative_to(self.root).as_posix()}"

    def delete_path(self, path: str) -> str:
        target = self._resolve(path)
        if target == self.root:
            raise ValueError("The workspace root cannot be deleted")
        if not target.exists():
            raise ValueError(f"Path not found: {path}")
        relative = target.relative_to(self.root).as_posix()
        trash_id = uuid.uuid4().hex
        trash_root = self.root / ".commchat-trash" / trash_id
        destination = trash_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(destination))
        manifest = {
            "id": trash_id,
            "path": relative,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        }
        (trash_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return json.dumps({"message": f"Moved {relative} to workspace trash", **manifest}, ensure_ascii=False)

    def list_trash(self) -> list[dict]:
        trash = self.root / ".commchat-trash"
        if not trash.is_dir():
            return []
        results = []
        for manifest in trash.glob("*/manifest.json"):
            try:
                results.append(json.loads(manifest.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(results, key=lambda item: item.get("deleted_at", ""), reverse=True)

    def restore_trash(self, trash_id: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", trash_id or ""):
            raise ValueError("Invalid trash id")
        trash_root = self.root / ".commchat-trash" / trash_id
        manifest_path = trash_root / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("Trash item not found")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative = str(manifest["path"])
        source = trash_root / relative
        target = self._resolve(relative)
        if target.exists():
            raise ValueError("Restore target already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        shutil.rmtree(trash_root, ignore_errors=True)
        return f"Restored {relative}"

    def git_status(self) -> dict:
        if not (self.root / ".git").exists():
            return {"is_repo": False, "branch": None, "changes": []}
        branch = self._git(["branch", "--show-current"]).strip() or "detached"
        lines = self._git(["status", "--short"]).splitlines()
        return {
            "is_repo": True,
            "branch": branch,
            "changes": [
                {"status": line[:2].strip() or "?", "path": line[3:].strip()}
                for line in lines if line.strip()
            ],
        }

    def git_diff(self, path: str | None = None) -> str:
        args = ["diff", "--no-ext-diff", "--"]
        if path:
            self._resolve(path)
            args.append(path)
        unstaged = self._git(args)
        staged_args = ["diff", "--cached", "--no-ext-diff", "--"] + ([path] if path else [])
        staged = self._git(staged_args)
        untracked = self._untracked_diff(path)
        sections = [section for section in (staged, unstaged, untracked) if section]
        return "\n".join(sections)[-100000:]

    def _untracked_diff(self, path: str | None = None) -> str:
        args = ["status", "--porcelain", "--untracked-files=all", "--"] + ([path] if path else [])
        output = self._git(args)
        diffs = []
        for line in output.splitlines():
            if not line.startswith("?? "):
                continue
            relative = line[3:].strip().strip('"')
            target = self._resolve(relative)
            if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
                continue
            content = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            diffs.append("".join(difflib.unified_diff([], content, fromfile="/dev/null", tofile=f"b/{relative}")))
        return "\n".join(diffs)

    def git_checkpoint(self, message: str = "Agent checkpoint") -> str:
        self._git(["add", "-A"])
        completed = self._git(["commit", "--allow-empty", "-m", message])
        return completed.strip() or "Checkpoint created"

    def git_restore(self, path: str) -> str:
        target = self._resolve(path)
        relative = target.relative_to(self.root).as_posix()
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=self.root,
            capture_output=True,
            timeout=15,
            shell=False,
        )
        if tracked.returncode != 0:
            raise ValueError("Only tracked files can be restored")
        self._git(["restore", "--staged", "--worktree", "--", relative])
        return f"Restored {relative} from HEAD"

    def _git(self, args: list[str]) -> str:
        if not (self.root / ".git").exists():
            raise ValueError("Workspace is not a Git repository")
        completed = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
        )
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or completed.stdout.strip() or "Git command failed")
        return completed.stdout

    def move_path(self, source: str, target: str) -> str:
        source_path = self._resolve(source)
        target_path = self._resolve(target)
        if source_path == self.root:
            raise ValueError("The workspace root cannot be moved")
        if not source_path.exists():
            raise ValueError(f"Path not found: {source}")
        if target_path.exists():
            raise ValueError(f"Target already exists: {target}")
        if source_path.is_dir():
            try:
                target_path.relative_to(source_path)
            except ValueError:
                pass
            else:
                raise ValueError("A directory cannot be moved inside itself")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(target_path))
        return f"Moved {source_path.relative_to(self.root).as_posix()} to {target_path.relative_to(self.root).as_posix()}"

    async def run_python_file(self, path: str, timeout: int | None = None) -> dict:
        target = self._require_text_file(path)
        if target.suffix.casefold() != ".py":
            raise ValueError("Only Python (.py) files can be run")
        cap = max(1, min(timeout or get_settings().code_exec_timeout, 120))

        def _run() -> subprocess.CompletedProcess[bytes]:
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["MPLBACKEND"] = "Agg"
            return subprocess.run(
                [sys.executable, str(target)],
                cwd=self.root,
                env=environment,
                capture_output=True,
                timeout=cap,
                shell=False,
            )

        try:
            result = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return {"exit_code": -1, "stdout": "", "stderr": f"Execution timed out after {cap}s"}
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace")[-20000:],
            "stderr": result.stderr.decode("utf-8", errors="replace")[-10000:],
        }

    def replace_in_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> str:
        if not old_text:
            raise ValueError("old_text must not be empty")
        target = self._require_text_file(path)
        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old_text)
        if occurrences == 0:
            raise ValueError("old_text was not found")
        if occurrences > 1 and not replace_all:
            raise ValueError(f"old_text occurs {occurrences} times; provide more context or set replace_all=true")
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        target.write_text(updated, encoding="utf-8")
        count = occurrences if replace_all else 1
        return f"Replaced {count} occurrence(s) in {target.relative_to(self.root).as_posix()}"

    async def run_command(self, argv: list[str], directory: str = ".", timeout: int = 60) -> str:
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ValueError("argv must be a non-empty list of strings")
        executable = Path(argv[0]).name.casefold()
        allowed_subcommands = SAFE_COMMANDS.get(executable)
        if executable not in SAFE_COMMANDS:
            raise ValueError(f"Command is not allowed: {argv[0]}")
        if allowed_subcommands is not None:
            if len(argv) < 2 or argv[1].casefold() not in allowed_subcommands:
                raise ValueError(f"Subcommand is not allowed for {argv[0]}")
        for argument in argv[1:]:
            if any(character in argument for character in "&|<>^%!\r\n"):
                raise ValueError("Shell control characters are not allowed in command arguments")
            candidate = Path(argument.split("=", 1)[-1])
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("Command arguments may not reference paths outside the workspace")

        cwd = self._resolve(directory)
        if not cwd.is_dir():
            raise ValueError(f"Not a directory: {directory}")

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, min(timeout, 120)),
                shell=False,
            )

        try:
            result = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return json.dumps(
                {"exit_code": -1, "stdout": "", "stderr": "Command timed out"},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "exit_code": result.returncode,
                "stdout": result.stdout[-20000:],
                "stderr": result.stderr[-10000:],
            },
            ensure_ascii=False,
        )

    def _resolve(self, path: str) -> Path:
        requested = Path(path or ".")
        if requested.is_absolute():
            raise ValueError("Absolute paths are not allowed")
        target = (self.root / requested).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path escapes the configured workspace") from exc
        if self._excluded(target):
            raise ValueError("Path is inside an excluded directory")
        return target

    def _require_text_file(self, path: str) -> Path:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError(f"File not found: {path}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"File exceeds {MAX_FILE_BYTES} bytes")
        return target

    def _excluded(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.root)
        except ValueError:
            return True
        return (
            any(part in EXCLUDED_PARTS for part in relative.parts)
            or relative.name == ".env"
            or relative.name.startswith(".env.")
        )
