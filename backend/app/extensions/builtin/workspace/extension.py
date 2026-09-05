from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class WorkspaceExtension:
    manifest = ExtensionManifest(
        id="coding.workspace", modes=frozenset({"coding"}),
        description="Workspace file discovery, reading, writing, and patch tools.",
        permissions=frozenset({"read", "write"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        return ExtensionContribution(tools=context.require("tool_registry").workspace_tools(
            context.require("workspace"),
        ))
