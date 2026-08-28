from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.code import CodeExecutor
from app.core.rag import LocalRagStore
from app.core.workspace import WorkspaceEditor
from app.packages.ai import ModelResponse
from app.products import ChatbotMode, CodingAgentMode
from app.products.tool_registry import ProductToolRegistry
from app.services import ProductGateway


class FakeProvider:
    async def complete(self, messages, tools, model, *, system=None):
        return ModelResponse(content="Test response")


def test_gateway_routes_chatbot_mode(tmp_path):
    events = asyncio.run(_collect(_gateway(tmp_path), mode="chatbot"))
    assert any(event["event"] == "status" and "Generating answer" in event["content"] for event in events)
    assert any(event["event"] == "answer" for event in events)


def test_gateway_routes_coding_agent_mode(tmp_path):
    events = asyncio.run(_collect(_gateway(tmp_path), mode="coding-agent"))
    assert any(event["event"] == "status" and "CodingAgent AgentRuntime started" in event["content"] for event in events)
    assert any(event["event"] == "answer" for event in events)


def _gateway(tmp_path: Path) -> ProductGateway:
    provider = FakeProvider()
    rag = LocalRagStore(str(tmp_path / "index.json"))
    tools = ProductToolRegistry(rag, CodeExecutor(timeout=2), WorkspaceEditor(tmp_path))
    return ProductGateway(
        chatbot=ChatbotMode(provider=provider, rag=rag, tools=tools),
        coding_agent=CodingAgentMode(provider=provider, tools=tools),
    )


async def _collect(gateway: ProductGateway, mode: str) -> list[dict]:
    events = []
    async for event in gateway.stream(
        message="What is OFDM?",
        history=[],
        mode=mode,
        use_rag=False,
        use_web=False,
    ):
        events.append(event)
    return events
