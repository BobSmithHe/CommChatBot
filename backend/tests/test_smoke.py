from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.code import CodeExecutor
from app.core.rag import LocalRagStore
from app.main import app
from app.infra.security import create_access_token, decode_access_token


def test_app_imports():
    assert app.title == "CommChatBot"


def test_access_token_round_trip():
    assert decode_access_token(create_access_token("42")) == "42"


def test_code_executor_runs_python():
    result = asyncio.run(CodeExecutor(timeout=5).execute("print(1 + 2)"))
    assert result["exit_code"] == 0
    assert result["stdout"] == "3"


def test_local_rag_search(tmp_path):
    store = LocalRagStore(str(tmp_path / "index.json"))
    store.add_document("OFDM uses orthogonal subcarriers. Subcarrier spacing can be 15 kHz.", "ofdm.md")
    results = asyncio.run(store.search("OFDM subcarrier spacing", top_k=2))
    assert results
    assert results[0].source == "ofdm.md"
