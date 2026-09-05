from __future__ import annotations

import asyncio
import json

from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest, Tool
from .project_store import ProjectMemoryStore


class ChatMemoryExtension:
    manifest = ExtensionManifest(
        id="chat.memory", modes=frozenset({"chatbot"}),
        description="Conversation and user memory recall.", permissions=frozenset({"read"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        request = context.request
        user_id = request.get("user_id")
        if user_id is None:
            return ExtensionContribution(values={"memory_context": ""})
        memory = context.require("memory")
        history = list(request.get("history") or [])
        query = request.get("message") or next(
            (str(item.get("content") or "") for item in reversed(history) if item.get("role") == "user"), "",
        )
        memories = await asyncio.to_thread(
            memory.recall, user_id=user_id, query=query,
            conversation_id=request.get("conversation_id"), task_id=request.get("task_id"),
        )
        events = []
        if memories:
            events.append({"event": "memory_recalled", "content": {
                "count": len(memories),
                "memories": [{"id": item["id"], "scope": item["scope"], "key": item["key"]} for item in memories],
            }})
        return ExtensionContribution(events=events, values={"memory_context": memory.format_for_prompt(memories)})


class ProjectMemoryExtension:
    manifest = ExtensionManifest(
        id="coding.memory", modes=frozenset({"coding"}),
        description="Project-scoped durable memory and contextual recall.",
        permissions=frozenset({"read", "write"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        workspace = context.require("workspace")
        memory = context.require("memory")
        request = context.request
        memory_store = ProjectMemoryStore()
        project_id = workspace.project_identity()
        user_id = request.get("user_id")
        task_id = request.get("task_id")
        conversation_id = request.get("conversation_id")
        preferences = memory.get_settings(user_id) if user_id is not None else None
        target = memory.target("project", user_id=user_id, target_value=project_id) if user_id is not None else None
        if user_id is not None and target is not None and preferences and preferences["enabled"] and preferences["project_scope"]:
            for key, value in memory_store.read(project_id).items():
                try:
                    memory.upsert(
                        user_id=user_id, target=target, key=key, value=value,
                        category="fact", source_type="legacy-json",
                    )
                except ValueError:
                    pass

        def read_project_memory() -> str:
            if user_id is None or target is None:
                return json.dumps(memory_store.read(project_id), ensure_ascii=False)
            if not preferences or not preferences["enabled"]:
                return json.dumps({}, ensure_ascii=False)
            rows = memory.list(user_id, scope="project", target_id=target.target_id)
            return json.dumps({item["key"]: item["value"] for item in rows}, ensure_ascii=False)

        def remember_project(key: str, value: str) -> str:
            if user_id is None or target is None:
                return json.dumps(memory_store.remember(project_id, key, value), ensure_ascii=False)
            if not preferences or not preferences["enabled"] or not preferences["project_scope"]:
                raise ValueError("Project memory is disabled in privacy settings")
            return json.dumps(memory.upsert(
                user_id=user_id, target=target, key=key, value=value,
                category="fact", confidence=1.0, source_type="agent-explicit", source_task_id=task_id,
            ), ensure_ascii=False)

        def forget_project_memory(key: str) -> str:
            if user_id is None or target is None:
                return json.dumps(memory_store.forget(project_id, key), ensure_ascii=False)
            if not preferences or not preferences["enabled"]:
                raise ValueError("Project memory is disabled in privacy settings")
            rows = memory.list(user_id, scope="project", target_id=target.target_id)
            match = next((item for item in rows if item["key"] == key), None)
            if not match:
                raise ValueError(f"Project memory key not found: {key}")
            memory.delete(user_id, match["id"])
            return json.dumps({"deleted": key}, ensure_ascii=False)

        tools = [
            Tool("read_project_memory", "Read durable preferences and project facts shared by conversations for this project.", {"type": "object", "properties": {}}, read_project_memory),
            Tool("remember_project", "Persist a stable project fact or user preference for future conversations on this project.", {
                "type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
            }, remember_project, capability="write"),
            Tool("forget_project_memory", "Remove one durable project memory by its exact key.", {
                "type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"],
            }, forget_project_memory, capability="write"),
        ]
        events: list[dict] = []
        if user_id is not None:
            memories = await asyncio.to_thread(
                memory.recall, user_id=user_id, query=request.get("message") or "current coding task",
                conversation_id=conversation_id, task_id=task_id, project_identity=project_id,
            )
            memory_context = memory.format_for_prompt(memories)
            if memories:
                events.append({"event": "memory_recalled", "content": {
                    "count": len(memories),
                    "memories": [{"id": item["id"], "scope": item["scope"], "key": item["key"]} for item in memories],
                }})
        else:
            memory_context = memory_store.format_for_prompt(project_id)
        return ExtensionContribution(
            tools=tools, events=events, prompt_sections=[f"Durable project memory:\n{memory_context}"],
            values={"project_identity": project_id},
        )
