from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.extensions.support.execution import CodeExecutor
from app.extensions.builtin.rag import LocalRagStore
from app.main import app
from app.infra.security import create_access_token, decode_access_token
from app.infra.config import Settings
from fastapi.testclient import TestClient


def test_app_imports():
    assert app.title == "CommChatBot"


def test_security_and_request_correlation_headers():
    client = TestClient(app)
    accepted = client.get("/health", headers={"X-Request-ID": "trace-123"})
    ready = client.get("/health/ready")
    rejected = client.get("/health", headers={"X-Request-ID": "bad id"})
    auth = client.post("/api/auth/login", json={})
    assert accepted.headers["x-request-id"] == "trace-123"
    assert accepted.headers["x-content-type-options"] == "nosniff"
    assert accepted.headers["x-frame-options"] == "DENY"
    assert accepted.headers["referrer-policy"] == "no-referrer"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert rejected.headers["x-request-id"] != "bad id"
    assert auth.headers["cache-control"] == "no-store"
    assert auth.headers["pragma"] == "no-cache"


def test_access_token_round_trip():
    assert decode_access_token(create_access_token("42")) == "42"


def test_production_configuration_rejects_insecure_auth_settings():
    settings = Settings(
        app_environment="production",
        jwt_secret_key="change-me",
        refresh_cookie_secure=False,
        password_reset_debug=True,
        cors_origins="*",
    )
    try:
        settings.validate_deployment()
    except RuntimeError as exc:
        detail = str(exc)
        assert "JWT_SECRET_KEY" in detail
        assert "REFRESH_COOKIE_SECURE" in detail
        assert "PASSWORD_RESET_DEBUG" in detail
        assert "CORS_ORIGINS" in detail
    else:
        raise AssertionError("unsafe production settings were accepted")


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
