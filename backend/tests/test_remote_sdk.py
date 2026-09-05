from __future__ import annotations

import asyncio

import httpx

from app.agent_runtime import RemoteAgentClient


def test_remote_sdk_stream_and_task_controls() -> None:
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/chat/stream":
            return httpx.Response(
                200,
                text=(
                    'event: task\ndata: {"task_id":"abc"}\n\n'
                    'event: answer\ndata: "done"\n\n'
                    'event: done\ndata: null\n\n'
                ),
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path.endswith("/subtasks"):
            return httpx.Response(200, json=[{"id": "child", "status": "running"}])
        return httpx.Response(200, json={"status": "ok"})

    async def scenario():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test")
        sdk = RemoteAgentClient("http://unused", client=http)
        events = [event async for event in sdk.stream("inspect", mode="coding-agent")]
        assert [(event.event, event.data) for event in events] == [
            ("task", {"task_id": "abc"}), ("answer", "done"), ("done", None),
        ]
        await sdk.task("task")
        await sdk.steer("task", "focus")
        await sdk.follow_up("task", "continue")
        await sdk.cancel("task")
        assert (await sdk.subagents("task"))[0]["id"] == "child"
        await sdk.send_agent("task", "child", "check tests")
        await sdk.interrupt_agent("task", "child")
        await http.aclose()

    asyncio.run(scenario())
    assert ("POST", "/api/tasks/task/subtasks/child/messages") in requests
    assert ("POST", "/api/tasks/task/subtasks/child/interrupt") in requests
