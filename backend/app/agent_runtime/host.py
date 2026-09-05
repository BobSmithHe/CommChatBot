from __future__ import annotations

from typing import Any, Literal, Protocol


class TaskCancelled(RuntimeError):
    """Normal control-flow signal for a user-cancelled agent task."""


class AgentRuntimeHost(Protocol):
    """Platform boundary consumed by the portable AgentRuntime core.

    Implementations may persist checkpoints, approvals, usage and mailbox
    messages in a database. The runtime itself deliberately knows nothing
    about HTTP, Redis, SQLAlchemy or application configuration.
    """

    async def run_hooks(self, event: str, payload: dict[str, Any], cwd: str | None) -> list[dict]: ...
    def ensure_not_cancelled(self, task_id: str | None) -> None: ...
    def save_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None: ...
    def record_usage(self, task_id: str | None, provider: str, model: str, usage: dict[str, Any]) -> None: ...
    def permission_action(
        self, permission_mode: str, capability: str,
    ) -> Literal["allow", "approve", "deny"]: ...
    def create_approval(self, task_id: str, tool_name: str, tool_input: dict, capability: str) -> str: ...
    async def wait_for_approval(self, approval_id: str) -> bool: ...
    def resolve_approval(self, approval_id: str, approved: bool) -> bool: ...
    def consume_messages(self, task_id: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]: ...
    def enqueue_message(self, task_id: str, kind: str, content: str) -> bool: ...
    def cancel(self, task_id: str) -> bool: ...


class NullRuntimeHost:
    """In-memory/no-op host used by the standalone SDK and unit tests."""

    async def run_hooks(self, _event: str, _payload: dict[str, Any], _cwd: str | None) -> list[dict]:
        return []

    def ensure_not_cancelled(self, _task_id: str | None) -> None:
        return None

    def save_checkpoint(self, _task_id: str, _checkpoint: dict[str, Any]) -> None:
        return None

    def record_usage(
        self, _task_id: str | None, _provider: str, _model: str, _usage: dict[str, Any],
    ) -> None:
        return None

    @staticmethod
    def permission_action(
        permission_mode: str, capability: str,
    ) -> Literal["allow", "approve", "deny"]:
        if capability == "read" or permission_mode == "full-access":
            return "allow"
        if permission_mode == "read-only":
            return "deny"
        if capability == "write":
            return "allow"
        return "approve"

    def create_approval(
        self, _task_id: str, _tool_name: str, _tool_input: dict, _capability: str,
    ) -> str:
        return ""

    async def wait_for_approval(self, _approval_id: str) -> bool:
        return False

    def resolve_approval(self, _approval_id: str, _approved: bool) -> bool:
        return False

    def consume_messages(self, _task_id: str, _kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        return []

    def enqueue_message(self, _task_id: str, _kind: str, _content: str) -> bool:
        return False

    def cancel(self, _task_id: str) -> bool:
        return False
