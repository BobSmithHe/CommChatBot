from __future__ import annotations

import os
import base64
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from ..sandbox import (
    ProcessLimitHandle,
    docker_container_name,
    docker_run_argv,
    remove_docker_container,
    sandbox_environment,
)
from ...infra.config import get_settings


MAX_OUTPUT_CHARS = 200_000
ANSI_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|.)")
TERMINAL_CONTAINER_PATTERN = re.compile(r"commchat-terminal-[a-f0-9]{16}")


def cleanup_orphaned_terminal_containers() -> list[str]:
    """Remove terminal containers left by a previous crashed API process."""
    if get_settings().sandbox_mode.casefold() != "docker":
        return []
    docker = shutil.which("docker")
    if not docker:
        return []
    try:
        listed = subprocess.run(
            [docker, "ps", "-a", "--filter", "name=commchat-terminal-", "--format", "{{.Names}}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = [
        name.strip()
        for name in listed.stdout.splitlines()
        if TERMINAL_CONTAINER_PATTERN.fullmatch(name.strip())
    ]
    for name in names:
        remove_docker_container(name)
    return names


class WorkspaceTerminalSession:
    """Ephemeral interactive PTY for one durable workspace.

    Workspace files and Agent checkpoints survive service restarts; this shell,
    its environment, process tree, and current directory do not. Interactive
    programs use write/read_since/interrupt/resize. ``execute`` is only a
    compatibility API for bounded, one-shot shell commands.
    """

    def __init__(self, workspace_id: str, root: Path, title: str, cols: int = 100, rows: int = 28) -> None:
        self.id = uuid.uuid4().hex
        self.workspace_id = workspace_id
        self.root = root.resolve()
        self.title = title
        self.cwd = "."
        self.cols = max(20, min(cols, 500))
        self.rows = max(5, min(rows, 200))
        self._output = ""
        self._base_offset = 0
        self._closed = False
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._command_lock = threading.Lock()
        self._pty = None
        self._process = None
        self._bridge: subprocess.Popen | None = None
        self._limits: ProcessLimitHandle | None = None
        self._container_name: str | None = None
        self._restart_after_interrupt_until = 0.0
        self._restart_count = 0
        self._last_restart_reason: str | None = None
        self.backend = "docker" if get_settings().sandbox_mode.casefold() == "docker" else ("conpty" if os.name == "nt" else "pty")
        self._spawn()
        threading.Thread(target=self._read_output, daemon=True, name=f"terminal-{self.id[:8]}").start()

    def _spawn(self) -> None:
        if os.name == "nt":
            shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
            shell_args = ["-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass"]
            if self.backend == "docker":
                self._container_name = docker_container_name("commchat-terminal")
                docker_args = docker_run_argv(
                    workspace=self.root,
                    name=self._container_name,
                    command=["/bin/bash", "--noprofile", "--norc"],
                    env={"TERM": "xterm-256color"},
                    interactive=True,
                )
                shell, shell_args = docker_args[0], docker_args[1:]
            node = shutil.which("node.exe") or shutil.which("node")
            node_pty = Path(__file__).resolve().parents[4] / "frontend" / "node_modules" / "node-pty"
            if not node or not node_pty.is_dir():
                raise RuntimeError("node-pty is required; run npm install in frontend")
            host = Path(__file__).with_name("pty_host.cjs")
            encoded_args = base64.urlsafe_b64encode(json.dumps(shell_args).encode("utf-8")).decode("ascii")
            self._bridge = subprocess.Popen(
                [node, str(host), str(self.root), str(self.cols), str(self.rows), shell, str(node_pty), encoded_args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=sandbox_environment(),
            )
            self._limits = ProcessLimitHandle(self._bridge)
            return

        # Unix uses the same hardened Docker sandbox when configured; host PTY
        # remains available for explicitly selected non-Docker deployments.
        from ptyprocess import PtyProcessUnicode

        if self.backend == "docker":
            self._container_name = docker_container_name("commchat-terminal")
            command = docker_run_argv(
                workspace=self.root,
                name=self._container_name,
                command=["/bin/bash", "--noprofile", "--norc"],
                env={"TERM": "xterm-256color"},
                interactive=True,
            )
            self._process = PtyProcessUnicode.spawn(
                command,
                cwd=str(self.root),
                dimensions=(self.rows, self.cols),
            )
            return

        shell = os.environ.get("SHELL") or "/bin/bash"
        self._process = PtyProcessUnicode.spawn([shell, "-l"], cwd=str(self.root), dimensions=(self.rows, self.cols))

    @property
    def alive(self) -> bool:
        if self._closed:
            return False
        if self._bridge is not None:
            return self._bridge.poll() is None
        if self._pty is not None:
            try:
                return bool(self._pty.isalive())
            except Exception:
                return False
        return bool(self._process and self._process.isalive())

    @property
    def end_offset(self) -> int:
        with self._condition:
            return self._base_offset + len(self._output)

    def payload(self) -> dict:
        with self._condition:
            return {
                "id": self.id,
                "title": self.title,
                "cwd": self.cwd,
                "output": self._output[-50_000:],
                "output_offset": self._base_offset + max(0, len(self._output) - 50_000),
                "cursor": self._base_offset + len(self._output),
                "running": self.alive,
                "backend": self.backend,
                "cols": self.cols,
                "rows": self.rows,
                "restarted": self._restart_count > 0,
                "restart_count": self._restart_count,
                "restart_reason": self._last_restart_reason,
            }

    def write(self, data: str) -> None:
        if not data or not self.alive:
            if not self.alive:
                raise ValueError("Terminal process has exited")
            return
        with self._write_lock:
            if "\x03" in data:
                # Docker Desktop occasionally lets Ctrl+C terminate the
                # attached client as well as the foreground process. Keep a
                # short recovery window so the terminal tab can respawn its
                # shell instead of becoming unusable.
                self._restart_after_interrupt_until = time.monotonic() + 2.5
            if self._bridge is not None:
                self._send_bridge({
                    "type": "input",
                    "data": base64.b64encode(data.encode("utf-8", errors="replace")).decode("ascii"),
                })
            elif self._pty is not None:
                self._pty.write(data)
            elif self._process is not None:
                self._process.write(data)

    def resize(self, cols: int, rows: int) -> dict:
        self.cols = max(20, min(int(cols), 500))
        self.rows = max(5, min(int(rows), 200))
        if self._bridge is not None:
            self._send_bridge({"type": "resize", "cols": self.cols, "rows": self.rows})
        elif self._pty is not None:
            self._pty.set_size(self.cols, self.rows)
        elif self._process is not None:
            self._process.setwinsize(self.rows, self.cols)
        return self.payload()

    def interrupt(self) -> dict:
        """Send ETX like a physical Ctrl+C without destroying the shell."""
        cursor = self.end_offset
        self.write("\x03")
        # ConPTY delivers ETX asynchronously. Wait until the foreground
        # program has yielded control before accepting a toolbar/API command;
        # otherwise the first bytes typed after Ctrl+C can be discarded while
        # PowerShell is still rebuilding its prompt. Raw WebSocket clients do
        # not pay this synchronization cost because they write ETX directly.
        deadline = time.monotonic() + 1.5
        observed = ""
        while time.monotonic() < deadline and self.alive:
            chunk, cursor, _ = self.read_since(cursor, min(0.1, deadline - time.monotonic()))
            observed += ANSI_PATTERN.sub("", chunk)
            if re.search(r"(?:^|\r?\n)(?:PS [^\r\n]*>|[^\r\n]*[$#]) $", observed):
                break
        return self.payload()

    def end_of_input(self) -> dict:
        """Send the platform-correct interactive EOF sequence.

        The UI exposes this as Ctrl+Z for Windows muscle memory. Docker and
        Unix PTYs use EOT (Ctrl+D); Windows ConPTY programs expect Ctrl+Z
        followed by Enter. This only affects the terminal foreground program
        and is unrelated to Agent task cancellation.
        """
        self.write("\x1a\r" if self.backend == "conpty" else "\x04")
        return self.payload()

    def read_since(self, cursor: int, wait_seconds: float = 0.0) -> tuple[str, int, bool]:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        with self._condition:
            while cursor >= self._base_offset + len(self._output) and self.alive and wait_seconds > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            cursor = max(cursor, self._base_offset)
            start = cursor - self._base_offset
            data = self._output[start:]
            return data, self._base_offset + len(self._output), self.alive

    def execute(self, command: str, timeout: int = 60) -> dict:
        """Run one bounded shell command and report exit status.

        This compatibility path is intended for commands such as pytest,
        git status, python test.py, or ls. REPLs, editors, pagers, top, and SSH
        sessions must use the interactive write/read_since/interrupt/resize API.
        """
        command = command.strip()
        if not command:
            return {**self.payload(), "command_output": "", "exit_code": 0, "timed_out": False}
        if "\r" in command or "\n" in command:
            raise ValueError("Run one command line at a time")
        marker = f"__COMMCHAT_DONE_{uuid.uuid4().hex}__"
        midpoint = len(marker) // 2
        marker_expression = f"$m='{marker[:midpoint]}'+'{marker[midpoint:]}'"
        start = self.end_offset
        with self._command_lock:
            self.write(
                (
                    f"{command}; $commchatOk=$?; $commchatNative=$LASTEXITCODE; "
                    "$commchatExit=if($commchatOk){0}elseif($null -ne $commchatNative "
                    "-and [int]$commchatNative -ne 0){[int]$commchatNative}else{1}; "
                    f"{marker_expression}; Write-Output ($m + ':' + $commchatExit + ':' + (Get-Location).Path)\r"
                )
                if os.name == "nt" and self.backend != "docker"
                else (
                    f"{command}; commchat_exit=$?; printf '\\n%s%s:%s:%s\\n' "
                    f"'{marker[:midpoint]}' '{marker[midpoint:]}' \"$commchat_exit\" \"$PWD\"\r"
                )
            )
            deadline = time.monotonic() + max(1, min(timeout, 120))
            cursor = start
            captured = ""
            while time.monotonic() < deadline:
                chunk, cursor, alive = self.read_since(cursor, min(0.25, deadline - time.monotonic()))
                captured += chunk
                clean = ANSI_PATTERN.sub("", captured)
                marker_index = clean.find(marker + ":")
                if marker_index >= 0:
                    after = clean[marker_index + len(marker) + 1:]
                    marker_line = after.splitlines()[0].strip() if after else ""
                    exit_text, separator, location = marker_line.partition(":")
                    if not separator or not re.fullmatch(r"-?\d+", exit_text):
                        raise RuntimeError("Terminal completion marker is malformed")
                    exit_code = int(exit_text)
                    self._update_cwd(location)
                    return {
                        **self.payload(),
                        "command_output": clean[:marker_index],
                        "exit_code": exit_code,
                        "timed_out": False,
                    }
                if not alive:
                    return {
                        **self.payload(),
                        "command_output": clean,
                        "exit_code": -1,
                        "interrupted": True,
                        "timed_out": False,
                    }
            self.interrupt()
            return {
                **self.payload(),
                "command_output": ANSI_PATTERN.sub("", captured),
                "exit_code": -1,
                "timed_out": True,
            }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._bridge is not None:
                bridge = self._bridge
                self._send_bridge({"type": "close"})
                try:
                    bridge.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    bridge.terminate()
                self._bridge = None
            elif self._pty is not None:
                self._pty.cancel_io()
                if self._pty.isalive():
                    self._pty.write("exit\r")
            elif self._process is not None and self._process.isalive():
                self._process.terminate(force=True)
        except Exception:
            pass
        if self._limits:
            self._limits.close()
            self._limits = None
        if self._container_name:
            remove_docker_container(self._container_name)
            self._container_name = None
        with self._condition:
            self._condition.notify_all()

    def _read_output(self) -> None:
        if self._bridge is not None:
            while not self._closed and self._bridge is not None:
                self._read_bridge_output()
                if self._closed:
                    return
                if time.monotonic() <= self._restart_after_interrupt_until:
                    self._restart_after_interrupt_until = 0.0
                    self._dispose_exited_bridge()
                    try:
                        self._spawn()
                    except Exception as exc:
                        self._append(f"\r\n[terminal restart failed: {type(exc).__name__}]\r\n")
                        break
                    self._record_restart("ctrl_c")
                    self._append("\r\n\x1b[90m[shell recovered after Ctrl+C]\x1b[0m\r\n")
                    continue
                break
            with self._condition:
                self._condition.notify_all()
            return
        while not self._closed:
            try:
                if self._pty is not None:
                    chunk = self._pty.read(blocking=True)
                elif self._process is not None:
                    chunk = self._process.read(4096)
                else:
                    break
            except Exception:
                # ConPTY can transiently cancel a blocking read while its
                # viewport is resized. Keep the reader alive in that case.
                if not self._closed and self.alive:
                    time.sleep(0.02)
                    continue
                break
            if chunk:
                self._append(str(chunk))
            elif not self.alive:
                break
            else:
                time.sleep(0.01)
        with self._condition:
            self._condition.notify_all()

    def _read_bridge_output(self) -> None:
        stream = self._bridge.stdout if self._bridge else None
        if stream is None:
            return
        for raw in stream:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("type") == "output":
                try:
                    chunk = base64.b64decode(message.get("data") or "").decode("utf-8", errors="replace")
                except (ValueError, TypeError):
                    continue
                self._append(chunk)
            elif message.get("type") == "error":
                self._append(f"\r\n[terminal host error: {message.get('message')}]\r\n")
        with self._condition:
            self._condition.notify_all()

    def _send_bridge(self, message: dict) -> None:
        if not self._bridge or self._bridge.poll() is not None or self._bridge.stdin is None:
            raise ValueError("Terminal host has exited")
        self._bridge.stdin.write(json.dumps(message, ensure_ascii=True) + "\n")
        self._bridge.stdin.flush()

    def _dispose_exited_bridge(self) -> None:
        bridge = self._bridge
        self._bridge = None
        if bridge and bridge.poll() is None:
            try:
                bridge.terminate()
            except OSError:
                pass
        if self._limits:
            self._limits.close()
            self._limits = None
        if self._container_name:
            remove_docker_container(self._container_name)
            self._container_name = None

    def _append(self, chunk: str) -> None:
        with self._condition:
            self._output += chunk
            if len(self._output) > MAX_OUTPUT_CHARS:
                remove = len(self._output) - MAX_OUTPUT_CHARS
                self._output = self._output[remove:]
                self._base_offset += remove
            self._condition.notify_all()

    def _record_restart(self, reason: str) -> None:
        with self._condition:
            self._restart_count += 1
            self._last_restart_reason = reason
            self.cwd = "."
            self._condition.notify_all()

    def _update_cwd(self, location: str) -> None:
        if not location:
            return
        if self.backend == "docker":
            if location == "/workspace":
                self.cwd = "."
            elif location.startswith("/workspace/"):
                self.cwd = location[len("/workspace/"):]
            else:
                self.cwd = "."
            return
        try:
            relative = Path(location).resolve().relative_to(self.root)
            self.cwd = relative.as_posix() or "."
        except (OSError, ValueError):
            self.cwd = "."


class WorkspaceTerminalManager:
    """Own in-memory terminal sessions; sessions are deliberately non-durable."""
    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], WorkspaceTerminalSession] = {}
        self._lock = threading.Lock()

    def list(self, workspace_id: str) -> list[dict]:
        return [session.payload() for (owner, _), session in self._sessions.items() if owner == workspace_id]

    def create(self, workspace_id: str, root: Path, cols: int = 100, rows: int = 28) -> dict:
        with self._lock:
            existing = sum(owner == workspace_id for owner, _ in self._sessions)
            if existing >= max(1, get_settings().terminal_max_per_workspace):
                raise ValueError("Terminal limit reached for this workspace")
            number = 1 + existing
            session = WorkspaceTerminalSession(workspace_id, root, f"Terminal {number}", cols, rows)
            self._sessions[(workspace_id, session.id)] = session
        return session.payload()

    def execute(self, workspace_id: str, terminal_id: str, command: str, timeout: int = 60) -> dict:
        return self._require(workspace_id, terminal_id).execute(command, timeout)

    def write(self, workspace_id: str, terminal_id: str, data: str) -> None:
        self._require(workspace_id, terminal_id).write(data)

    def resize(self, workspace_id: str, terminal_id: str, cols: int, rows: int) -> dict:
        return self._require(workspace_id, terminal_id).resize(cols, rows)

    def read_since(self, workspace_id: str, terminal_id: str, cursor: int, wait_seconds: float = 0.0):
        return self._require(workspace_id, terminal_id).read_since(cursor, wait_seconds)

    def payload(self, workspace_id: str, terminal_id: str) -> dict:
        return self._require(workspace_id, terminal_id).payload()

    def interrupt(self, workspace_id: str, terminal_id: str) -> dict:
        return self._require(workspace_id, terminal_id).interrupt()

    def end_of_input(self, workspace_id: str, terminal_id: str) -> dict:
        return self._require(workspace_id, terminal_id).end_of_input()

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
