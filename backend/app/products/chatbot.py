from __future__ import annotations

import asyncio
from typing import AsyncIterator

from ..core.rag import LocalRagStore, RetrievedChunk, route_rag_query
from ..core.memory import memory_service
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
        docs: list[RetrievedChunk] = []
        if use_rag and not resume_state:
            decision = route_rag_query(message or "", history) if self.settings.rag_intent_routing else None
            if decision is None or decision.should_retrieve:
                retrieval_query = decision.query if decision else (message or "")
                yield {"event": "status", "content": f"Searching local knowledge: {retrieval_query}"}
                try:
                    docs = await asyncio.wait_for(
                        self.rag.search(retrieval_query, top_k=5),
                        timeout=max(0.1, float(self.settings.rag_search_timeout_seconds)),
                    )
                except asyncio.TimeoutError:
                    yield {
                        "event": "status",
                        "content": "Local knowledge search timed out; continuing without RAG.",
                    }
                    docs = []
                except Exception as exc:
                    yield {
                        "event": "status",
                        "content": f"Local knowledge search unavailable ({type(exc).__name__}); continuing without RAG.",
                    }
                    docs = []
                yield {"event": "result", "content": self._summarize_docs(docs) if docs else "No sufficiently relevant local knowledge matched."}
                if docs:
                    yield {
                        "event": "sources",
                        "content": [
                            {"source": doc.source, "title": doc.title, "chunk_id": doc.chunk_id, "score": doc.score}
                            for doc in docs
                        ],
                    }
            else:
                yield {"event": "status", "content": f"Skipped local knowledge: {decision.reason}"}

        web_context = ""
        if use_web and not resume_state:
            yield {"event": "status", "content": f"Searching the web: {message or ''}"}
            web_context = await self.tools.search_web(message or "")
            yield {"event": "result", "content": web_context[:1200]}

        prompt = None if resume_state is not None and message is None else self._rag_prompt(message or "", docs, web_context)
        memory_context = ""
        if user_id is not None:
            recall_query = message or next(
                (str(item.get("content") or "") for item in reversed(history) if item.get("role") == "user"),
                "",
            )
            memories = await asyncio.to_thread(
                memory_service.recall,
                user_id=user_id,
                query=recall_query,
                conversation_id=conversation_id,
                task_id=task_id,
            )
            memory_context = memory_service.format_for_prompt(memories)
            if memories:
                yield {
                    "event": "memory_recalled",
                    "content": {
                        "count": len(memories),
                        "memories": [
                            {"id": item["id"], "scope": item["scope"], "key": item["key"]}
                            for item in memories
                        ],
                    },
                }
        runtime_system = system_context or self._rag_system_prompt()
        if memory_context:
            runtime_system += "\n\n" + memory_context
        runtime = AgentRuntime(
            provider=self.provider,
            model=self.settings.chat_model_id or getattr(self.provider, "default_model", "") or self.settings.deepseek_model,
            tools=[],
            system_prompt=runtime_system,
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
