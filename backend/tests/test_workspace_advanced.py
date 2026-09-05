from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.workspace.editor import WorkspaceEditor
from app.core.workspace.manager import ConversationWorkspaceManager
from app.core.workspace.memory import ProjectMemoryStore


def test_workspace_walk_prunes_dependencies_and_versions_detect_external_changes(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "node_modules" / "huge").mkdir(parents=True)
    (tmp_path / "node_modules" / "huge" / "ignored.js").write_text("ignored", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hidden", encoding="utf-8")
    editor = WorkspaceEditor(tmp_path)

    entries = editor.list_entries(limit=1000)
    paths = {item["path"] for item in entries}
    assert "src/main.py" in paths
    assert not any(path.startswith("node_modules") for path in paths)
    assert ".env" not in paths

    first = editor.file_version("src/main.py")
    (tmp_path / "src" / "main.py").write_text("value = 200\n", encoding="utf-8")
    assert editor.file_version("src/main.py") != first


def test_local_project_import_copies_source_but_excludes_secrets_and_dependencies(tmp_path, monkeypatch) -> None:
    source = tmp_path / "projects" / "sample"
    source.mkdir(parents=True)
    (source / "main.py").write_text("print('imported')\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=hidden", encoding="utf-8")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    managed = tmp_path / "managed"
    settings = SimpleNamespace(
        workspace_import_roots=str(tmp_path / "projects"),
        git_clone_timeout_seconds=30,
    )
    monkeypatch.setattr("app.core.workspace.manager.get_settings", lambda: settings)
    manager = ConversationWorkspaceManager(managed)

    workspace_id = manager.import_workspace(str(source), "local", "main")
    editor = manager.editor(workspace_id)
    assert "imported" in editor.read_text("main.py")
    assert not (editor.root / ".env").exists()
    assert not (editor.root / "node_modules").exists()


def test_project_memory_is_shared_across_conversations_for_same_import(tmp_path, monkeypatch) -> None:
    source = tmp_path / "projects" / "sample"
    source.mkdir(parents=True)
    (source / "main.py").write_text("print('shared')\n", encoding="utf-8")
    settings = SimpleNamespace(
        workspace_import_roots=str(tmp_path / "projects"),
        git_clone_timeout_seconds=30,
    )
    monkeypatch.setattr("app.core.workspace.manager.get_settings", lambda: settings)
    manager = ConversationWorkspaceManager(tmp_path / "managed")
    first = manager.editor(manager.import_workspace(str(source), "local", "main"))
    second = manager.editor(manager.import_workspace(str(source), "local", "main"))
    store = ProjectMemoryStore(tmp_path / "memory")

    assert first.project_identity() == second.project_identity()
    store.remember(first.project_identity(), "test command", "pytest -q")
    assert store.read(second.project_identity())["test command"] == "pytest -q"


def test_local_import_rejects_source_outside_allowlist(tmp_path, monkeypatch) -> None:
    source = tmp_path / "outside"
    source.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    settings = SimpleNamespace(workspace_import_roots=str(allowed), git_clone_timeout_seconds=30)
    monkeypatch.setattr("app.core.workspace.manager.get_settings", lambda: settings)
    manager = ConversationWorkspaceManager(tmp_path / "managed")
    with pytest.raises(ValueError, match="outside WORKSPACE_IMPORT_ROOTS"):
        manager.import_workspace(str(source), "local", "main")


def test_workspace_git_branch_create_and_switch(tmp_path) -> None:
    manager = ConversationWorkspaceManager(tmp_path / "managed")
    editor = manager.editor(manager.create())
    editor.write_file("main.py", "print('main')\n")
    editor.git_checkpoint("main file")
    editor.git_switch_branch("feature/editor", create=True)
    assert editor.git_branches()["current"] == "feature/editor"
    editor.git_switch_branch("main")
    assert editor.git_branches()["current"] == "main"
