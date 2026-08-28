from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import httpx

from .config import get_settings


class ExternalIntegrations:
    """Optional infrastructure adapters; failures never break local operation."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._redis = None
        self._langfuse = None

    def cache_task(self, task_id: str, payload: dict[str, Any]) -> None:
        client = self._redis_client()
        if not client:
            return
        try:
            client.setex(f"commchat:task:{task_id}", 86400, json.dumps(payload, ensure_ascii=False, default=str))
        except Exception:
            self._redis = False

    def audit_task(
        self,
        *,
        task_id: str,
        status: str,
        conversation_id: int | None = None,
        mode: str | None = None,
        error: str | None = None,
    ) -> None:
        """Mirror the task lifecycle into the configured MySQL audit table."""
        try:
            import pymysql

            connection = pymysql.connect(
                host=self.settings.db_host,
                port=self.settings.db_port,
                user=self.settings.db_user,
                password=self.settings.db_password,
                database=self.settings.db_name,
                connect_timeout=1,
                read_timeout=2,
                write_timeout=2,
                autocommit=True,
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS commchat_agent_audit (
                            task_id VARCHAR(32) PRIMARY KEY,
                            conversation_id BIGINT NULL,
                            mode VARCHAR(32) NULL,
                            status VARCHAR(32) NOT NULL,
                            error TEXT NULL,
                            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP
                        )
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO commchat_agent_audit
                            (task_id, conversation_id, mode, status, error)
                        VALUES (%s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            conversation_id = COALESCE(VALUES(conversation_id), conversation_id),
                            mode = COALESCE(VALUES(mode), mode),
                            status = VALUES(status),
                            error = VALUES(error)
                        """,
                        (task_id, conversation_id, mode, status, error),
                    )
            finally:
                connection.close()
        except Exception:
            return

    def append_task_event(self, task_id: str, event_type: str, content: Any) -> None:
        client = self._redis_client()
        if not client:
            return
        try:
            key = f"commchat:task:{task_id}:events"
            client.rpush(key, json.dumps({"event": event_type, "content": content}, ensure_ascii=False, default=str))
            client.expire(key, 86400)
        except Exception:
            self._redis = False

    def observe_task(self, *, task_id: str, name: str, input: Any = None, output: Any = None, metadata: Any = None, error: str | None = None) -> None:
        client = self._langfuse_client()
        if not client:
            return
        try:
            client.create_event(
                trace_context={"trace_id": task_id},
                name=name,
                input=input,
                output=output,
                metadata=metadata,
                level="ERROR" if error else "DEFAULT",
                status_message=error,
            )
        except Exception:
            self._langfuse = False

    def health(self) -> dict[str, str]:
        return {
            "mysql": self._mysql_health(),
            "redis": "ok" if self._redis_client() else "unavailable",
            "milvus": self._http_health(self.settings.milvus_uri.rstrip("/") + "/healthz"),
            "langfuse": self._http_health(self.settings.langfuse_host.rstrip("/") + "/api/public/health"),
        }

    def _redis_client(self):
        if self._redis is False:
            return None
        if self._redis is None:
            try:
                import redis

                client = redis.Redis(
                    host=self.settings.redis_host,
                    port=self.settings.redis_port,
                    password=self.settings.redis_password or None,
                    socket_connect_timeout=0.5,
                    socket_timeout=0.5,
                    decode_responses=True,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._redis = False
        return self._redis or None

    def _langfuse_client(self):
        if not self.settings.observability_enabled:
            return None
        if self._langfuse is False:
            return None
        if self._langfuse is None:
            if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
                self._langfuse = False
                return None
            try:
                health_url = self.settings.langfuse_host.rstrip("/") + "/api/public/health"
                if self._http_health(health_url) != "ok":
                    self._langfuse = False
                    return None
                from langfuse import Langfuse

                self._langfuse = Langfuse(
                    public_key=self.settings.langfuse_public_key,
                    secret_key=self.settings.langfuse_secret_key,
                    host=self.settings.langfuse_host,
                    timeout=2,
                    flush_at=20,
                )
            except Exception:
                self._langfuse = False
        return self._langfuse or None

    def _mysql_health(self) -> str:
        try:
            import pymysql

            connection = pymysql.connect(
                host=self.settings.db_host,
                port=self.settings.db_port,
                user=self.settings.db_user,
                password=self.settings.db_password,
                database=self.settings.db_name,
                connect_timeout=1,
            )
            connection.close()
            return "ok"
        except Exception:
            return "unavailable"

    @staticmethod
    def _http_health(url: str) -> str:
        try:
            response = httpx.get(url, timeout=0.75)
            return "ok" if response.status_code < 500 else f"http-{response.status_code}"
        except Exception:
            return "unavailable"


@lru_cache
def get_external_integrations() -> ExternalIntegrations:
    return ExternalIntegrations()
