from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class WebSearchExtension:
    manifest = ExtensionManifest(
        id="chat.web-search", modes=frozenset({"chatbot"}), description="Optional web search context.",
        permissions=frozenset({"network"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        request = context.request
        if not request.get("use_web") or request.get("resume_state"):
            return ExtensionContribution(values={"web_context": ""})
        message = str(request.get("message") or "")
        web_context = await context.require("tool_registry").search_web(message)
        return ExtensionContribution(
            events=[
                {"event": "status", "content": f"Searching the web: {message}"},
                {"event": "result", "content": web_context[:1200]},
            ], values={"web_context": web_context},
        )
