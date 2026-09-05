from __future__ import annotations

import asyncio
import json
import time
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.code import CodeExecutor
from app.core.rag import LocalRagStore
from app.core.workspace import ConversationWorkspaceManager, WorkspaceEditor
from app.core.workspace.terminal import (
    WorkspaceTerminalManager,
    WorkspaceTerminalSession,
    cleanup_orphaned_terminal_containers,
)
from app.core.workspace.terminal import MAX_OUTPUT_CHARS
from app.infra.config import get_settings
from app.core.runtime_state import RuntimeTaskManager
from app.products.tool_registry import ProductToolRegistry


def test_chat_and_coding_tools_are_isolated(tmp_path):
    registry = ProductToolRegistry(
        LocalRagStore(str(tmp_path / "index.json")),
        CodeExecutor(timeout=2),
        WorkspaceEditor(tmp_path),
    )

    chat_names = {tool.name for tool in registry.chatbot_tools(use_rag=True, use_web=True)}
    coding_names = {tool.name for tool in registry.coding_agent_tools()}

    assert chat_names == {"search_knowledge", "search_web"}
    assert {"search_knowledge", "search_web"}.isdisjoint(coding_names)
    assert {"list_files", "read_file", "search_files", "write_file", "replace_in_file", "run_command"} <= coding_names
    assert {"apply_patch", "get_diagnostics", "review_changes"} <= coding_names


def test_workspace_editor_can_create_read_search_and_edit(tmp_path):
    editor = WorkspaceEditor(tmp_path)

    editor.write_file("src/example.py", "value = 1\nprint(value)\n")
    assert "src/example.py" in editor.list_files(pattern="*.py")
    assert "1: value = 1" in editor.read_file("src/example.py")
    assert "src/example.py:2" in editor.search_files("print", pattern="*.py")

    editor.replace_in_file("src/example.py", "value = 1", "value = 2")
    assert "value = 2" in editor.read_file("src/example.py")


def test_workspace_editor_applies_unified_diff_and_rejects_escape(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    editor.write_file("main.py", "value = 1\n")
    result = json.loads(editor.apply_patch(
        "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    ))

    assert result["changed_files"] == ["main.py"]
    assert editor.read_text("main.py") == "value = 2\n"
    with pytest.raises(ValueError, match="escapes"):
        editor.apply_patch(
            "--- a/main.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-value = 2\n+escaped = True\n"
        )


def test_write_tool_returns_automatic_static_feedback(tmp_path):
    registry = ProductToolRegistry(
        LocalRagStore(str(tmp_path / "index.json")),
        CodeExecutor(timeout=2),
        WorkspaceEditor(tmp_path),
    )
    tool = next(item for item in registry.coding_agent_tools() if item.name == "write_file")

    result = tool.handler("broken.py", "def broken(:\n", False)

    payload = json.loads(result)
    assert payload["diagnostic_count"] > 0
    assert "SyntaxError" in result or "invalid-syntax" in result
    assert "broken.py" in payload["diff"]


def test_workspace_editor_rejects_escape_and_sensitive_files(tmp_path):
    editor = WorkspaceEditor(tmp_path)

    with pytest.raises(ValueError, match="escapes"):
        editor.write_file("../outside.txt", "no")
    with pytest.raises(ValueError, match="excluded"):
        editor.write_file(".env", "SECRET=no")


def test_workspace_editor_rejects_unapproved_commands(tmp_path):
    editor = WorkspaceEditor(tmp_path)

    with pytest.raises(ValueError, match="not allowed"):
        asyncio.run(editor.run_command(["powershell", "-Command", "Get-ChildItem"]))


def test_python_command_runs_against_workspace(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    editor.write_file("hello_world.py", "print('hello world')\n")

    result = json.loads(asyncio.run(editor.run_command(["python", "hello_world.py"])))

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hello world"


def test_execute_python_tool_mounts_selected_workspace(tmp_path):
    editor = WorkspaceEditor(tmp_path)
    editor.write_file("marker.txt", "workspace-visible\n")
    registry = ProductToolRegistry(
        LocalRagStore(str(tmp_path / "index.json")),
        CodeExecutor(timeout=2),
        WorkspaceEditor(tmp_path / "fallback"),
    )
    tool = next(item for item in registry.coding_agent_tools(editor) if item.name == "execute_python")

    result = json.loads(asyncio.run(tool.handler("print(open('marker.txt', encoding='utf-8').read().strip())")))

    assert result["exit_code"] == 0
    assert result["stdout"] == "workspace-visible"
    assert not list(tmp_path.glob(".commchat-exec-*.py"))


def test_conversation_workspaces_are_isolated_and_tools_use_selected_workspace(tmp_path):
    manager = ConversationWorkspaceManager(tmp_path / "workspaces")
    first_id = manager.create()
    second_id = manager.create()
    first = manager.editor(first_id)
    second = manager.editor(second_id)
    first.write_file("main.py", "print('first')\n")
    first.create_directory("empty-folder")
    first.create_directory("target-folder")

    assert first_id != second_id
    assert "main.py" in first.list_files()
    assert {"path": "empty-folder", "type": "directory"} in first.list_entries()
    assert second.list_files() == "No files found."

    run_result = asyncio.run(first.run_python_file("main.py"))
    assert run_result["exit_code"] == 0
    assert run_result["stdout"].strip() == "first"

    first.delete_path("empty-folder")
    assert all(entry["path"] != "empty-folder" for entry in first.list_entries())

    first.move_path("main.py", "target-folder/main.py")
    assert "target-folder/main.py" in first.list_files()

    registry = ProductToolRegistry(
        LocalRagStore(str(tmp_path / "index.json")),
        CodeExecutor(timeout=2),
        WorkspaceEditor(tmp_path / "fallback"),
    )
    list_tool = next(tool for tool in registry.coding_agent_tools(first) if tool.name == "list_files")
    assert list_tool.handler() == "target-folder/main.py"


def test_workspace_terminal_preserves_process_and_working_directory(tmp_path):
    manager = WorkspaceTerminalManager()
    terminal = manager.create("workspace-test", tmp_path)
    docker = terminal["backend"] == "docker"
    try:
        result = manager.execute(
            "workspace-test", terminal["id"],
            "printf 'terminal-ok\\n'" if docker else "Write-Output 'terminal-ok'",
        )
        assert "terminal-ok" in result["command_output"]
        assert result["exit_code"] == 0
        assert result["timed_out"] is False

        failed = manager.execute(
            "workspace-test", terminal["id"],
            "false" if docker else "cmd /c exit 7",
        )
        assert failed["exit_code"] != 0
        assert failed["timed_out"] is False

        result = manager.execute(
            "workspace-test",
            terminal["id"],
            "mkdir -p nested; cd nested" if docker else "New-Item -ItemType Directory -Path nested | Out-Null; Set-Location nested",
        )
        assert result["cwd"] == "nested"

        resized = manager.resize("workspace-test", terminal["id"], 132, 41)
        assert resized["backend"] in {"conpty", "pty", "docker"}
        assert (resized["cols"], resized["rows"]) == (132, 41)

        manager.write("workspace-test", terminal["id"], "sleep 20\r" if docker else "Start-Sleep -Seconds 20\r")
        # Wait until PowerShell has entered the foreground pipeline before
        # sending the control event; sending it while the command is still in
        # the ConPTY input queue would correctly cancel the empty prompt.
        time.sleep(1.0)
        interrupted = manager.interrupt("workspace-test", terminal["id"])
        assert interrupted["running"] is True
        result = manager.execute(
            "workspace-test", terminal["id"],
            "printf 'after-ctrl-c\\n'" if docker else "Write-Output 'after-ctrl-c'",
            timeout=8,
        )
        assert "after-ctrl-c" in result["command_output"]
        assert result["exit_code"] == 0

        # Ctrl+Z is exposed as an EOF action. It must leave a Python REPL and
        # keep the surrounding shell available for the next command on both
        # Docker/Unix PTYs and Windows ConPTY.
        manager.write("workspace-test", terminal["id"], "python -q\r")
        time.sleep(0.5)
        manager.end_of_input("workspace-test", terminal["id"])
        result = manager.execute(
            "workspace-test", terminal["id"],
            "printf 'after-ctrl-z\\n'" if docker else "Write-Output 'after-ctrl-z'",
            timeout=8,
        )
        assert "after-ctrl-z" in result["command_output"]
        assert result["exit_code"] == 0

        cursor = result["cursor"]
        manager.write(
            "workspace-test", terminal["id"],
            "printf 'cursor-ok\\n'\r" if docker else "Write-Output 'cursor-ok'\r",
        )
        deadline = time.monotonic() + 3
        chunks = []
        next_cursor = cursor
        alive = True
        while "cursor-ok" not in "".join(chunks) and time.monotonic() < deadline:
            chunk, next_cursor, alive = manager.read_since(
                "workspace-test", terminal["id"], next_cursor, wait_seconds=0.25,
            )
            chunks.append(chunk)
        assert "cursor-ok" in "".join(chunks)
        assert next_cursor > cursor
        assert alive is True

        session = manager._require("workspace-test", terminal["id"])
        before_restart = session.payload()["restart_count"]
        session._record_restart("ctrl_c")
        restarted = session.payload()
        assert restarted["restarted"] is True
        assert restarted["restart_count"] == before_restart + 1
        assert restarted["restart_reason"] == "ctrl_c"
        assert restarted["cwd"] == "."

        session._append("x" * (MAX_OUTPUT_CHARS + 17))
        chunk, truncated_cursor, _ = session.read_since(0)
        assert len(chunk) == MAX_OUTPUT_CHARS
        assert truncated_cursor == session.end_offset

        second = manager.create("workspace-test", tmp_path)
        assert len(manager.list("workspace-test")) == 2
        manager.close("workspace-test", second["id"])
        with pytest.raises(ValueError, match="Terminal not found"):
            manager.payload("workspace-test", second["id"])

        timed_out = manager.execute(
            "workspace-test", terminal["id"],
            "sleep 5" if docker else "Start-Sleep -Seconds 5",
            timeout=1,
        )
        assert timed_out["timed_out"] is True
        assert timed_out["exit_code"] == -1

        if get_settings().sandbox_mode.casefold() == "docker":
            assert terminal["backend"] == "docker"
    finally:
        manager.close_all()

    with pytest.raises(ValueError, match="Terminal not found"):
        manager.execute("workspace-test", terminal["id"], "echo missing")


def test_orphan_terminal_cleanup_only_removes_owned_names(monkeypatch):
    removed = []

    monkeypatch.setattr(
        "app.core.workspace.terminal.get_settings",
        lambda: type("Settings", (), {"sandbox_mode": "docker"})(),
    )
    monkeypatch.setattr("app.core.workspace.terminal.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(
        "app.core.workspace.terminal.subprocess.run",
        lambda *_args, **_kwargs: type("Result", (), {
            "stdout": "commchat-terminal-0123456789abcdef\nunrelated-container\ncommchat-terminal-nothex\n",
        })(),
    )
    monkeypatch.setattr("app.core.workspace.terminal.remove_docker_container", removed.append)

    assert cleanup_orphaned_terminal_containers() == ["commchat-terminal-0123456789abcdef"]
    assert removed == ["commchat-terminal-0123456789abcdef"]


def test_terminal_ctrl_z_uses_platform_eof_sequence():
    session = object.__new__(WorkspaceTerminalSession)
    written = []
    session.write = written.append
    session.payload = lambda: {"running": True}

    session.backend = "docker"
    session.end_of_input()
    session.backend = "pty"
    session.end_of_input()
    session.backend = "conpty"
    session.end_of_input()

    assert written == ["\x04", "\x04", "\x1a\r"]


def test_workspace_git_diff_checkpoint_and_recoverable_delete(tmp_path):
    manager = ConversationWorkspaceManager(tmp_path / "workspaces")
    editor = manager.editor(manager.create())
    editor.write_file("main.py", "print('one')\n")
    assert any(item["path"] == "main.py" for item in editor.git_status()["changes"])
    assert "+print('one')" in editor.git_diff("main.py")
    editor.git_checkpoint("first")
    editor.replace_in_file("main.py", "one", "two")
    assert "print('two')" in editor.git_diff("main.py")

    payload = editor.delete_path("main.py")
    assert "workspace trash" in payload
    trash = editor.list_trash()
    assert trash and trash[0]["path"] == "main.py"
    editor.restore_trash(trash[0]["id"])
    assert "print('two')" in editor.read_text("main.py")


def test_permission_modes_classify_tool_capabilities():
    assert RuntimeTaskManager.permission_action("read-only", "read") == "allow"
    assert RuntimeTaskManager.permission_action("read-only", "write") == "deny"
    assert RuntimeTaskManager.permission_action("workspace-write", "write") == "allow"
    assert RuntimeTaskManager.permission_action("workspace-write", "execute") == "approve"
    assert RuntimeTaskManager.permission_action("full-access", "network") == "allow"
