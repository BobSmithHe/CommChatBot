from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.runtime"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "CommChatBot"
    app_version: str = "0.8.0"
    host: str = "0.0.0.0"
    port: int = 8765
    cors_origins: str = "http://127.0.0.1:5173,http://localhost:5173"

    data_dir: str = str(PROJECT_ROOT / "data")
    sqlite_path: str = str(PROJECT_ROOT / "data" / "app.db")
    upload_dir: str = str(PROJECT_ROOT / "data" / "uploads")
    knowledge_index_path: str = str(PROJECT_ROOT / "data" / "knowledge" / "index.json")
    coding_workspace_dir: str = str(PROJECT_ROOT / "data" / "coding_workspaces")
    workspace_import_roots: str = str(PROJECT_ROOT)
    git_clone_timeout_seconds: int = 180

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    anthropic_api_key: str = ""
    anthropic_auth_token: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_model: str = "claude-sonnet-4-6"
    chat_model_id: str = ""
    coding_model_id: str = ""
    chat_provider_id: str = "deepseek"
    coding_provider_id: str = "deepseek"
    model_providers_json: str = ""
    model_input_cost_per_million: float = 0.0
    model_output_cost_per_million: float = 0.0
    model_temperature: float = 0.3
    model_max_tokens: int = 4096
    model_context_tokens: int = 32768
    context_summary_tokens: int = 2400
    context_recent_messages: int = 12
    llm_context_compaction: bool = True
    context_compaction_trigger_ratio: float = 0.78
    memory_auto_capture: bool = True
    memory_semantic_recall: bool = True
    memory_recall_limit: int = 12
    memory_min_confidence: float = 0.6
    memory_merge_similarity: float = 0.9
    memory_max_records_per_user: int = 5000
    memory_collection_name: str = "commchat_memories"
    deepseek_thinking_mode: str = "enabled"
    deepseek_reasoning_effort: str = "low"
    max_agent_turns: int = 8
    approval_timeout_seconds: int = 300
    hooks_config_path: str = ""

    tavily_api_key: str = ""
    jwt_secret_key: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30
    refresh_token_days: int = 30
    password_reset_minutes: int = 15
    password_reset_debug: bool = True
    auth_rate_limit_window_seconds: int = 60
    registration_rate_limit: int = 6
    login_rate_limit: int = 8
    password_reset_rate_limit: int = 4
    allow_anonymous: bool = True

    code_exec_timeout: int = 30
    rag_chunk_size: int = 900
    rag_chunk_overlap: int = 150
    rag_dense_model: str = ""
    rag_intent_routing: bool = True
    rag_min_dense_score: float = 0.42
    rag_min_lexical_score: float = 0.15
    rag_search_timeout_seconds: float = 12.0
    model_request_timeout_seconds: float = 90.0
    workspace_dir: str = str(PROJECT_ROOT)

    db_host: str = "localhost"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = ""
    db_name: str = "wireless_comm_ai"
    database_backend: str = "sqlite"
    database_url: str = ""
    database_pool_size: int = 10
    database_max_overflow: int = 20

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    agent_worker_mode: str = "embedded"
    agent_worker_concurrency: int = 8
    task_queue_name: str = "commchatbot:agent:queue"
    task_event_poll_ms: int = 120

    sandbox_mode: str = "local"
    sandbox_image: str = "commchatbot-sandbox:0.8.0"
    sandbox_memory_mb: int = 2048
    sandbox_max_processes: int = 16
    sandbox_cpu_seconds: int = 120
    sandbox_cpus: float = 1.0
    sandbox_tmpfs_mb: int = 256
    sandbox_max_output_bytes: int = 2_000_000
    sandbox_network: str = "deny"
    terminal_max_per_workspace: int = 4

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
