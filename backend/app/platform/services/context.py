from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ...infra.config import get_settings
from ..database import Conversation, Message
from ...providers import LLMMessage, ModelProvider
from .lazy import LazyService


def estimate_tokens(value: Any) -> int:
    """Fast, dependency-free upper estimate suitable for enforcing a budget."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", value))
    other = max(0, len(value) - cjk)
    return max(1, cjk + (other + 3) // 4)


def truncate_to_tokens(value: Any, max_tokens: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if estimate_tokens(text) <= max_tokens:
        return text
    if max_tokens <= 1:
        return "…"
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_tokens(text[:middle] + "…") <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


class ConversationContextManager:
    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()

    def prepare(self, db: Session, conversation_id: int) -> list[dict]:
        conversation = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if not conversation:
            return []
        query = db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.role.in_(("user", "assistant", "system")),
        )
        if conversation.summary_until_message_id:
            query = query.filter(Message.id > conversation.summary_until_message_id)
        rows = query.order_by(Message.id.asc()).all()
        input_budget = max(2048, self.settings.model_context_tokens - self.settings.model_max_tokens - 1024)
        summary = conversation.context_summary or ""
        total = estimate_tokens(summary) + sum(estimate_tokens(row.content) + 8 for row in rows)
        trigger_count = max(self.settings.context_recent_messages * 2, 24)

        if total > int(input_budget * 0.72) or len(rows) > trigger_count:
            keep_count = max(2, self.settings.context_recent_messages)
            older, recent = rows[:-keep_count], rows[-keep_count:]
            if older:
                summary = self._merge_summary(summary, older)
                conversation.context_summary = summary
                conversation.summary_until_message_id = older[-1].id
                db.commit()
                rows = recent

        result: list[dict] = []
        if summary:
            result.append({
                "role": "system",
                "content": "Earlier conversation summary (preserve facts and decisions):\n" + summary,
            })
        result.extend({"role": row.role, "content": row.content} for row in rows)
        return result

    def _merge_summary(self, previous: str, rows: list[Message]) -> str:
        parts = []
        if previous:
            parts.append("Previous summary:\n" + previous)
        labels = {"user": "User", "assistant": "Assistant", "system": "System"}
        for row in rows:
            content = re.sub(r"\s+", " ", row.content).strip()
            parts.append(f"{labels.get(row.role, row.role)}: {truncate_to_tokens(content, 240)}")
        return truncate_to_tokens("\n".join(parts), self.settings.context_summary_tokens)


def fit_llm_messages(messages: list[LLMMessage], max_tokens: int) -> list[LLMMessage]:
    """Keep a possible summary plus the newest contiguous model/tool exchange."""
    if not messages:
        return []
    budget = max(256, max_tokens)
    if sum(estimate_tokens(item.content) + 8 for item in messages) <= budget:
        return messages

    summary = messages[0] if messages[0].role == "system" else None
    summary_cost = estimate_tokens(summary.content) + 8 if summary else 0
    if summary_cost > budget // 3 and summary:
        summary = LLMMessage("system", truncate_to_tokens(summary.content, budget // 3))
        summary_cost = estimate_tokens(summary.content) + 8

    remaining = budget - summary_cost
    kept: list[LLMMessage] = []
    start = 1 if messages[0].role == "system" else 0
    for item in reversed(messages[start:]):
        cost = estimate_tokens(item.content) + 8
        if cost > remaining and not kept:
            kept.append(LLMMessage(item.role, truncate_to_tokens(item.content, max(64, remaining - 8))))
            remaining = 0
            break
        if cost > remaining:
            break
        kept.append(item)
        remaining -= cost
    kept.reverse()
    return ([summary] if summary else []) + kept


@dataclass
class ContextCompactionResult:
    messages: list[LLMMessage]
    compacted: bool = False
    tokens_before: int = 0
    tokens_after: int = 0
    summary: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


async def compact_llm_messages(
    messages: list[LLMMessage],
    *,
    provider: ModelProvider,
    model: str,
    max_tokens: int,
) -> ContextCompactionResult:
    """Use the active LLM for structured compaction, with deterministic fallback."""
    settings = get_settings()
    tokens_before = sum(estimate_tokens(item.content) + 8 for item in messages)
    trigger = int(max_tokens * max(0.5, min(settings.context_compaction_trigger_ratio, 0.95)))
    if not settings.llm_context_compaction or tokens_before <= trigger:
        return ContextCompactionResult(messages, False, tokens_before, tokens_before)

    keep_budget = max(512, max_tokens // 2)
    used = 0
    start = len(messages)
    for index in range(len(messages) - 1, -1, -1):
        cost = estimate_tokens(messages[index].content) + 8
        if used + cost > keep_budget and start < len(messages):
            break
        used += cost
        start = index
    while start > 0 and messages[start].role == "tool":
        start -= 1
    older, recent = messages[:start], messages[start:]
    if not older:
        fitted = fit_llm_messages(messages, max_tokens)
        return ContextCompactionResult(
            fitted,
            True,
            tokens_before,
            sum(estimate_tokens(item.content) + 8 for item in fitted),
            summary="[deterministic truncation]",
        )

    serialized = []
    for item in older:
        content = truncate_to_tokens(item.content, 1_200)
        serialized.append(f"[{item.role}]\n{content}")
    request = (
        "Summarize the following earlier agent context. Return concise Markdown with exactly these sections:\n"
        "## Goal\n## Constraints & Preferences\n## Progress\n## Errors\n## Key Decisions\n"
        "## Next Steps\n## Read Files\n## Modified Files\n"
        "Preserve concrete paths, commands, errors, user decisions, pending work, and tool outcomes.\n\n"
        + "\n\n".join(serialized)
    )
    try:
        response = await provider.complete(
            [LLMMessage("user", request)],
            [],
            model,
            system="You compact agent history without continuing the task or inventing facts.",
        )
        summary = str(response.content or "").strip()
        if not summary:
            raise RuntimeError("Compaction model returned an empty summary")
        compacted = [LLMMessage("system", "Earlier structured context summary:\n" + summary), *recent]
        if sum(estimate_tokens(item.content) + 8 for item in compacted) > max_tokens:
            compacted = fit_llm_messages(compacted, max_tokens)
        return ContextCompactionResult(
            compacted,
            True,
            tokens_before,
            sum(estimate_tokens(item.content) + 8 for item in compacted),
            summary=summary,
            usage=dict(response.metadata.get("usage") or {}),
        )
    except Exception:
        fitted = fit_llm_messages(messages, max_tokens)
        return ContextCompactionResult(
            fitted,
            True,
            tokens_before,
            sum(estimate_tokens(item.content) + 8 for item in fitted),
            summary="[deterministic fallback]",
        )


context_manager = LazyService(ConversationContextManager)
