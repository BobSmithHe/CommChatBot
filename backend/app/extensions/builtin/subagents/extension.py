from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Literal

from app.agent_runtime import (
    AgentRuntime,
    AgentRuntimeHost,
    ExtensionContext,
    ExtensionContribution,
    ExtensionManifest,
    TaskCancelled,
    Tool,
)


class _SubagentHost:
    """Map one child runtime onto a durable subtask without inventing AgentTask rows."""

    def __init__(self, base: AgentRuntimeHost, manager, task_id: str, subtask_id: str) -> None:
        self.base = base
        self.manager = manager
        self.task_id = task_id
        self.subtask_id = subtask_id

    async def run_hooks(self, event: str, payload: dict[str, Any], cwd: str | None) -> list[dict]:
        return await self.base.run_hooks(event, {**payload, "subtask_id": self.subtask_id}, cwd)

    def ensure_not_cancelled(self, _task_id: str | None) -> None:
        self.manager.ensure_subtask_not_cancelled(self.task_id, self.subtask_id)

    def save_checkpoint(self, _task_id: str, checkpoint: dict[str, Any]) -> None:
        self.manager.save_subtask_checkpoint(self.task_id, self.subtask_id, checkpoint)

    def record_usage(self, _task_id: str | None, provider: str, model: str, usage: dict[str, Any]) -> None:
        self.base.record_usage(self.task_id, provider, model, usage)

    def permission_action(self, _mode: str, capability: str) -> Literal["allow", "approve", "deny"]:
        return "allow" if capability == "read" else "deny"

    def create_approval(self, *_args, **_kwargs) -> str:
        return ""

    async def wait_for_approval(self, _approval_id: str) -> bool:
        return False

    def resolve_approval(self, _approval_id: str, _approved: bool) -> bool:
        return False

    def consume_messages(self, _task_id: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        return self.manager.consume_subtask_messages(self.task_id, self.subtask_id, kinds)

    def enqueue_message(self, _task_id: str, kind: str, content: str) -> bool:
        return bool(self.manager.enqueue_subtask_message(self.task_id, self.subtask_id, kind, content))

    def cancel(self, _task_id: str) -> bool:
        return self.manager.cancel_subtask(self.task_id, self.subtask_id)


class SubagentExtension:
    manifest = ExtensionManifest(
        id="coding.subagents", modes=frozenset({"coding"}),
        requires=("coding.workspace", "coding.diagnostics", "coding.git"),
        description="Persistent inspectable child-agent tree with steering and recovery.",
        permissions=frozenset({"read"}),
    )

    def __init__(self) -> None:
        self._jobs: dict[str, asyncio.Task[str]] = {}
        self._ephemeral: dict[str, dict[str, Any]] = {}

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        workspace = context.require("workspace")
        provider = context.require("provider")
        registry = context.require("tool_registry")
        settings = context.require("settings")
        manager = context.require("task_manager")
        runtime_host = context.require("runtime_host")
        parent_task_id = context.request.get("task_id")

        def read_tools():
            return [
                registry.list_files_tool(workspace), registry.read_file_tool(workspace),
                registry.search_files_tool(workspace), registry.get_diagnostics_tool(workspace),
                registry.review_changes_tool(workspace),
            ]

        async def run_child(subtask_id: str, task: str, focus: str, model: str) -> str:
            durable = bool(parent_task_id)
            host = (
                _SubagentHost(runtime_host, manager, parent_task_id, subtask_id)
                if durable else runtime_host
            )
            if durable:
                manager.set_subtask_status(parent_task_id, subtask_id, "running")
                resume_state = manager.load_subtask_checkpoint(parent_task_id, subtask_id)
            else:
                self._ephemeral[subtask_id]["status"] = "running"
                resume_state = None
            runtime = AgentRuntime(
                provider=provider, model=model, tools=read_tools(),
                system_prompt=(
                    "You are a bounded read-only coding sub-agent. Inspect the shared workspace and independently "
                    "analyze the delegated task. Do not edit files or run commands. Return concrete findings, file "
                    "paths, risks, and a recommended approach to the parent agent."
                ),
                max_turns=max(2, min(8, settings.max_agent_turns)), permission_mode="read-only",
                workspace_dir=str(workspace.root), host=host,
                task_id=subtask_id if durable else None,
                context_window=settings.model_context_tokens, max_output_tokens=settings.model_max_tokens,
                context_compaction=settings.llm_context_compaction,
                context_compaction_trigger_ratio=settings.context_compaction_trigger_ratio,
            )
            final = ""
            try:
                initial_prompt = None if resume_state else f"Task: {task}\nFocus: {focus}"
                async for event in runtime.run(initial_prompt, [], resume_state=resume_state):
                    if durable:
                        manager.ensure_subtask_not_cancelled(parent_task_id, subtask_id)
                    if event.type == "final_answer":
                        final = str(event.data.get("content") or "")
                    elif event.type == "message_update" and event.data.get("final"):
                        final += str(event.data.get("delta") or "")
                    if durable and event.type in {
                        "turn_start", "turn_end", "thinking", "tool_start", "tool_update", "tool_end",
                    }:
                        manager.append_event(parent_task_id, "subagent_progress", {
                            "id": subtask_id, "event": event.type, "data": event.data,
                        })
                if not final.strip():
                    raise RuntimeError("Sub-agent completed without a report")
                if durable:
                    manager.update_subtask(parent_task_id, subtask_id, "completed", result=final)
                else:
                    self._ephemeral[subtask_id].update(status="completed", result=final)
                return final
            except (TaskCancelled, asyncio.CancelledError):
                if durable:
                    manager.update_subtask(parent_task_id, subtask_id, "cancelled", error="Interrupted")
                else:
                    self._ephemeral[subtask_id].update(status="cancelled", error="Interrupted")
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if durable:
                    manager.update_subtask(parent_task_id, subtask_id, "failed", error=error)
                else:
                    self._ephemeral[subtask_id].update(status="failed", error=error)
                raise

        def launch(subtask_id: str, task: str, focus: str, model: str) -> None:
            existing = self._jobs.get(subtask_id)
            if existing and not existing.done():
                return
            self._jobs[subtask_id] = asyncio.create_task(
                run_child(subtask_id, task, focus, model), name=f"subagent:{subtask_id}",
            )

        async def spawn_agent(
            task: str, focus: str = "", model: str = "", parent_id: str = "",
        ) -> str:
            selected_model = model or settings.coding_model_id or getattr(provider, "default_model", "") or settings.deepseek_model
            request = {"task": task, "focus": focus, "model": selected_model, "parent_id": parent_id or None}
            if parent_task_id:
                subtask_id = manager.create_subtask(
                    parent_task_id, task, focus, model=selected_model,
                    parent_subtask_id=parent_id or None, status="queued", request=request,
                )
            else:
                subtask_id = uuid.uuid4().hex
                self._ephemeral[subtask_id] = {
                    "id": subtask_id, "name": task, "focus": focus, "model": selected_model,
                    "parent_id": parent_id or None, "status": "queued", "result": None, "error": None,
                }
            launch(subtask_id, task, focus, selected_model)
            return json.dumps({"id": subtask_id, "status": "running"}, ensure_ascii=False)

        def status(subtask_id: str) -> dict[str, Any] | None:
            if parent_task_id:
                return manager.subtask(parent_task_id, subtask_id)
            return self._ephemeral.get(subtask_id)

        async def wait_agent(agent_id: str, timeout_seconds: float = 300) -> str:
            item = status(agent_id)
            if not item:
                raise ValueError(f"Sub-agent not found: {agent_id}")
            if item["status"] in {"interrupted", "queued"} and agent_id not in self._jobs:
                request = item.get("request") or {
                    "task": item["name"], "focus": item.get("focus", ""), "model": item.get("model", ""),
                }
                launch(agent_id, request["task"], request.get("focus", ""), request.get("model", ""))
            job = self._jobs.get(agent_id)
            if job and not job.done():
                try:
                    await asyncio.wait_for(asyncio.shield(job), timeout=max(0.1, min(timeout_seconds, 3600)))
                except asyncio.TimeoutError:
                    return json.dumps({"id": agent_id, "status": "running", "timed_out": True})
            item = status(agent_id) or {}
            return json.dumps(item, ensure_ascii=False, default=str)

        async def delegate_task(task: str, focus: str = "") -> str:
            created = json.loads(await spawn_agent(task, focus))
            report = json.loads(await wait_agent(created["id"]))
            if report.get("status") != "completed":
                raise RuntimeError(report.get("error") or f"Sub-agent ended with {report.get('status')}")
            return str(report.get("result") or "")

        async def send_agent(agent_id: str, message: str, kind: str = "steering") -> str:
            if kind not in {"steering", "follow_up"}:
                raise ValueError("kind must be steering or follow_up")
            if not parent_task_id:
                raise ValueError("Persistent child messaging requires a durable parent task")
            queued = manager.enqueue_subtask_message(parent_task_id, agent_id, kind, message)
            if not queued:
                raise ValueError("Sub-agent is not accepting messages")
            return json.dumps({"agent_id": agent_id, **queued}, ensure_ascii=False)

        async def interrupt_agent(agent_id: str) -> str:
            item = status(agent_id)
            if not item:
                raise ValueError(f"Sub-agent not found: {agent_id}")
            job = self._jobs.get(agent_id)
            if job and not job.done():
                job.cancel()
            if parent_task_id:
                manager.cancel_subtask(parent_task_id, agent_id)
            else:
                self._ephemeral[agent_id]["status"] = "cancelled"
            return json.dumps({"id": agent_id, "status": "cancelled"})

        async def list_agents() -> str:
            items = manager.list_subtasks(parent_task_id) if parent_task_id else list(self._ephemeral.values())
            return json.dumps({"agents": items}, ensure_ascii=False, default=str)

        tools = [
            Tool("spawn_agent", "Start an inspectable read-only child agent and return immediately.", {
                "type": "object", "properties": {
                    "task": {"type": "string"}, "focus": {"type": "string"},
                    "model": {"type": "string"}, "parent_id": {"type": "string"},
                }, "required": ["task"], "additionalProperties": False,
            }, spawn_agent, execution_mode="parallel"),
            Tool("send_agent", "Send steering or follow-up input to a running child agent.", {
                "type": "object", "properties": {
                    "agent_id": {"type": "string"}, "message": {"type": "string"},
                    "kind": {"type": "string", "enum": ["steering", "follow_up"]},
                }, "required": ["agent_id", "message"], "additionalProperties": False,
            }, send_agent),
            Tool("wait_agent", "Wait for a child agent and return its durable status and result.", {
                "type": "object", "properties": {
                    "agent_id": {"type": "string"}, "timeout_seconds": {"type": "number", "minimum": 0.1},
                }, "required": ["agent_id"], "additionalProperties": False,
            }, wait_agent, execution_mode="parallel"),
            Tool("interrupt_agent", "Interrupt one child agent without cancelling the parent task.", {
                "type": "object", "properties": {"agent_id": {"type": "string"}},
                "required": ["agent_id"], "additionalProperties": False,
            }, interrupt_agent),
            Tool("list_agents", "List the persistent child-agent task tree and current statuses.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, list_agents),
            Tool("delegate_task", "Compatibility helper: spawn and wait for one read-only child agent.", {
                "type": "object", "properties": {
                    "task": {"type": "string"}, "focus": {"type": "string"},
                }, "required": ["task"], "additionalProperties": False,
            }, delegate_task, execution_mode="parallel"),
        ]
        return ExtensionContribution(tools=tools)
