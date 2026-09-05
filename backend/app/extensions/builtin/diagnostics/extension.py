from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class DiagnosticsExtension:
    manifest = ExtensionManifest(
        id="coding.diagnostics", modes=frozenset({"coding"}), requires=("coding.workspace",),
        description="LSP/static diagnostics feedback for agent edits.", permissions=frozenset({"read"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        return ExtensionContribution(tools=context.require("tool_registry").diagnostics_tools(
            context.require("workspace"),
        ))
