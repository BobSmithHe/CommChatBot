from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

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
            "milvus": self._milvus_health(),
            "langfuse": self._http_health(self.settings.langfuse_host.rstrip("/") + "/api/public/health"),
            "sandbox": self._sandbox_health(),
        }

    def _sandbox_health(self) -> str:
        if self.settings.sandbox_mode.casefold() != "docker":
            return f"ok ({self.settings.sandbox_mode})"
        docker = shutil.which("docker")
        if not docker:
            return "unavailable"
        try:
            result = subprocess.run(
                [docker, "image", "inspect", self.settings.sandbox_image],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return "ok (docker)" if result.returncode == 0 else "image unavailable"
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"

    def _milvus_health(self) -> str:
        try:
            from pymilvus import MilvusClient

            client = MilvusClient(
                uri=self.settings.milvus_uri,
                token=self.settings.milvus_token or None,
                db_name=self.settings.milvus_db_name or "default",
                timeout=2,
            )
            client.list_collections(timeout=2)
            client.close()
            return "ok"
        except Exception:
            return "unavailable"

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

                # Local self-hosted Langfuse must not be routed through a
                # machine-wide HTTP(S) proxy.  Proxying localhost can produce
                # misleading empty 502 responses even though Langfuse itself
                # is healthy.  Remote deployments keep the normal proxy
                # behaviour.
                http_client = httpx.Client(
                    timeout=2,
                    trust_env=self._trust_env_for_url(self.settings.langfuse_host),
                )
                self._langfuse = Langfuse(
                    public_key=self.settings.langfuse_public_key,
                    secret_key=self.settings.langfuse_secret_key,
                    host=self.settings.langfuse_host,
                    timeout=2,
                    flush_at=20,
                    httpx_client=http_client,
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
        # Self-hosted services can take longer than a sub-second timeout while
        # their Docker container is warming up.  A small retry avoids reporting
        # a healthy local Langfuse instance as unavailable without making the
        # general health endpoint block for an excessive amount of time.
        with httpx.Client(
            timeout=2.0,
            trust_env=ExternalIntegrations._trust_env_for_url(url),
        ) as client:
            for attempt in range(2):
                try:
                    response = client.get(url)
                    return "ok" if response.status_code < 500 else f"http-{response.status_code}"
                except (httpx.TimeoutException, httpx.NetworkError):
                    if attempt:
                        return "unavailable"
                except Exception:
                    return "unavailable"
        return "unavailable"

    @staticmethod
    def _trust_env_for_url(url: str) -> bool:
        """Use environment proxies except for localhost/loopback services."""
        hostname = (urlparse(url).hostname or "").casefold()
        if hostname == "localhost":
            return False
        try:
            return not ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return True


@lru_cache
def get_external_integrations() -> ExternalIntegrations:
    return ExternalIntegrations()
