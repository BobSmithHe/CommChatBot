from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest


class RemoteExecutionExtension:
    manifest = ExtensionManifest(
        id="coding.remote-execution", modes=frozenset({"coding"}),
        description="Durable isolated remote runner tools.", permissions=frozenset({"read", "execute", "network"}),
    )

    async def activate(self, context: ExtensionContext) -> ExtensionContribution:
        return ExtensionContribution(tools=context.require("remote_execution").tools(
            user_id=context.request.get("user_id"), task_id=context.request.get("task_id"),
        ))
