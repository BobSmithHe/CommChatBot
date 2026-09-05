from __future__ import annotations

from typing import Any, Literal

from .services.hooks import hook_runner
from .services.task_runtime import runtime_task_manager


class PlatformAgentRuntimeHost:
    """Adapter from the portable agent core to CommChat platform services."""

    async def run_hooks(self, event: str, payload: dict[str, Any], cwd: str | None) -> list[dict]:
        return list(await hook_runner.run(event, payload, cwd))

    def ensure_not_cancelled(self, task_id: str | None) -> None:
        runtime_task_manager.ensure_not_cancelled(task_id)

    def save_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        runtime_task_manager.save_checkpoint(task_id, checkpoint)

    def record_usage(
        self, task_id: str | None, provider: str, model: str, usage: dict[str, Any],
    ) -> None:
        runtime_task_manager.record_usage(task_id, provider, model, usage)

    def permission_action(
        self, permission_mode: str, capability: str,
    ) -> Literal["allow", "approve", "deny"]:
        return runtime_task_manager.permission_action(permission_mode, capability)

    def create_approval(
        self, task_id: str, tool_name: str, tool_input: dict, capability: str,
    ) -> str:
        return runtime_task_manager.create_approval(task_id, tool_name, tool_input, capability)

    async def wait_for_approval(self, approval_id: str) -> bool:
        return await runtime_task_manager.wait_for_approval(approval_id)

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        return runtime_task_manager.resolve_approval(approval_id, approved)

    def consume_messages(self, task_id: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        return runtime_task_manager.consume_messages(task_id, kinds)

    def enqueue_message(self, task_id: str, kind: str, content: str) -> bool:
        return bool(runtime_task_manager.enqueue_message(task_id, kind, content))

    def cancel(self, task_id: str) -> bool:
        return runtime_task_manager.cancel(task_id)
