import asyncio

from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class RagExtension:
    manifest = ExtensionManifest(
        id="chat.rag", modes=frozenset({"chatbot"}),
        description="Intent-routed local knowledge retrieval.", permissions=frozenset({"read"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        request = context.request
        if not request.get("use_rag") or request.get("resume_state"):
            return ExtensionContribution(values={"rag_docs": []})
        message = str(request.get("message") or "")
        history = list(request.get("history") or [])
        settings = context.require("settings")
        router = context.require("rag_intent_router")
        decision = router(message, history) if settings.rag_intent_routing else None
        if decision is not None and not decision.should_retrieve:
            return ExtensionContribution(
                events=[{"event": "status", "content": f"Skipped local knowledge: {decision.reason}"}],
                values={"rag_docs": []},
            )
        query = decision.query if decision else message
        events = [{"event": "status", "content": f"Searching local knowledge: {query}"}]
        try:
            docs = await asyncio.wait_for(
                context.require("rag").search(query, top_k=5),
                timeout=max(0.1, float(settings.rag_search_timeout_seconds)),
            )
        except asyncio.TimeoutError:
            docs = []
            events.append({"event": "status", "content": "Local knowledge search timed out; continuing without RAG."})
        except Exception as exc:
            docs = []
            events.append({
                "event": "status",
                "content": f"Local knowledge search unavailable ({type(exc).__name__}); continuing without RAG.",
            })
        events.append({
            "event": "result",
            "content": (
                "Found " + str(len(docs)) + " chunks: "
                + " | ".join(f"{doc.source} ({doc.score})" for doc in docs)
                if docs else "No sufficiently relevant local knowledge matched."
            ),
        })
        if docs:
            events.append({"event": "sources", "content": [
                {"source": doc.source, "title": doc.title, "chunk_id": doc.chunk_id, "score": doc.score}
                for doc in docs
            ]})
        return ExtensionContribution(events=events, values={"rag_docs": docs})
