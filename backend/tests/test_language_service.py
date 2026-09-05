from __future__ import annotations

import pytest

from app.extensions.builtin.workspace.language import WorkspaceLanguageService


def test_python_language_service_completion_diagnostics_format_and_definition(tmp_path) -> None:
    service = WorkspaceLanguageService(tmp_path)
    completions = service.complete("main.py", "import os\nos.pa", 2, 6)
    assert any(item["label"] == "path" for item in completions)

    diagnostics = service.diagnostics("main.py", "print(missing_name)\n")
    assert any(item["code"] == "F821" for item in diagnostics)

    formatted = service.format("main.py", "value={\"a\":1,\"b\":2}\n")
    assert 'value = {"a": 1, "b": 2}' in formatted

    (tmp_path / "utils.py").write_text("def greet(name):\n    return f'Hi {name}'\n", encoding="utf-8")
    source = "from utils import greet\n\ngreet('Codex')\n"
    definitions = service.definition("main.py", source, 3, 3)
    assert definitions and definitions[0]["path"] == "utils.py"
    assert definitions[0]["line"] == 1


def test_language_service_rejects_paths_outside_workspace(tmp_path) -> None:
    service = WorkspaceLanguageService(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        service.diagnostics("../outside.py", "print('no')")


def test_persistent_lsp_hover_references_and_rename(tmp_path) -> None:
    service = WorkspaceLanguageService(tmp_path)
    source = (
        "def greet(name: str) -> str:\n"
        "    return f'Hi {name}'\n\n"
        "message = greet('Codex')\n"
    )
    (tmp_path / "main.py").write_text(source, encoding="utf-8")

    hover = service.hover("main.py", source, 4, 12)
    assert hover and "greet" in str(hover.get("contents", hover))

    references = service.references("main.py", source, 1, 6)
    assert len(references) >= 2
    assert all(item["path"] == "main.py" for item in references)

    edits = service.rename("main.py", source, 1, 6, "welcome")
    assert edits and edits[0]["path"] == "main.py"
    assert len(edits[0]["edits"]) == 2
    assert {item["newText"] for item in edits[0]["edits"]} == {"welcome"}
