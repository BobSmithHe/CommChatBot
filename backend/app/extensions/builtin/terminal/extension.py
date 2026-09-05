from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class TerminalExtension:
    manifest = ExtensionManifest(
        id="coding.terminal", modes=frozenset({"coding"}), requires=("coding.workspace",),
        description="Sandboxed one-shot command and Python execution tools.", permissions=frozenset({"execute"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        return ExtensionContribution(tools=context.require("tool_registry").terminal_tools(
            context.require("workspace"),
        ))
