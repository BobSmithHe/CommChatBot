from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator

from ..providers import ModelProvider
from .events import AgentEvent
from .host import AgentRuntimeHost, NullRuntimeHost
from .messages import AgentMessage
from .runtime import AgentRuntime
from .tools import Tool


@dataclass
class AgentSessionConfig:
    provider: ModelProvider
    model: str
    system_prompt: str
    tools: list[Tool] = field(default_factory=list)
    max_turns: int = 8
    task_id: str | None = None
    permission_mode: str = "workspace-write"
    workspace_dir: str | None = None
    host: AgentRuntimeHost = field(default_factory=NullRuntimeHost)


class AgentSession:
    """Programmatic facade over AgentRuntime's event protocol."""

    def __init__(self, config: AgentSessionConfig) -> None:
        self.config = config
        self.runtime = AgentRuntime(
            provider=config.provider,
            model=config.model,
            tools=list(config.tools),
            system_prompt=config.system_prompt,
            max_turns=config.max_turns,
            task_id=config.task_id,
            permission_mode=config.permission_mode,
            workspace_dir=config.workspace_dir,
            host=config.host,
        )

    async def prompt(
        self,
        prompt: str | None,
        *,
        history: list[AgentMessage] | None = None,
        resume_state: dict | None = None,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self.runtime.run(prompt, history, resume_state):
            yield event

    def steer(self, content: str) -> bool:
        return bool(
            self.config.task_id
            and self.config.host.enqueue_message(self.config.task_id, "steering", content)
        )

    def follow_up(self, content: str) -> bool:
        return bool(
            self.config.task_id
            and self.config.host.enqueue_message(self.config.task_id, "follow_up", content)
        )

    def cancel(self) -> bool:
        return bool(self.config.task_id and self.config.host.cancel(self.config.task_id))


def create_agent_session(config: AgentSessionConfig) -> AgentSession:
    return AgentSession(config)
