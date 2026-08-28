from __future__ import annotations

from typing import AsyncIterator

from ..core.workspace import WorkspaceEditor
from ..infra.config import get_settings
from ..packages.agent import AgentRuntime
from ..packages.ai import ModelProvider
from .common import history_to_agent_messages, map_agent_event
from .tool_registry import ProductToolRegistry


class CodingAgentMode:
    """Coding-agent product shell.

    This is the third-layer wrapper corresponding to MiniCode-PI's
    coding-agent product package: it chooses coding tools and working prompt,
    then delegates execution to the generic AgentRuntime.
    """

    def __init__(self, *, provider: ModelProvider, tools: ProductToolRegistry) -> None:
        self.provider = provider
        self.tools = tools
        self.settings = get_settings()

    async def stream(
        self,
        *,
        message: str,
        history: list[dict],
        system_context: str | None = None,
        workspace_dir: str | None = None,
        task_id: str | None = None,
        permission_mode: str = "workspace-write",
        resume_state: dict | None = None,
    ) -> AsyncIterator[dict]:
        workspace = WorkspaceEditor(workspace_dir) if workspace_dir else None
        tools = self.tools.coding_agent_tools(workspace)
        runtime = AgentRuntime(
            provider=self.provider,
            model=self.settings.coding_model_id or self.settings.deepseek_model,
            tools=tools,
            system_prompt=system_context or self._system_prompt(tools),
            max_turns=self.settings.max_agent_turns,
            task_id=task_id,
            permission_mode=permission_mode,
            workspace_dir=workspace_dir,
        )
        yield {"event": "status", "content": "CodingAgent AgentRuntime started"}
        async for event in runtime.run(
            message,
            history_to_agent_messages(history),
            resume_state=resume_state,
        ):
            mapped = map_agent_event(event)
            if mapped:
                yield mapped

    @staticmethod
    def _system_prompt(tools) -> str:
        names = ", ".join(tool.name for tool in tools)
        return (
            "You are a coding agent working inside this conversation's isolated project workspace. "
            "Inspect relevant files before editing. Use exact replacements for existing files and write_file for new files. "
            "Never claim a file changed unless a write tool succeeded. Verify relevant changes with run_command when possible. "
            "Use execute_python for temporary calculations only. "
            "Return concise Markdown summarizing changed files and verification. "
            f"Available tools: {names}."
        )
