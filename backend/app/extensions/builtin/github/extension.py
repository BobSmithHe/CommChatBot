from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class GitHubExtension:
    manifest = ExtensionManifest(
        id="coding.github", modes=frozenset({"coding"}),
        description="GitHub pull request, checks, and review tools.",
        permissions=frozenset({"read", "write", "network"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        return ExtensionContribution(tools=context.require("github").tools(context.require("workspace")))
