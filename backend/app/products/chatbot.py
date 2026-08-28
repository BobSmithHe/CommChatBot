from __future__ import annotations

from typing import AsyncIterator

from ..core.rag import LocalRagStore, RetrievedChunk
from ..infra.config import get_settings
from ..packages.agent import AgentRuntime
from ..packages.ai import ModelProvider
from .common import history_to_agent_messages, map_agent_event
from .tool_registry import ProductToolRegistry


class ChatbotMode:
    """Chat product shell with deterministic optional RAG and web context."""

    def __init__(self, *, provider: ModelProvider, rag: LocalRagStore, tools: ProductToolRegistry) -> None:
        self.provider = provider
        self.rag = rag
        self.tools = tools
        self.settings = get_settings()

    async def stream_rag(
        self,
        *,
        message: str,
        history: list[dict],
        use_rag: bool,
        use_web: bool,
        system_context: str | None = None,
        task_id: str | None = None,
        resume_state: dict | None = None,
    ) -> AsyncIterator[dict]:
        docs: list[RetrievedChunk] = []
        if use_rag and not resume_state:
            yield {"event": "status", "content": f"Searching local knowledge: {message}"}
            docs = await self.rag.search(message, top_k=5)
            yield {"event": "result", "content": self._summarize_docs(docs) if docs else "No local knowledge matched."}
            if docs:
                yield {
                    "event": "sources",
                    "content": [
                        {"source": doc.source, "title": doc.title, "chunk_id": doc.chunk_id, "score": doc.score}
                        for doc in docs
                    ],
                }

        web_context = ""
        if use_web and not resume_state:
            yield {"event": "status", "content": f"Searching the web: {message}"}
            web_context = await self.tools.search_web(message)
            yield {"event": "result", "content": web_context[:1200]}

        prompt = self._rag_prompt(message, docs, web_context)
        runtime = AgentRuntime(
            provider=self.provider,
            model=self.settings.chat_model_id or self.settings.deepseek_model,
            tools=[],
            system_prompt=system_context or self._rag_system_prompt(),
            max_turns=1,
            task_id=task_id,
            permission_mode="read-only",
        )
        yield {"event": "status", "content": "Generating answer"}
        async for event in runtime.run(
            prompt,
            history_to_agent_messages(history),
            resume_state=resume_state,
        ):
            mapped = map_agent_event(event)
            if mapped:
                yield mapped

    @staticmethod
    def _rag_prompt(message: str, docs: list[RetrievedChunk], web_context: str) -> str:
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
    def _summarize_docs(docs: list[RetrievedChunk]) -> str:
        return "Found " + str(len(docs)) + " chunks: " + " | ".join(f"{doc.source} ({doc.score})" for doc in docs)

    @staticmethod
    def _rag_system_prompt() -> str:
        return "You are a wireless communications chatbot. Answer in Markdown and cite retrieved source names when useful."
