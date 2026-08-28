from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "CommChatBot"
    app_version: str = "0.4.0"
    host: str = "0.0.0.0"
    port: int = 8765
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    data_dir: str = str(PROJECT_ROOT / "data")
    sqlite_path: str = str(PROJECT_ROOT / "data" / "app.db")
    upload_dir: str = str(PROJECT_ROOT / "data" / "uploads")
    knowledge_index_path: str = str(PROJECT_ROOT / "data" / "knowledge" / "index.json")
    coding_workspace_dir: str = str(PROJECT_ROOT / "data" / "coding_workspaces")

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    chat_model_id: str = ""
    coding_model_id: str = ""
    model_temperature: float = 0.3
    model_max_tokens: int = 4096
    max_agent_turns: int = 8
    approval_timeout_seconds: int = 300
    hooks_config_path: str = ""

    tavily_api_key: str = ""
    jwt_secret_key: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    allow_anonymous: bool = True

    code_exec_timeout: int = 30
    rag_chunk_size: int = 900
    rag_chunk_overlap: int = 150
    rag_dense_model: str = ""
    workspace_dir: str = str(PROJECT_ROOT)

    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "wireless_comm_ai"

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""

    sandbox_mode: str = "local"

    milvus_uri: str = "http://localhost:19530"
    milvus_token: str = ""
    milvus_db_name: str = ""

    embedding_model: str = "BAAI/bge-large-zh-v1.5"
    embedding_dimension: int = 1024
    embedding_device: str = "cpu"

    zhipu_api_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"
    observability_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.knowledge_index_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.coding_workspace_dir).mkdir(parents=True, exist_ok=True)
    return settings
