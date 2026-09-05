from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

from app.infra.config import get_settings
from .editor import WorkspaceEditor


WORKSPACE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class ConversationWorkspaceManager:
    """Creates and resolves an isolated filesystem root for each coding conversation."""

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root if root is not None else get_settings().coding_workspace_dir
        self.root = Path(configured).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self) -> str:
        workspace_id = uuid.uuid4().hex
        target = self._path(workspace_id, create=True)
        self._initialize_git(target)
        self._write_project_identity(target, f"workspace:{workspace_id}")
        return workspace_id

    def fork(self, source_workspace_id: str) -> str:
        """Create an independent filesystem snapshot for a conversation branch."""
        source = self._path(source_workspace_id, create=False)
        if not source.is_dir():
            raise ValueError("Source workspace does not exist")
        workspace_id = uuid.uuid4().hex
        target = self._path(workspace_id, create=False)
        shutil.copytree(source, target, ignore=self._copy_ignore)
        if not (target / ".git").exists():
            self._initialize_git(target)
        return workspace_id

    def import_workspace(self, source: str, kind: str = "local", branch: str = "main") -> str:
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch or "") or ".." in Path(branch).parts:
            raise ValueError("Invalid Git branch")
        workspace_id = uuid.uuid4().hex
        target = self._path(workspace_id, create=False)
        timeout = max(30, min(get_settings().git_clone_timeout_seconds, 600))
        try:
            if kind == "clone":
                parsed = urlparse(source)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise ValueError("Only HTTP(S) Git clone URLs are allowed")
                self._git_clone(source, target, branch, timeout)
            else:
                origin = self._allowed_local_source(source)
                if kind == "worktree":
                    if not (origin / ".git").exists():
                        raise ValueError("Git worktree requires a repository source")
                    completed = subprocess.run(
                        ["git", "worktree", "add", "--detach", str(target), branch],
                        cwd=origin,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        shell=False,
                    )
                    if completed.returncode != 0:
                        raise ValueError(completed.stderr.strip() or "Unable to create Git worktree")
                elif (origin / ".git").exists():
                    self._git_clone(str(origin), target, branch, timeout)
                else:
                    shutil.copytree(origin, target, ignore=self._copy_ignore)
                    self._initialize_git(target)
            if kind == "clone":
                port = f":{parsed.port}" if parsed.port else ""
                identity = f"clone:{parsed.scheme}://{parsed.hostname}{port}{parsed.path}".casefold()
            else:
                identity = f"local:{origin}".casefold()
            self._write_project_identity(target, identity)
            return workspace_id
        except Exception:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise

    def editor(self, workspace_id: str) -> WorkspaceEditor:
        target = self._path(workspace_id, create=True)
        if not (target / ".git").exists():
            self._initialize_git(target)
        return WorkspaceEditor(target)

    def _path(self, workspace_id: str, *, create: bool) -> Path:
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id or ""):
            raise ValueError("Invalid workspace id")
        target = (self.root / workspace_id).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Workspace escapes the configured root") from exc
        if create:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def _allowed_local_source(self, source: str) -> Path:
        candidate = Path(source).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError("Local project directory does not exist")
        configured = [item.strip() for item in get_settings().workspace_import_roots.split(";") if item.strip()]
        roots = [Path(item).expanduser().resolve() for item in configured]
        if not any(self._is_relative_to(candidate, root) for root in roots):
            raise ValueError("Local project is outside WORKSPACE_IMPORT_ROOTS")
        if self._is_relative_to(candidate, self.root):
            raise ValueError("Cannot import a managed workspace as a new project")
        return candidate

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in {"node_modules", "dist", "build", "__pycache__", ".pytest_cache"}}
        ignored.update(name for name in names if name == ".env" or name.startswith(".env."))
        return ignored

    @staticmethod
    def _git_clone(source: str, target: Path, branch: str, timeout: int) -> None:
        completed = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--branch", branch, "--single-branch", source, str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "Git clone failed")

    @staticmethod
    def _initialize_git(target: Path) -> None:
        try:
            subprocess.run(["git", "init", "-b", "main"], cwd=target, capture_output=True, timeout=15, shell=False)
            exclude = target / ".git" / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(".commchat-trash/\n__pycache__/\n.pytest_cache/\nnode_modules/\ndist/\n", encoding="utf-8")
            subprocess.run(["git", "config", "user.name", "CommChatBot Agent"], cwd=target, capture_output=True, timeout=10, shell=False)
            subprocess.run(["git", "config", "user.email", "agent@commchatbot.local"], cwd=target, capture_output=True, timeout=10, shell=False)
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "Initialize conversation workspace"],
                cwd=target,
                capture_output=True,
                timeout=15,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    @staticmethod
    def _write_project_identity(target: Path, identity: str) -> None:
        """Keep project identity in Git metadata so it does not enter the user's diff."""
        metadata = target / ".git" / "commchat-project.json"
        if not metadata.parent.is_dir():
            return
        metadata.write_text(json.dumps({"project_id": identity}, ensure_ascii=False), encoding="utf-8")
