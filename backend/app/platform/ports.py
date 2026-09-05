from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol


class WorkspaceHandle(Protocol):
    root: Path

    def project_identity(self) -> str: ...


class ConversationAccessPort(Protocol):
    """Application-owned conversation operations needed by platform jobs."""

    def history(self, db: Any, conversation_id: int) -> list[dict]: ...
    def ensure_workspace(self, db: Any, conversation: Any) -> WorkspaceHandle: ...


class ProductGatewayPort(Protocol):
    def stream(self, **kwargs: Any) -> AsyncIterator[dict]: ...


class MemoryJobQueuePort(Protocol):
    def enqueue(self, **kwargs: Any) -> str | None: ...
