from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class MCPExtension:
    manifest = ExtensionManifest(
        id="coding.mcp", modes=frozenset({"coding"}),
        description="MCP tools, resources, and prompts.",
        permissions=frozenset({"read", "write", "execute", "network"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        tools, errors = await context.require("mcp").load_tools(
            context.require("workspace"), trusted=bool(context.request.get("project_trusted")),
        )
        return ExtensionContribution(
            tools=tools,
            events=[{"event": "mcp_discovery", "content": {
                "tools": [tool.name for tool in tools], "errors": errors,
            }}],
        )
