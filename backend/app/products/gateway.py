from __future__ import annotations

from collections.abc import AsyncIterator

from .chatbot import ChatbotMode
from .coding_agent import CodingAgentMode


class ProductGateway:
    """Application facade selecting the Chat or Coding product profile."""

    def __init__(self, *, chatbot: ChatbotMode, coding_agent: CodingAgentMode) -> None:
        self.chatbot = chatbot
        self.coding_agent = coding_agent

    async def stream(
        self,
        *,
        message: str | None,
        history: list[dict],
        mode: str,
        use_rag: bool,
        use_web: bool,
        system_context: str | None = None,
        workspace_dir: str | None = None,
        task_id: str | None = None,
        user_id: int | None = None,
        conversation_id: int | None = None,
        permission_mode: str = "workspace-write",
        project_trusted: bool = False,
        resume_state: dict | None = None,
    ) -> AsyncIterator[dict]:
        normalized = mode.lower().strip()
        if normalized == "coding-agent":
            async for event in self.coding_agent.stream(
                message=message, history=history, system_context=system_context,
                workspace_dir=workspace_dir, task_id=task_id, user_id=user_id,
                conversation_id=conversation_id, permission_mode=permission_mode,
                project_trusted=project_trusted, resume_state=resume_state,
            ):
                yield event
            return
        if normalized != "chatbot":
            raise ValueError(f"Unsupported mode: {mode}")
        async for event in self.chatbot.stream_rag(
            message=message, history=history, use_rag=use_rag, use_web=use_web,
            system_context=system_context, task_id=task_id, user_id=user_id,
            conversation_id=conversation_id, resume_state=resume_state,
        ):
            yield event
