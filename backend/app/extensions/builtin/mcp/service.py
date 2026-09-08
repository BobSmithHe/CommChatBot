from __future__ import annotations

import json
import os
import re
import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from app.infra.config import get_settings
from app.agent_runtime import Tool
from app.extensions.builtin.workspace import WorkspaceEditor
from app.platform.services.lazy import LazyService


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    url: str = ""
    headers: dict[str, str] | None = None
    oauth: dict[str, str] | None = None


class _MCPConnection:
    """A single-task session owner, avoiding cross-task AnyIO cancel-scope use."""

    def __init__(self, runtime: "MCPClientRuntime", server: MCPServerConfig, cwd: Path) -> None:
        self.runtime = runtime
        self.server = server
        self.cwd = cwd
        self.queue: asyncio.Queue[tuple[str, tuple, dict, asyncio.Future] | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def request(self, operation: str, *args, **kwargs) -> Any:
        async with self.lock:
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._worker())
            future = asyncio.get_running_loop().create_future()
            self.queue.put_nowait((operation, args, kwargs, future))
        return await future

    async def _worker(self) -> None:
        idle = max(5, get_settings().mcp_idle_timeout_seconds)
        try:
            async with self.runtime._session(self.server, self.cwd) as session:
                while True:
                    try:
                        request = await asyncio.wait_for(self.queue.get(), timeout=idle)
                    except TimeoutError:
                        return
                    if request is None:
                        return
                    operation, args, kwargs, future = request
                    if future.cancelled():
                        continue
                    try:
                        result = await getattr(session, operation)(*args, **kwargs)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        future.set_exception(exc)
                        while not self.queue.empty():
                            queued = self.queue.get_nowait()
                            if queued is not None and not queued[3].done():
                                queued[3].set_exception(RuntimeError("MCP session disconnected; retry the tool call"))
                        return
                    else:
                        future.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            while not self.queue.empty():
                queued = self.queue.get_nowait()
                if queued is not None and not queued[3].done():
                    queued[3].set_exception(exc)

    async def close(self) -> None:
        if self.task and not self.task.done():
            await self.queue.put(None)
            await asyncio.gather(self.task, return_exceptions=True)


class MCPClientRuntime:
    """MCP client backed by the official Python SDK.

    Connections are pooled per server/workspace and reconnect after failure or
    idle expiry. Agent checkpoints still never depend on a live MCP session.
    """

    def __init__(self) -> None:
        self._connections: dict[str, _MCPConnection] = {}
        self._oauth_tokens: dict[str, tuple[str, float]] = {}

    async def load_tools(self, workspace: WorkspaceEditor, *, trusted: bool) -> tuple[list[Tool], list[str]]:
        if not trusted:
            return [], []
        servers = self._configs(workspace)
        tools: list[Tool] = []
        errors: list[str] = []
        for server in servers:
            try:
                definitions = await self._list(server, workspace.root)
            except Exception as exc:
                errors.append(f"{server.name}: {type(exc).__name__}: {exc}")
                continue
            for definition in definitions:
                remote_name = str(self._field(definition, "name") or "")
                if not remote_name:
                    continue
                local_name = self._tool_name(server.name, remote_name)
                description = str(self._field(definition, "description") or f"MCP tool {remote_name}")
                schema = self._field(definition, "inputSchema") or self._field(definition, "input_schema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}

                async def handler(_server=server, _remote_name=remote_name, **kwargs):
                    return await self._call(_server, workspace.root, _remote_name, kwargs)

                tools.append(Tool(
                    name=local_name,
                    description=f"[MCP:{server.name}] {description}",
                    input_schema=schema,
                    handler=handler,
                    capability="network" if server.url else "execute",
                ))
            tools.extend(self._server_capability_tools(server, workspace.root))
        return tools, errors

    def _server_capability_tools(self, server: MCPServerConfig, cwd: Path) -> list[Tool]:
        capability = "network" if server.url else "execute"

        async def list_resources() -> str:
            return await self._session_call(server, cwd, "list_resources")

        async def read_resource(uri: str) -> str:
            return await self._session_call(server, cwd, "read_resource", uri)

        async def list_prompts() -> str:
            return await self._session_call(server, cwd, "list_prompts")

        async def get_prompt(name: str, arguments: dict[str, str] | None = None) -> str:
            return await self._session_call(server, cwd, "get_prompt", name, arguments=arguments or {})

        prefix = server.name
        return [
            Tool(self._tool_name(prefix, "list_resources"), f"List resources exposed by MCP server {server.name}.", {
                "type": "object", "properties": {},
            }, list_resources, capability=capability),
            Tool(self._tool_name(prefix, "read_resource"), f"Read one resource from MCP server {server.name}.", {
                "type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"],
            }, read_resource, capability=capability),
            Tool(self._tool_name(prefix, "list_prompts"), f"List prompts exposed by MCP server {server.name}.", {
                "type": "object", "properties": {},
            }, list_prompts, capability=capability),
            Tool(self._tool_name(prefix, "get_prompt"), f"Render a prompt from MCP server {server.name}.", {
                "type": "object", "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object", "additionalProperties": {"type": "string"}},
                }, "required": ["name"],
            }, get_prompt, capability=capability),
        ]

    def _configs(self, workspace: WorkspaceEditor) -> list[MCPServerConfig]:
        candidates = [workspace.root / ".commchat" / "mcp.json"]
        configured = get_settings().mcp_config_path.strip()
        if configured:
            candidates.append(Path(configured).expanduser())
        merged: dict[str, Any] = {}
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            servers = payload.get("mcpServers", payload.get("servers", {})) if isinstance(payload, dict) else {}
            if isinstance(servers, dict):
                merged.update(servers)
        result: list[MCPServerConfig] = []
        for name, raw in list(merged.items())[:20]:
            if not isinstance(raw, dict) or raw.get("disabled") is True:
                continue
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name))[:48]
            command = str(raw.get("command") or "").strip()
            url = str(raw.get("url") or "").strip()
            if not clean_name or not (command or url):
                continue
            env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
            headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
            oauth = raw.get("oauth") if isinstance(raw.get("oauth"), dict) else {}
            result.append(MCPServerConfig(
                name=clean_name,
                command=command,
                args=tuple(str(item) for item in raw.get("args", []) if isinstance(item, (str, int, float))),
                env={str(k): self._expand(str(v)) for k, v in env.items()},
                url=url,
                headers={str(k): self._expand(str(v)) for k, v in headers.items()},
                oauth={str(k): self._expand(str(v)) for k, v in oauth.items()},
            ))
        return result

    @asynccontextmanager
    async def _session(self, server: MCPServerConfig, cwd: Path) -> AsyncIterator[Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise RuntimeError("MCP support requires the 'mcp' Python package") from exc
        if server.url:
            headers = await self._headers(server)
            async with streamablehttp_client(server.url, headers=headers or None) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            return
        environment = dict(os.environ)
        environment.update(server.env or {})
        parameters = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            env=environment,
            cwd=str(cwd),
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _list(self, server: MCPServerConfig, cwd: Path) -> list[Any]:
        response = await self._request(server, cwd, "list_tools")
        return list(getattr(response, "tools", []) or [])

    async def _call(self, server: MCPServerConfig, cwd: Path, name: str, arguments: dict[str, Any]) -> str:
        result = await self._request(server, cwd, "call_tool", name, arguments=arguments)
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        return json.dumps(payload, ensure_ascii=False, default=str)

    async def _session_call(self, server: MCPServerConfig, cwd: Path, operation: str, *args, **kwargs) -> str:
        result = await self._request(server, cwd, operation, *args, **kwargs)
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        return json.dumps(payload, ensure_ascii=False, default=str)

    async def _request(self, server: MCPServerConfig, cwd: Path, operation: str, *args, **kwargs) -> Any:
        identity = json.dumps({
            "name": server.name, "command": server.command, "args": server.args,
            "url": server.url, "cwd": str(cwd), "headers": server.headers, "oauth": server.oauth,
        }, sort_keys=True, ensure_ascii=False)
        connection = self._connections.get(identity)
        if connection is None:
            connection = _MCPConnection(self, server, cwd)
            self._connections[identity] = connection
        return await connection.request(operation, *args, **kwargs)

    async def _headers(self, server: MCPServerConfig) -> dict[str, str]:
        headers = dict(server.headers or {})
        oauth = server.oauth or {}
        token_url = oauth.get("token_url", "").strip()
        if not token_url:
            return headers
        identity = "\0".join((token_url, oauth.get("client_id", ""), oauth.get("scope", "")))
        cached = self._oauth_tokens.get(identity)
        if cached and cached[1] > time.time() + 30:
            headers["Authorization"] = f"Bearer {cached[0]}"
            return headers
        data = {"grant_type": "client_credentials"}
        if oauth.get("scope"):
            data["scope"] = oauth["scope"]
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                token_url, data=data,
                auth=(oauth.get("client_id", ""), oauth.get("client_secret", "")),
            )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("OAuth token response did not contain access_token")
        self._oauth_tokens[identity] = (token, time.time() + max(60, int(payload.get("expires_in", 3600))))
        headers["Authorization"] = f"Bearer {token}"
        return headers

    async def close(self) -> None:
        connections = list(self._connections.values())
        self._connections.clear()
        if connections:
            await asyncio.gather(*(connection.close() for connection in connections), return_exceptions=True)

    @staticmethod
    def _field(value: Any, name: str) -> Any:
        if isinstance(value, dict):
            return value.get(name)
        return getattr(value, name, None)

    @staticmethod
    def _tool_name(server: str, tool: str) -> str:
        clean = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp_{server}_{tool}")
        return clean[:64]

    @staticmethod
    def _expand(value: str) -> str:
        return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", lambda match: os.getenv(match.group(1), ""), value)


mcp_client_runtime = LazyService(MCPClientRuntime)
