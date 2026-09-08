from __future__ import annotations

from typing import Any, Literal

from .services.hooks import HookRunner, hook_runner
from .services.task_runtime import RuntimeTaskManager, runtime_task_manager


class PlatformAgentRuntimeHost:
    """Adapter from the portable agent core to CommChat platform services."""

    def __init__(
        self,
        *,
        hooks: HookRunner | None = None,
        tasks: RuntimeTaskManager | None = None,
    ) -> None:
        self.hooks = hooks or hook_runner
        self.tasks = tasks or runtime_task_manager

    async def run_hooks(self, event: str, payload: dict[str, Any], cwd: str | None) -> list[dict]:
        return list(await self.hooks.run(event, payload, cwd))

    def ensure_not_cancelled(self, task_id: str | None) -> None:
        self.tasks.ensure_not_cancelled(task_id)

    def save_checkpoint(self, task_id: str, checkpoint: dict[str, Any]) -> None:
        self.tasks.save_checkpoint(task_id, checkpoint)

    def record_usage(
        self, task_id: str | None, provider: str, model: str, usage: dict[str, Any],
    ) -> None:
        self.tasks.record_usage(task_id, provider, model, usage)

    def permission_action(
        self, permission_mode: str, capability: str,
    ) -> Literal["allow", "approve", "deny"]:
        return self.tasks.permission_action(permission_mode, capability)

    def create_approval(
        self, task_id: str, tool_name: str, tool_input: dict, capability: str,
    ) -> str:
        return self.tasks.create_approval(task_id, tool_name, tool_input, capability)

    async def wait_for_approval(self, approval_id: str) -> bool:
        return await self.tasks.wait_for_approval(approval_id)

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        return self.tasks.resolve_approval(approval_id, approved)

    def consume_messages(self, task_id: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        return self.tasks.consume_messages(task_id, kinds)

    def enqueue_message(self, task_id: str, kind: str, content: str) -> bool:
        return bool(self.tasks.enqueue_message(task_id, kind, content))

    def cancel(self, task_id: str) -> bool:
        return self.tasks.cancel(task_id)
