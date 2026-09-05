from __future__ import annotations

import ast
from pathlib import Path

from app.bootstrap import get_container
from app.products.profiles import CHAT_PROFILE, CODING_PROFILE


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _module_parts(path: Path) -> tuple[list[str], list[str]]:
    relative = path.relative_to(APP_ROOT).with_suffix("")
    parts = ["app", *relative.parts]
    package = parts[:-1]
    if parts[-1] == "__init__":
        parts = parts[:-1]
        package = parts
    return parts, package


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    _, package = _module_parts(path)
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(item.name for item in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            base = []
        else:
            trim = node.level - 1
            base = package[:-trim] if trim else package
        module = [item for item in (node.module or "").split(".") if item]
        result.add(".".join([*base, *module]))
    return result


def _layer_violations(layer: str, forbidden: tuple[str, ...]) -> list[str]:
    violations = []
    for path in (APP_ROOT / layer).rglob("*.py"):
        for imported in _imports(path):
            if imported.startswith(forbidden):
                violations.append(f"{path.relative_to(APP_ROOT)} -> {imported}")
    return sorted(violations)


def test_legacy_junk_drawer_directories_have_been_removed() -> None:
    assert not (APP_ROOT / "core").exists()
    assert not (APP_ROOT / "packages").exists()
    assert not (APP_ROOT / "extensions" / "capabilities").exists()


def test_agent_runtime_is_recursively_independent_of_application_layers() -> None:
    assert _layer_violations("agent_runtime", (
        "app.platform", "app.extensions", "app.products", "app.api", "app.infra", "app.bootstrap",
    )) == []


def test_providers_do_not_read_application_configuration() -> None:
    assert _layer_violations("providers", (
        "app.infra", "app.platform", "app.products", "app.extensions", "app.api", "app.bootstrap",
    )) == []


def test_platform_never_depends_on_api_products_extensions_or_bootstrap() -> None:
    assert _layer_violations("platform", (
        "app.api", "app.products", "app.extensions", "app.bootstrap",
    )) == []


def test_products_depend_only_on_ports_profiles_and_runtime_contracts() -> None:
    assert _layer_violations("products", (
        "app.api", "app.bootstrap", "app.extensions", "app.infra", "app.platform",
    )) == []


def test_profiles_select_capabilities_without_cross_mode_leaks() -> None:
    registry = get_container().extension_registry
    chat = {item.manifest.id for item in registry.resolve(CHAT_PROFILE, "chatbot")}
    coding = {item.manifest.id for item in registry.resolve(CODING_PROFILE, "coding")}
    assert chat == {"chat.rag", "chat.web-search", "chat.memory"}
    assert {"coding.workspace", "coding.diagnostics", "coding.git", "coding.terminal"} <= coding
    assert chat.isdisjoint(coding)
