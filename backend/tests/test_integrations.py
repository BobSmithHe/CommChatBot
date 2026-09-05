from app.infra.integrations import ExternalIntegrations
import httpx


def test_local_integrations_bypass_environment_proxy() -> None:
    assert ExternalIntegrations._trust_env_for_url("http://localhost:3000") is False
    assert ExternalIntegrations._trust_env_for_url("http://127.0.0.1:3000") is False
    assert ExternalIntegrations._trust_env_for_url("http://[::1]:3000") is False


def test_remote_integrations_keep_environment_proxy() -> None:
    assert ExternalIntegrations._trust_env_for_url("https://cloud.langfuse.com") is True


def test_http_health_retries_transient_local_timeout(monkeypatch) -> None:
    attempts = []

    def fake_get(self, url):
        attempts.append(url)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("warming up")
        return httpx.Response(200)

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    assert ExternalIntegrations._http_health("http://localhost:3000/api/public/health") == "ok"
    assert len(attempts) == 2
