from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.memory import MemoryService
from app.infra.database import Base
from app.packages.ai import ModelResponse


class FakeIndex:
    def __init__(self) -> None:
        self.rows = {}
        self.semantic = {}

    def upsert(self, payload):
        self.rows[payload["id"]] = payload
        return True

    def delete(self, memory_id):
        self.rows.pop(memory_id, None)
        return True

    def search(self, _query, _user_id, _limit):
        return dict(self.semantic)


class ExtractProvider:
    async def complete(self, _messages, _tools, _model, *, system=None):
        return ModelResponse(
            json.dumps({
                "memories": [{
                    "scope": "user",
                    "key": "preferred language",
                    "value": "Reply in Chinese",
                    "category": "preference",
                    "importance": 0.9,
                    "confidence": 0.95,
                    "ttl_days": 0,
                }],
            }),
            metadata={"usage": {"input_tokens": 12, "output_tokens": 8}},
        )


@pytest.fixture
def service(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr("app.core.memory.SessionLocal", session)
    result = MemoryService()
    result.index = FakeIndex()
    return result


def test_scoped_memory_upsert_revision_recall_and_delete(service) -> None:
    target = service.target("project", user_id=7, target_value="git:https://example.test/repo")
    first = service.upsert(
        user_id=7,
        target=target,
        key="test command",
        value="pytest -q",
        category="workflow",
        importance=0.9,
        confidence=1.0,
    )
    updated = service.upsert(
        user_id=7,
        target=target,
        key="test command",
        value="pytest tests -q",
        category="workflow",
        importance=0.9,
        confidence=1.0,
    )

    assert first["id"] == updated["id"]
    assert updated["revision"] == 2
    assert updated["history"][0]["value"] == "pytest -q"
    service.index.semantic[updated["id"]] = 0.92
    recalled = service.recall(
        user_id=7,
        query="how should tests run",
        project_identity="git:https://example.test/repo",
    )
    assert recalled[0]["key"] == "test command"
    assert recalled[0]["access_count"] == 1
    assert service.delete(7, updated["id"]) is True
    assert service.list(7) == []
    assert service.list(7, include_inactive=True) == []
    assert service.get(7, updated["id"]) is None


def test_clear_is_a_privacy_hard_delete(service) -> None:
    user_target = service.target("user", user_id=8)
    conversation_target = service.target("conversation", user_id=8, target_value=3)
    service.upsert(user_id=8, target=user_target, key="language", value="Chinese")
    service.upsert(user_id=8, target=conversation_target, key="topic", value="OFDM")

    assert service.clear(8, "conversation") == 1
    remaining = service.list(8, include_inactive=True)
    assert [item["key"] for item in remaining] == ["language"]
    assert service.clear(8) == 1
    assert service.list(8, include_inactive=True) == []


def test_expired_memory_is_physically_purged(service) -> None:
    target = service.target("user", user_id=12)
    expired = service.upsert(
        user_id=12,
        target=target,
        key="temporary fact",
        value="already expired",
        expires_at=datetime.utcnow() - timedelta(seconds=1),
    )

    assert service.list(12, include_inactive=True) == []
    assert service.get(12, expired["id"]) is None
    assert expired["id"] not in service.index.rows


def test_memory_privacy_settings_and_secret_filter(service) -> None:
    target = service.target("user", user_id=9)
    with pytest.raises(ValueError, match="credential"):
        service.upsert(
            user_id=9,
            target=target,
            key="api key",
            value="sk-abcdefghijklmnopqrstuv",
        )
    service.upsert(user_id=9, target=target, key="language", value="Chinese")
    service.update_settings(9, {"enabled": False})
    assert service.recall(user_id=9, query="language") == []


def test_automatic_extraction_is_scoped_and_records_usage(service) -> None:
    memories, usage = asyncio.run(service.auto_extract(
        provider=ExtractProvider(),
        model="test-model",
        user_id=11,
        user_text="请记住我偏好中文",
        assistant_text="好的。",
        conversation_id=3,
        task_id="a" * 32,
    ))

    assert len(memories) == 1
    assert memories[0]["scope"] == "user"
    assert memories[0]["source_type"] == "auto"
    assert usage == {"input_tokens": 12, "output_tokens": 8}


def test_target_ids_separate_all_four_scopes(service) -> None:
    targets = {
        service.target("user", user_id=1).target_id,
        service.target("project", user_id=1, target_value="repo").target_id,
        service.target("conversation", user_id=1, target_value=2).target_id,
        service.target("task", user_id=1, target_value="b" * 32).target_id,
    }
    assert len(targets) == 4
