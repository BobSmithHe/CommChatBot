from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ...infra.config import get_settings
from ..sandbox import run_sandboxed
from .language import WorkspaceLanguageService


EXCLUDED_PARTS = {".git", ".commchat-trash", ".idea", ".pytest_cache", "__pycache__", "node_modules", "dist", "build", "data"}
MAX_FILE_BYTES = 1_000_000
SAFE_COMMANDS = {
    "pytest": None,
    "pytest.exe": None,
    "python": None,
    "python.exe": None,
    "python3": None,
    "python3.exe": None,
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
        for path, kind in self._walk(target):
            if kind != "file" or not fnmatch.fnmatch(path.name, pattern or "*"):
                continue
            results.append(path.relative_to(self.root).as_posix())
            if len(results) >= max(1, min(limit, 500)):
                break
        return "\n".join(sorted(results)) or "No files found."

    def list_entries(self, directory: str = ".", limit: int = 500) -> list[dict]:
        """Return files and directories, including empty directories, for the editor tree."""
        target = self._resolve(directory)
        if not target.is_dir():
            raise ValueError(f"Not a directory: {directory}")
        entries: list[dict] = []
        for path, kind in self._walk(target):
            item = {"path": path.relative_to(self.root).as_posix(), "type": kind}
            if kind == "file":
                item["version"] = self.file_version_path(path)
            entries.append(item)
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

    def file_version(self, path: str) -> str:
        return self.file_version_path(self._require_text_file(path))

    @staticmethod
    def file_version_path(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"

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
        for path, kind in self._walk(target):
            if kind != "file" or not fnmatch.fnmatch(path.name, pattern or "*") or path.stat().st_size > MAX_FILE_BYTES:
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

    def apply_patch(self, patch: str) -> str:
        """Validate and atomically apply a workspace-bounded unified diff."""
        if not patch or not patch.strip():
            raise ValueError("Patch must not be empty")
        if len(patch.encode("utf-8")) > 500_000:
            raise ValueError("Patch exceeds 500000 bytes")
        if "GIT binary patch" in patch or "Binary files " in patch:
            raise ValueError("Binary patches are not supported")
        changed_paths = self._patch_paths(patch)
        if not changed_paths:
            raise ValueError("Patch does not contain unified diff file headers")

        command = ["git", "apply", "--recount", "--whitespace=nowarn", "-"]
        checked = subprocess.run(
            [*command[:2], "--check", *command[2:]],
            cwd=self.root,
            input=patch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            check=False,
        )
        if checked.returncode != 0:
            raise ValueError(checked.stderr.strip() or checked.stdout.strip() or "Patch validation failed")
        applied = subprocess.run(
            command,
            cwd=self.root,
            input=patch,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=False,
            check=False,
        )
        if applied.returncode != 0:
            raise ValueError(applied.stderr.strip() or applied.stdout.strip() or "Patch application failed")
        return json.dumps(
            {
                "message": f"Applied patch to {len(changed_paths)} file(s)",
                "changed_files": changed_paths,
                "diagnostics": self.diagnostics(changed_paths),
                "diff": patch[-50000:],
            },
            ensure_ascii=False,
        )

    def diagnostics(self, paths: list[str] | None = None) -> list[dict]:
        """Return LSP/Ruff diagnostics for selected or currently changed Python files."""
        selected = list(dict.fromkeys(paths or self._changed_paths()))
        if not selected:
            selected = [line for line in self.list_files(pattern="*.py", limit=50).splitlines() if line]
        service = WorkspaceLanguageService(self.root)
        results: list[dict] = []
        for relative in selected[:50]:
            try:
                target = self._resolve(relative)
            except ValueError:
                continue
            if not target.is_file() or target.suffix.casefold() != ".py":
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            for item in service.diagnostics(relative, content):
                results.append({"path": relative, **item})
        return results

    def review_changes(self) -> str:
        """Inspect the current diff and static diagnostics before finalizing work."""
        diagnostics = self.diagnostics()
        try:
            diff = self.git_diff()
        except ValueError:
            diff = ""
        return json.dumps(
            {
                "diff": diff[-50000:] or "No workspace changes.",
                "diagnostics": diagnostics,
                "diagnostic_count": len(diagnostics),
            },
            ensure_ascii=False,
        )

    def project_identity(self) -> str:
        metadata = self.root / ".git" / "commchat-project.json"
        if metadata.is_file():
            try:
                project_id = str(json.loads(metadata.read_text(encoding="utf-8")).get("project_id") or "")
                if project_id:
                    return project_id
            except (OSError, ValueError):
                pass
        if (self.root / ".git").exists():
            completed = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                check=False,
            )
            remote = completed.stdout.strip()
            if completed.returncode == 0 and remote:
                parsed = urlsplit(remote)
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    port = f":{parsed.port}" if parsed.port else ""
                    remote = urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", parsed.path, "", ""))
                return f"git:{remote.casefold()}"
        return f"path:{str(self.root).casefold()}"

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

    def git_branches(self) -> dict:
        current = self._git(["branch", "--show-current"]).strip() or "detached"
        output = self._git(["branch", "--format=%(refname:short)"])
        return {"current": current, "branches": [line.strip() for line in output.splitlines() if line.strip()]}

    def git_switch_branch(self, name: str, create: bool = False) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", name or "") or ".." in Path(name).parts:
            raise ValueError("Invalid Git branch")
        if self.git_status()["changes"]:
            raise ValueError("Commit, checkpoint, or discard workspace changes before switching branches")
        args = ["switch", "-c", name] if create else ["switch", name]
        return self._git(args).strip() or f"Switched to {name}"

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

        def _run():
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["MPLBACKEND"] = "Agg"
            return run_sandboxed(
                [sys.executable, str(target)],
                cwd=self.root,
                env=environment,
                timeout=cap,
            )

        result = await asyncio.to_thread(_run)
        if result.timed_out:
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

    def _changed_paths(self) -> list[str]:
        if not (self.root / ".git").exists():
            return []
        output = self._git(["status", "--porcelain", "--untracked-files=all"])
        paths: list[str] = []
        for line in output.splitlines():
            raw = line[3:].strip().strip('"')
            if " -> " in raw:
                raw = raw.split(" -> ", 1)[1]
            if raw:
                paths.append(raw.replace("\\", "/"))
        return paths

    def _patch_paths(self, patch: str) -> list[str]:
        paths: list[str] = []
        for line in patch.splitlines():
            if not line.startswith(("--- ", "+++ ")):
                continue
            raw = line[4:].split("\t", 1)[0].strip().strip('"')
            if raw == "/dev/null":
                continue
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            if not raw or "\x00" in raw:
                raise ValueError("Patch contains an invalid path")
            self._resolve(raw)
            paths.append(raw.replace("\\", "/"))
        return list(dict.fromkeys(paths))

    async def run_command(self, argv: list[str], directory: str = ".", timeout: int = 60) -> str:
        """Run one bounded Agent command through the configured sandbox.

        In Docker mode the workspace is bind-mounted at /workspace. Directory
        tracking in interactive terminals is unrelated and is not a security
        boundary for this Agent execution path.
        """
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

        def _run():
            return run_sandboxed(
                argv,
                cwd=cwd,
                timeout=max(1, min(timeout, 120)),
            )

        result = await asyncio.to_thread(_run)
        if result.timed_out:
            return json.dumps(
                {"exit_code": -1, "stdout": "", "stderr": "Command timed out", "timed_out": True},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "exit_code": result.returncode,
                "stdout": result.stdout.decode("utf-8", errors="replace")[-20000:],
                "stderr": result.stderr.decode("utf-8", errors="replace")[-10000:],
                "timed_out": False,
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

    def _walk(self, target: Path):
        """Walk without descending into dependency, generated, or secret trees."""
        for current, directories, files in os.walk(target, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if name not in EXCLUDED_PARTS and not name.startswith(".env")
            )
            for name in directories:
                path = current_path / name
                if not self._excluded(path):
                    yield path, "directory"
            for name in sorted(files):
                path = current_path / name
                if not self._excluded(path):
                    yield path, "file"
