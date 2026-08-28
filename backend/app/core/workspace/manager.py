from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from ...infra.config import get_settings
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
        return workspace_id

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
