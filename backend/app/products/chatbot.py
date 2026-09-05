from __future__ import annotations

from typing import AsyncIterator, Mapping, Any

from ..agent_runtime import AgentRuntime, AgentRuntimeHost, ExtensionContext, ExtensionRegistry
from ..providers import ModelProvider
from .common import history_to_agent_messages, map_agent_event
from .ports import RetrievalPort, RetrievedDocument, ToolCatalogPort
from .profiles import CHAT_PROFILE, ProductRuntimeConfig, apply_profile_overrides


class ChatbotMode:
    """Chat product shell with deterministic optional RAG and web context."""

    def __init__(
        self, *, provider: ModelProvider, rag: RetrievalPort, tools: ToolCatalogPort,
        extensions: ExtensionRegistry, runtime_host: AgentRuntimeHost,
        extension_services: Mapping[str, Any], config: ProductRuntimeConfig,
    ) -> None:
        self.provider = provider
        self.rag = rag
        self.tools = tools
        self.config = config
        self.extensions = extensions
        self.runtime_host = runtime_host
        self.extension_services = dict(extension_services)

    async def stream_rag(
        self,
        *,
        message: str | None,
        history: list[dict],
        use_rag: bool,
        use_web: bool,
        system_context: str | None = None,
        task_id: str | None = None,
        user_id: int | None = None,
        conversation_id: int | None = None,
        resume_state: dict | None = None,
    ) -> AsyncIterator[dict]:
        profile = apply_profile_overrides(CHAT_PROFILE, self.config.chat_extensions)
        activated = await self.extensions.activate(profile, ExtensionContext(
            mode="chatbot",
            services={
                **self.extension_services,
                "rag": self.rag, "tool_registry": self.tools,
            },
            request={
                "message": message, "history": history, "use_rag": use_rag, "use_web": use_web,
                "task_id": task_id, "user_id": user_id, "conversation_id": conversation_id,
                "resume_state": resume_state,
            },
        ))
        for extension_event in activated.events:
            yield extension_event
        docs: list[RetrievedDocument] = activated.values.get("rag_docs", [])
        web_context = str(activated.values.get("web_context") or "")

        prompt = None if resume_state is not None and message is None else self._rag_prompt(message or "", docs, web_context)
        memory_context = str(activated.values.get("memory_context") or "")
        runtime_system = system_context or self._rag_system_prompt()
        if memory_context:
            runtime_system += "\n\n" + memory_context
        runtime = AgentRuntime(
            provider=self.provider,
            model=self.config.chat_model_id or getattr(self.provider, "default_model", "") or self.config.fallback_model_id,
            tools=[],
            system_prompt=runtime_system,
            max_turns=1,
            task_id=task_id,
            permission_mode="read-only",
            hook_handlers=activated.hooks,
            host=self.runtime_host,
            context_window=self.config.context_window,
            max_output_tokens=self.config.max_output_tokens,
            context_compaction=self.config.context_compaction,
            context_compaction_trigger_ratio=self.config.context_compaction_trigger_ratio,
        )
        yield {"event": "status", "content": "Generating answer"}
        try:
            async for event in runtime.run(
                prompt,
                history_to_agent_messages(history),
                resume_state=resume_state,
            ):
                mapped = map_agent_event(event)
                if mapped:
                    yield mapped
        finally:
            await activated.close()

    @staticmethod
    def _rag_prompt(message: str, docs: list[RetrievedDocument], web_context: str) -> str:
        if not docs and not web_context:
            return message
        sections = []
        if docs:
            sections.append("Local knowledge:\n" + "\n\n".join(
                f"[{doc.source}] score={doc.score}\n{doc.content[:1800]}" for doc in docs
            ))
        if web_context:
            sections.append("Web search:\n" + web_context[:5000])
        context = "\n\n".join(sections)
        return f"Question:\n{message}\n\nRetrieved context:\n{context}\n\nAnswer using the context when relevant."

    @staticmethod
    def _rag_system_prompt() -> str:
        return "You are a wireless communications chatbot. Answer in Markdown and cite retrieved source names when useful."
