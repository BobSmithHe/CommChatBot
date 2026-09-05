import json

from app.agent_runtime import ExtensionContext, ExtensionContribution, ExtensionManifest, Tool


class PlanExtension:
    manifest = ExtensionManifest(
        id="coding.plan", modes=frozenset({"coding"}), description="Structured task planning.",
        permissions=frozenset({"read"}),
    )

    async def activate(self, _context: ExtensionContext) -> ExtensionContribution:
        plan_state: list[dict] = []

        def update_plan(steps: list[dict], explanation: str = "") -> str:
            normalized: list[dict] = []
            for index, step in enumerate(steps[:20], start=1):
                description = str(step.get("description") or "").strip()
                status = str(step.get("status") or "pending").strip().casefold()
                if not description:
                    raise ValueError(f"Plan step {index} needs a description")
                if status not in {"pending", "in_progress", "completed"}:
                    raise ValueError(f"Invalid plan status: {status}")
                normalized.append({"description": description[:500], "status": status})
            if not normalized:
                raise ValueError("Plan must contain at least one step")
            if sum(item["status"] == "in_progress" for item in normalized) > 1:
                raise ValueError("Only one plan step may be in progress")
            plan_state[:] = normalized
            return json.dumps({"explanation": explanation, "steps": plan_state}, ensure_ascii=False)

        return ExtensionContribution(tools=[Tool(
            name="update_plan",
            description="Create or update the task plan. Use for multi-step work and update statuses after milestones.",
            input_schema={
                "type": "object", "properties": {
                    "explanation": {"type": "string"},
                    "steps": {"type": "array", "items": {
                        "type": "object", "properties": {
                            "description": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        }, "required": ["description", "status"],
                    }},
                }, "required": ["steps"],
            }, handler=update_plan,
        )])
