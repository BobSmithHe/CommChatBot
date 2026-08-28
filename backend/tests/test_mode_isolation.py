from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.code import CodeExecutor
from app.core.rag import LocalRagStore
from app.core.workspace import ConversationWorkspaceManager, WorkspaceEditor
from app.core.workspace.terminal import WorkspaceTerminalManager
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


def test_workspace_editor_can_create_read_search_and_edit(tmp_path):
    editor = WorkspaceEditor(tmp_path)

    editor.write_file("src/example.py", "value = 1\nprint(value)\n")
    assert "src/example.py" in editor.list_files(pattern="*.py")
    assert "1: value = 1" in editor.read_file("src/example.py")
    assert "src/example.py:2" in editor.search_files("print", pattern="*.py")

    editor.replace_in_file("src/example.py", "value = 1", "value = 2")
    assert "value = 2" in editor.read_file("src/example.py")


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
    try:
        result = manager.execute("workspace-test", terminal["id"], "Write-Output 'terminal-ok'")
        assert "terminal-ok" in result["command_output"]

        result = manager.execute(
            "workspace-test",
            terminal["id"],
            "New-Item -ItemType Directory -Path nested | Out-Null; Set-Location nested",
        )
        assert result["cwd"] == "nested"
    finally:
        manager.close_all()


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
