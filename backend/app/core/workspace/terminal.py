from __future__ import annotations

import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path


class WorkspaceTerminalSession:
    """A persistent PowerShell process rooted in one conversation workspace."""

    def __init__(self, workspace_id: str, root: Path, title: str) -> None:
        self.id = uuid.uuid4().hex
        self.workspace_id = workspace_id
        self.root = root.resolve()
        self.title = title
        self.cwd = "."
        self.output = "PowerShell · conversation workspace\n"
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._process = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "-"],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._read_output, daemon=True).start()
        self._write("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n")

    def payload(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "output": self.output[-50000:],
            "running": self._process.poll() is None,
        }

    def execute(self, command: str, timeout: int = 60) -> dict:
        command = command.strip()
        if not command:
            return self.payload()
        if "\r" in command or "\n" in command:
            raise ValueError("Run one command line at a time")
        marker = f"__COMMCHAT_DONE_{uuid.uuid4().hex}__"
        with self._lock:
            if self._process.poll() is not None:
                raise ValueError("Terminal process has exited")
            self.output += f"\nPS {self.cwd}> {command}\n"
            self._write(command + "\n")
            self._write(
                f"Write-Output ('{marker}:' + $LASTEXITCODE + ':' + (Get-Location).Path)\n"
            )
            deadline = time.monotonic() + max(1, min(timeout, 120))
            chunks: list[str] = []
            while time.monotonic() < deadline:
                try:
                    line = self._queue.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    if self._process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                if marker in line:
                    prefix, marker_data = line.split(marker, 1)
                    if prefix:
                        chunks.append(prefix)
                        self.output += prefix
                    self._update_cwd(marker_data)
                    rendered = "".join(chunks)
                    return {**self.payload(), "command_output": rendered}
                chunks.append(line)
                self.output += line
            rendered = "".join(chunks)
            if self._process.poll() is not None:
                self.output += "\n[terminal] process exited.\n"
                return {**self.payload(), "command_output": rendered, "interrupted": True}
            self.output += "\n[terminal] command timed out; the process may still be running.\n"
            return {**self.payload(), "command_output": rendered, "timed_out": True}

    def close(self) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def _write(self, value: str) -> None:
        if self._process.stdin is None:
            raise ValueError("Terminal stdin is unavailable")
        self._process.stdin.write(value)
        self._process.stdin.flush()

    def _read_output(self) -> None:
        if self._process.stdout is None:
            self._queue.put(None)
            return
        for line in self._process.stdout:
            self._queue.put(line)
        self._queue.put(None)

    def _update_cwd(self, marker_data: str) -> None:
        parts = marker_data.strip().split(":", 2)
        if len(parts) < 3:
            return
        candidate = Path(parts[2]).resolve()
        try:
            relative = candidate.relative_to(self.root)
            self.cwd = relative.as_posix() or "."
        except ValueError:
            self.cwd = "."
            self.output += "[terminal] Directory left the workspace; returning to workspace root.\n"
            escaped = str(self.root).replace("'", "''")
            self._write(f"Set-Location -LiteralPath '{escaped}'\n")


class WorkspaceTerminalManager:
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], WorkspaceTerminalSession] = {}
        self._lock = threading.Lock()

    def list(self, workspace_id: str) -> list[dict]:
        return [session.payload() for (owner, _), session in self._sessions.items() if owner == workspace_id]

    def create(self, workspace_id: str, root: Path) -> dict:
        with self._lock:
            number = 1 + sum(owner == workspace_id for owner, _ in self._sessions)
            session = WorkspaceTerminalSession(workspace_id, root, f"Terminal {number}")
            self._sessions[(workspace_id, session.id)] = session
        return session.payload()

    def execute(self, workspace_id: str, terminal_id: str, command: str) -> dict:
        return self._require(workspace_id, terminal_id).execute(command)

    def payload(self, workspace_id: str, terminal_id: str) -> dict:
        return self._require(workspace_id, terminal_id).payload()

    def interrupt(self, workspace_id: str, terminal_id: str) -> dict:
        session = self._require(workspace_id, terminal_id)
        session.close()
        return session.payload()

    def close(self, workspace_id: str, terminal_id: str) -> None:
        with self._lock:
            session = self._sessions.pop((workspace_id, terminal_id), None)
        if not session:
            raise ValueError("Terminal not found")
        session.close()

    def close_workspace(self, workspace_id: str) -> None:
        ids = [terminal_id for owner, terminal_id in self._sessions if owner == workspace_id]
        for terminal_id in ids:
            self.close(workspace_id, terminal_id)

    def close_all(self) -> None:
        for workspace_id, terminal_id in list(self._sessions):
            self.close(workspace_id, terminal_id)

    def _require(self, workspace_id: str, terminal_id: str) -> WorkspaceTerminalSession:
        session = self._sessions.get((workspace_id, terminal_id))
        if not session:
            raise ValueError("Terminal not found")
        return session


terminal_manager = WorkspaceTerminalManager()
