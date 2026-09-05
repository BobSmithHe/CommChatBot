from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class GitExtension:
    manifest = ExtensionManifest(
        id="coding.git", modes=frozenset({"coding"}), requires=("coding.workspace",),
        description="Workspace diff and Git review feedback.", permissions=frozenset({"read"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        return ExtensionContribution(tools=context.require("tool_registry").git_tools(
            context.require("workspace"),
        ))
