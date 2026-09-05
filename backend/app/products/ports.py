from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from ..agent_runtime import Tool


class RetrievedDocument(Protocol):
    source: str
    title: str
    chunk_id: str
    score: float
    content: str


class RetrievalPort(Protocol):
    async def search(self, query: str, top_k: int = 5) -> list[RetrievedDocument]: ...


class WorkspacePort(Protocol):
    root: Path

    def project_identity(self) -> str: ...


class WorkspaceFactory(Protocol):
    def __call__(self, root: str | None = None) -> WorkspacePort: ...


class ToolCatalogPort(Protocol):
    workspace: WorkspacePort

    def workspace_tools(self, workspace: WorkspacePort | None = None) -> list[Tool]: ...
    def diagnostics_tools(self, workspace: WorkspacePort | None = None) -> list[Tool]: ...
    def git_tools(self, workspace: WorkspacePort | None = None) -> list[Tool]: ...
    def terminal_tools(self, workspace: WorkspacePort | None = None) -> list[Tool]: ...
    async def search_web(self, query: str) -> str: ...


class ProjectContextPort(Protocol):
    def extension_overrides(self, workspace: WorkspacePort, *, trusted: bool) -> list[str]: ...


ExtensionServices = dict[str, Any]
