from __future__ import annotations

from functools import lru_cache
from typing import AsyncIterator

from .core.code import CodeExecutor
from .core.attachments import ChatAttachmentStore
from .core.rag import LocalRagStore
from .core.workspace import ConversationWorkspaceManager, WorkspaceEditor
from .infra.config import get_settings
from .packages.ai import ModelProvider, ProviderRegistry
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
                message=message,
                history=history,
                system_context=system_context,
                workspace_dir=workspace_dir,
                task_id=task_id,
                user_id=user_id,
                conversation_id=conversation_id,
                permission_mode=permission_mode,
                project_trusted=project_trusted,
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
            user_id=user_id,
            conversation_id=conversation_id,
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
def get_model_provider() -> ModelProvider:
    return get_provider_registry().create(get_settings().coding_provider_id)


@lru_cache
def get_provider_registry() -> ProviderRegistry:
    return ProviderRegistry()


@lru_cache
def get_chat_model_provider() -> ModelProvider:
    return get_provider_registry().create(get_settings().chat_provider_id)


@lru_cache
def get_coding_model_provider() -> ModelProvider:
    return get_provider_registry().create(get_settings().coding_provider_id)


@lru_cache
def get_product_tool_registry() -> ProductToolRegistry:
    return ProductToolRegistry(get_rag_store(), get_code_executor(), get_workspace_editor())


@lru_cache
def get_chatbot_mode() -> ChatbotMode:
    return ChatbotMode(
        provider=get_chat_model_provider(),
        rag=get_rag_store(),
        tools=get_product_tool_registry(),
    )


@lru_cache
def get_coding_agent_mode() -> CodingAgentMode:
    return CodingAgentMode(provider=get_coding_model_provider(), tools=get_product_tool_registry())


@lru_cache
def get_product_gateway() -> ProductGateway:
    return ProductGateway(chatbot=get_chatbot_mode(), coding_agent=get_coding_agent_mode())


def get_chat_orchestrator() -> ProductGateway:
    """Backward-compatible name used by existing route imports."""
    return get_product_gateway()
