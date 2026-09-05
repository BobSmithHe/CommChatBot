from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.rag import LocalRagStore, route_rag_query
from app.packages.ai import ModelResponse
from app.products import ChatbotMode


class FakeProvider:
    async def complete(self, messages, tools, model, *, system=None):
        return ModelResponse(content="你好！有什么可以帮你的？")


class SpyRag:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(self, query: str, top_k: int = 5):
        self.queries.append(query)
        return []


class SlowRag:
    async def search(self, query: str, top_k: int = 5):
        await asyncio.sleep(1)
        return []


class FakeMilvus:
    def __init__(self, score: float) -> None:
        self.score = score

    def search(self, query: str, limit: int):
        return [{
            "content": "OFDM uses orthogonal subcarriers.",
            "score": self.score,
            "source": "ofdm.md",
            "title": "OFDM",
            "chunk_id": "chunk-1",
        }]


def test_intent_router_skips_greetings_and_meta_questions() -> None:
    for message in ("你好", "您好！", "谢谢", "你是谁？", "帮我写一首诗"):
        assert route_rag_query(message).should_retrieve is False


def test_intent_router_skips_tracebacks_and_code_error_analysis() -> None:
    traceback = """Traceback (most recent call last):
  File \"/workspace/example.py\", line 81, in <module>
    result = samples / channel
ValueError: operands could not be broadcast together with shapes (48000,) (48,)"""
    assert route_rag_query(traceback).should_retrieve is False
    assert route_rag_query("这段 Python 代码报错了，怎么修复？").should_retrieve is False
    assert route_rag_query("根据知识库分析这个 ValueError").should_retrieve is True


def test_intent_router_keeps_real_questions_and_rewrites_followups() -> None:
    assert route_rag_query("你好，请问 OFDM 是什么？").should_retrieve is True
    decision = route_rag_query(
        "它有什么优点？",
        [{"role": "user", "content": "OFDM 的基本原理是什么？"}],
    )
    assert decision.should_retrieve is True
    assert "OFDM 的基本原理" in decision.query


def test_chatbot_does_not_search_rag_for_greeting() -> None:
    rag = SpyRag()
    chatbot = ChatbotMode(
        provider=FakeProvider(),
        rag=rag,
        tools=SimpleNamespace(search_web=None),
    )

    async def collect():
        return [event async for event in chatbot.stream_rag(
            message="你好",
            history=[],
            use_rag=True,
            use_web=False,
        )]

    events = asyncio.run(collect())
    assert rag.queries == []
    assert any("Skipped local knowledge" in str(event.get("content")) for event in events)


def test_chatbot_rag_timeout_falls_back_to_model_answer() -> None:
    chatbot = ChatbotMode(
        provider=FakeProvider(),
        rag=SlowRag(),
        tools=SimpleNamespace(search_web=None),
    )
    chatbot.settings = SimpleNamespace(
        rag_intent_routing=True,
        rag_search_timeout_seconds=0.01,
        chat_model_id="",
        deepseek_model="fake-model",
    )

    async def collect():
        return [event async for event in chatbot.stream_rag(
            message="OFDM 的基本原理是什么？",
            history=[],
            use_rag=True,
            use_web=False,
        )]

    events = asyncio.run(collect())
    assert any("timed out" in str(event.get("content")) for event in events)
    assert any(event.get("event") == "answer" for event in events)


def test_low_similarity_milvus_result_is_rejected_and_score_is_preserved(tmp_path) -> None:
    store = LocalRagStore(str(tmp_path / "index.json"))
    store._loaded = True
    store._milvus = FakeMilvus(0.12)
    assert asyncio.run(store.search("你好", top_k=5)) == []

    store._milvus = FakeMilvus(0.81)
    results = asyncio.run(store.search("OFDM 是什么", top_k=5))
    assert len(results) == 1
    assert results[0].score == 0.81
