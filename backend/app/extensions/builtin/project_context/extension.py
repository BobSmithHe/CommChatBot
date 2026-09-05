from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class ProjectContextExtension:
    manifest = ExtensionManifest(
        id="coding.project-context", modes=frozenset({"coding"}),
        description="Trusted AGENTS, declarative project extensions, and Skills.",
        permissions=frozenset({"read", "write", "execute", "network"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        bundle = context.require("project_context").load(
            context.require("workspace"), trusted=bool(context.request.get("project_trusted")),
        )
        sections: list[str] = []
        if bundle.text:
            sections.append("Trusted project context:\n" + bundle.text)
        elif bundle.discovered:
            sections.append(
                "Project-local instructions, skills, or extensions were discovered but are disabled "
                "until the user explicitly trusts this project."
            )
        return ExtensionContribution(
            tools=bundle.tools, prompt_sections=sections,
            values={"project_context_discovered": bundle.discovered},
        )
