from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx


@dataclass(frozen=True)
class RemoteAgentEvent:
    event: str
    data: Any = None


class RemoteAgentClient:
    """Async HTTP/SSE SDK for a running CommChat Agent App Server.

    The wire surface is task-oriented: start/resume a streamed run, inspect
    durable task state, steer or cancel it, and control child-agent threads.
    """

    def __init__(
        self, base_url: str, *, access_token: str = "", timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers,
            timeout=httpx.Timeout(timeout, read=None),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> "RemoteAgentClient":
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()

    async def stream(
        self,
        message: str,
        *,
        mode: Literal["chatbot", "coding-agent"] = "coding-agent",
        conversation_id: int | None = None,
        use_rag: bool = False,
        use_web: bool = False,
        system_context: str | None = None,
        resume_task_id: str | None = None,
    ) -> AsyncIterator[RemoteAgentEvent]:
        payload = {
            "message": message,
            "mode": mode,
            "conversation_id": conversation_id,
            "use_rag": use_rag,
            "use_web": use_web,
            "system_context": system_context,
            "resume_task_id": resume_task_id,
        }
        async with self.client.stream("POST", "/api/chat/stream", json=payload) as response:
            response.raise_for_status()
            event_name = "message"
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if not line:
                    if data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            data = raw
                        yield RemoteAgentEvent(event_name, data)
                    event_name, data_lines = "message", []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())

    async def task(self, task_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/tasks/{task_id}")

    async def steer(self, task_id: str, content: str) -> dict[str, Any]:
        return await self._task_message(task_id, "steering", content)

    async def follow_up(self, task_id: str, content: str) -> dict[str, Any]:
        return await self._task_message(task_id, "follow_up", content)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/api/tasks/{task_id}/cancel")

    async def subagents(self, task_id: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/api/tasks/{task_id}/subtasks")

    async def subagent(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/tasks/{task_id}/subtasks/{agent_id}")

    async def send_agent(
        self, task_id: str, agent_id: str, content: str,
        *, kind: Literal["steering", "follow_up"] = "steering",
    ) -> dict[str, Any]:
        return await self._json(
            "POST", f"/api/tasks/{task_id}/subtasks/{agent_id}/messages",
            json={"kind": kind, "content": content},
        )

    async def interrupt_agent(self, task_id: str, agent_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/api/tasks/{task_id}/subtasks/{agent_id}/interrupt")

    async def _task_message(self, task_id: str, kind: str, content: str) -> dict[str, Any]:
        return await self._json(
            "POST", f"/api/tasks/{task_id}/messages", json={"kind": kind, "content": content},
        )

    async def _json(self, method: str, url: str, **kwargs) -> Any:
        response = await self.client.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()
