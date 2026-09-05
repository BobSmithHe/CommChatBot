from dataclasses import dataclass

from ..agent_runtime import AgentProfile


@dataclass(frozen=True)
class ProductRuntimeConfig:
    """Configuration values consumed by product shells.

    Infrastructure settings are translated into this immutable value by the
    bootstrap container, so products never read environment variables.
    """

    chat_model_id: str
    coding_model_id: str
    fallback_model_id: str
    max_agent_turns: int = 8
    context_window: int = 128_000
    max_output_tokens: int = 8_192
    context_compaction: bool = True
    context_compaction_trigger_ratio: float = 0.72
    chat_extensions: str = ""
    coding_extensions: str = ""


CHAT_PROFILE = AgentProfile(
    id="chatbot",
    extensions=("chat.rag", "chat.web-search", "chat.memory"),
)

CODING_PROFILE = AgentProfile(
    id="coding",
    extensions=(
        "coding.workspace",
        "coding.diagnostics",
        "coding.git",
        "coding.terminal",
        "coding.project-context",
        "coding.mcp",
        "coding.github",
        "coding.remote-execution",
        "coding.plan",
        "coding.memory",
        "coding.subagents",
    ),
)


def apply_profile_overrides(profile: AgentProfile, configured: str | list[str] | tuple[str, ...]) -> AgentProfile:
    """Apply pi-style ordered +extension/-extension profile overrides."""
    if isinstance(configured, str):
        entries = [item.strip() for item in configured.split(",") if item.strip()]
    else:
        entries = [str(item).strip() for item in configured if str(item).strip()]
    if not entries:
        return profile
    if not all(item.startswith(("+", "-")) for item in entries):
        return AgentProfile(profile.id, tuple(dict.fromkeys(item.lstrip("+") for item in entries)))
    selected = list(profile.extensions)
    for entry in entries:
        extension_id = entry[1:]
        if entry.startswith("-"):
            selected = [item for item in selected if item != extension_id]
        elif extension_id not in selected:
            selected.append(extension_id)
    return AgentProfile(profile.id, tuple(selected))
