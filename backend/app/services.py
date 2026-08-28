from __future__ import annotations

from functools import lru_cache
from typing import AsyncIterator

from .core.code import CodeExecutor
from .core.attachments import ChatAttachmentStore
from .core.rag import LocalRagStore
from .core.workspace import ConversationWorkspaceManager, WorkspaceEditor
from .packages.ai import OpenAICompatibleProvider
from .products import ChatbotMode, CodingAgentMode
from .products.tool_registry import ProductToolRegistry


class ProductGateway:
    """API-facing product gateway.

    HTTP routes call this layer. It selects a product mode and never talks to
    the low-level model provider or AgentRuntime directly.
    """

    def __init__(self, *, chatbot: ChatbotMode, coding_agent: CodingAgentMode) -> None:
        self.chatbot = chatbot
        self.coding_agent = coding_agent

    async def stream(
        self,
        *,
        message: str,
        history: list[dict],
        mode: str,
        use_rag: bool,
        use_web: bool,
        system_context: str | None = None,
        workspace_dir: str | None = None,
        task_id: str | None = None,
        permission_mode: str = "workspace-write",
        resume_state: dict | None = None,
    ) -> AsyncIterator[dict]:
        normalized = mode.lower().strip()
        if normalized == "coding-agent":
            async for event in self.coding_agent.stream(
                message=message,
                history=history,
                system_context=system_context,
                workspace_dir=workspace_dir,
                task_id=task_id,
                permission_mode=permission_mode,
                resume_state=resume_state,
            ):
                yield event
            return

        if normalized != "chatbot":
            raise ValueError(f"Unsupported mode: {mode}")
        async for event in self.chatbot.stream_rag(
            message=message,
            history=history,
            use_rag=use_rag,
            use_web=use_web,
            system_context=system_context,
            task_id=task_id,
            resume_state=resume_state,
        ):
            yield event


@lru_cache
def get_rag_store() -> LocalRagStore:
    return LocalRagStore()


@lru_cache
def get_code_executor() -> CodeExecutor:
    return CodeExecutor()


@lru_cache
def get_chat_attachment_store() -> ChatAttachmentStore:
    return ChatAttachmentStore()


@lru_cache
def get_workspace_editor() -> WorkspaceEditor:
    return WorkspaceEditor()


@lru_cache
def get_conversation_workspace_manager() -> ConversationWorkspaceManager:
    return ConversationWorkspaceManager()


@lru_cache
def get_model_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider()


@lru_cache
def get_product_tool_registry() -> ProductToolRegistry:
    return ProductToolRegistry(get_rag_store(), get_code_executor(), get_workspace_editor())


@lru_cache
def get_chatbot_mode() -> ChatbotMode:
    return ChatbotMode(
        provider=get_model_provider(),
        rag=get_rag_store(),
        tools=get_product_tool_registry(),
    )


@lru_cache
def get_coding_agent_mode() -> CodingAgentMode:
    return CodingAgentMode(provider=get_model_provider(), tools=get_product_tool_registry())


@lru_cache
def get_product_gateway() -> ProductGateway:
    return ProductGateway(chatbot=get_chatbot_mode(), coding_agent=get_coding_agent_mode())


def get_chat_orchestrator() -> ProductGateway:
    """Backward-compatible name used by existing route imports."""
    return get_product_gateway()
