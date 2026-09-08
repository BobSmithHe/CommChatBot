from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from ..infra.config import get_settings


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False, default="")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="user")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    mode = Column(String(30), nullable=False, default="chatbot", server_default="chatbot", index=True)
    workspace_id = Column(String(64), nullable=True, index=True)
    permission_mode = Column(String(30), nullable=False, default="workspace-write", server_default="workspace-write")
    is_archived = Column(Boolean, nullable=False, default=False, server_default="0", index=True)
    context_summary = Column(Text, nullable=True)
    summary_until_message_id = Column(Integer, nullable=True)
    parent_conversation_id = Column(Integer, nullable=True, index=True)
    forked_from_message_id = Column(Integer, nullable=True)
    branch_name = Column(String(120), nullable=False, default="main", server_default="main")
    project_trusted = Column(Boolean, nullable=False, default=False, server_default="0")
    title = Column(String(200), default="New Conversation")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    role = Column(String(30), nullable=False)
    content = Column(Text, nullable=False)
    trace_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    mode = Column(String(30), nullable=False)
    prompt = Column(Text, nullable=False)
    checkpoint_json = Column(Text, nullable=True)
    request_json = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="running", index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RuntimeEventRecord(Base):
    __tablename__ = "runtime_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(32), nullable=True, unique=True, index=True)
    task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=False, index=True)
    event_type = Column(String(40), nullable=False)
    content_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AgentSubtaskRecord(Base):
    __tablename__ = "agent_subtasks"

    id = Column(String(32), primary_key=True)
    task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=False, index=True)
    parent_subtask_id = Column(String(32), nullable=True, index=True)
    name = Column(String(160), nullable=False)
    focus = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="running", index=True)
    model = Column(String(200), nullable=False, default="")
    request_json = Column(Text, nullable=True)
    checkpoint_json = Column(Text, nullable=True)
    result = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SubagentMailboxMessage(Base):
    __tablename__ = "subagent_mailbox_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    subtask_id = Column(String(32), ForeignKey("agent_subtasks.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    consumed_at = Column(DateTime, nullable=True)


class MemoryExtractionJob(Base):
    __tablename__ = "memory_extraction_jobs"

    id = Column(String(32), primary_key=True)
    task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    mode = Column(String(30), nullable=False)
    user_text = Column(Text, nullable=False)
    assistant_text = Column(Text, nullable=False)
    project_identity = Column(String(500), nullable=True)
    status = Column(String(30), nullable=False, default="queued", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ScheduledAutomationRecord(Base):
    __tablename__ = "scheduled_automations"

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    prompt = Column(Text, nullable=False)
    interval_seconds = Column(Integer, nullable=False)
    rrule = Column(Text, nullable=True)
    timezone = Column(String(80), nullable=False, default="UTC", server_default="UTC")
    status = Column(String(20), nullable=False, default="active", index=True)
    next_run_at = Column(DateTime, nullable=False, index=True)
    last_run_at = Column(DateTime, nullable=True)
    last_task_id = Column(String(32), nullable=True)
    notification_policy = Column(String(30), nullable=False, default="completion")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NotificationRecord(Base):
    __tablename__ = "notifications"

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(40), nullable=False)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    task_id = Column(String(32), nullable=True, index=True)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class NotificationEndpointRecord(Base):
    __tablename__ = "notification_endpoints"

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    target = Column(Text, nullable=False)
    secret = Column(String(255), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NotificationDeliveryRecord(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("notification_id", "endpoint_id", name="uq_notification_delivery"),
    )

    id = Column(String(32), primary_key=True)
    notification_id = Column(String(32), ForeignKey("notifications.id"), nullable=False, index=True)
    endpoint_id = Column(String(32), ForeignKey("notification_endpoints.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="queued", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    delivered_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GitHubSubscriptionRecord(Base):
    __tablename__ = "github_subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "repository", name="uq_github_subscription"),
    )

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    repository = Column(String(250), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=datetime.utcnow)


class GitHubWebhookEventRecord(Base):
    __tablename__ = "github_webhook_events"

    id = Column(String(32), primary_key=True)
    delivery_id = Column(String(100), nullable=False, unique=True, index=True)
    event_type = Column(String(80), nullable=False, index=True)
    repository = Column(String(250), nullable=True, index=True)
    action = Column(String(80), nullable=True)
    payload_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class RemoteRunnerRecord(Base):
    __tablename__ = "remote_runners"

    id = Column(String(32), primary_key=True)
    name = Column(String(120), nullable=False)
    capabilities_json = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="online", index=True)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RemoteJobRecord(Base):
    __tablename__ = "remote_jobs"

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    runner_id = Column(String(32), ForeignKey("remote_runners.id"), nullable=False, index=True)
    task_id = Column(String(32), nullable=True, index=True)
    command_json = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="queued", index=True)
    lease_token = Column(String(64), nullable=True, index=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    cancel_requested = Column(Boolean, nullable=False, default=False, server_default="0")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    output = Column(Text, nullable=True)
    exit_code = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApprovalRequestRecord(Base):
    __tablename__ = "approval_requests"

    id = Column(String(32), primary_key=True)
    task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=False, index=True)
    tool_name = Column(String(120), nullable=False)
    tool_input_json = Column(Text, nullable=False)
    capability = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)


class TaskMailboxMessage(Base):
    __tablename__ = "task_mailbox_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False, index=True)
    content = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    consumed_at = Column(DateTime, nullable=True)


class ModelUsageRecord(Base):
    __tablename__ = "model_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=True, index=True)
    provider = Column(String(80), nullable=False, default="")
    model = Column(String(200), nullable=False, default="")
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class MemorySettingsRecord(Base):
    __tablename__ = "memory_settings"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=True)
    auto_capture = Column(Boolean, nullable=False, default=True)
    semantic_recall = Column(Boolean, nullable=False, default=True)
    user_scope = Column(Boolean, nullable=False, default=True)
    project_scope = Column(Boolean, nullable=False, default=True)
    conversation_scope = Column(Boolean, nullable=False, default=True)
    task_scope = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MemoryRecord(Base):
    # Keep separate from legacy application tables named ``memories``.
    __tablename__ = "agent_memories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "scope",
            "target_id",
            "normalized_key",
            name="uq_memory_target_key",
        ),
    )

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    scope = Column(String(20), nullable=False, index=True)
    target_id = Column(String(96), nullable=False, index=True)
    key = Column(String(200), nullable=False)
    normalized_key = Column(String(200), nullable=False)
    value = Column(Text, nullable=False)
    category = Column(String(40), nullable=False, default="fact", index=True)
    importance = Column(Float, nullable=False, default=0.5)
    confidence = Column(Float, nullable=False, default=0.8)
    privacy = Column(String(20), nullable=False, default="private")
    source_type = Column(String(30), nullable=False, default="manual")
    source_message_id = Column(Integer, ForeignKey("messages.id"), nullable=True)
    source_task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=True, index=True)
    metadata_json = Column(Text, nullable=True)
    history_json = Column(Text, nullable=True)
    revision = Column(Integer, nullable=False, default=1)
    access_count = Column(Integer, nullable=False, default=0)
    last_accessed_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)


class RefreshTokenRecord(Base):
    __tablename__ = "refresh_tokens"

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    ip_address = Column(String(80), nullable=True)
    user_agent = Column(String(300), nullable=True)


class PasswordResetTokenRecord(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(String(32), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AuthAuditRecord(Base):
    __tablename__ = "auth_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event = Column(String(50), nullable=False, index=True)
    subject = Column(String(200), nullable=True)
    ip_address = Column(String(80), nullable=True)
    success = Column(Boolean, nullable=False, default=False)
    detail = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


settings = get_settings()
Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
if settings.database_url.strip():
    database_url = settings.database_url.strip()
elif settings.database_backend.casefold() == "mysql":
    database_url = (
        f"mysql+pymysql://{quote_plus(settings.db_user)}:{quote_plus(settings.db_password)}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}?charset=utf8mb4"
    )
else:
    database_url = f"sqlite:///{settings.sqlite_path}"

engine_options = {"pool_pre_ping": True}
if database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
else:
    engine_options.update({
        "pool_size": max(2, settings.database_pool_size),
        "max_overflow": max(0, settings.database_max_overflow),
        "pool_recycle": 1800,
    })
engine = create_engine(database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db(*, recover_tasks: bool = True) -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_conversations()
    _migrate_messages()
    _migrate_agent_tasks()
    _migrate_agent_subtasks()
    _migrate_runtime_events()
    _migrate_automations()
    _migrate_remote_jobs()
    if recover_tasks:
        _recover_interrupted_tasks()
    with SessionLocal() as db:
        ensure_anonymous_user(db)


def _migrate_conversations() -> None:
    """Add conversation fields introduced after the initial SQLite schema."""
    columns = {column["name"] for column in inspect(engine).get_columns("conversations")}
    with engine.begin() as connection:
        if "mode" not in columns:
            connection.execute(
                text("ALTER TABLE conversations ADD COLUMN mode VARCHAR(30) NOT NULL DEFAULT 'chatbot'")
            )
            _create_index(connection, "ix_conversations_mode", "conversations", "mode")
        if "workspace_id" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN workspace_id VARCHAR(64)"))
            _create_index(connection, "ix_conversations_workspace_id", "conversations", "workspace_id")
        if "permission_mode" not in columns:
            connection.execute(
                text("ALTER TABLE conversations ADD COLUMN permission_mode VARCHAR(30) NOT NULL DEFAULT 'workspace-write'")
            )
        if "is_archived" not in columns:
            connection.execute(
                text("ALTER TABLE conversations ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT 0")
            )
            _create_index(connection, "ix_conversations_is_archived", "conversations", "is_archived")
        if "context_summary" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN context_summary TEXT"))
        if "summary_until_message_id" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN summary_until_message_id INTEGER"))
        if "parent_conversation_id" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN parent_conversation_id INTEGER"))
            _create_index(connection, "ix_conversations_parent_conversation_id", "conversations", "parent_conversation_id")
        if "forked_from_message_id" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN forked_from_message_id INTEGER"))
        if "branch_name" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN branch_name VARCHAR(120) NOT NULL DEFAULT 'main'"))
        if "project_trusted" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN project_trusted BOOLEAN NOT NULL DEFAULT 0"))


def _migrate_messages() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("messages")}
    if "trace_json" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE messages ADD COLUMN trace_json TEXT"))


def _migrate_agent_tasks() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("agent_tasks")}
    if "checkpoint_json" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE agent_tasks ADD COLUMN checkpoint_json TEXT"))
    if "request_json" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE agent_tasks ADD COLUMN request_json TEXT"))


def _migrate_agent_subtasks() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("agent_subtasks")}
    with engine.begin() as connection:
        if "request_json" not in columns:
            connection.execute(text("ALTER TABLE agent_subtasks ADD COLUMN request_json TEXT"))
        if "checkpoint_json" not in columns:
            connection.execute(text("ALTER TABLE agent_subtasks ADD COLUMN checkpoint_json TEXT"))


def _migrate_runtime_events() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("runtime_events")}
    with engine.begin() as connection:
        if "event_key" not in columns:
            connection.execute(text("ALTER TABLE runtime_events ADD COLUMN event_key VARCHAR(32)"))
        _create_index(connection, "ix_runtime_events_event_key", "runtime_events", "event_key")
        connection.execute(text(
            "DELETE FROM runtime_events WHERE event_key IS NOT NULL AND id NOT IN "
            "(SELECT keep_id FROM (SELECT MIN(id) AS keep_id FROM runtime_events "
            "WHERE event_key IS NOT NULL GROUP BY event_key) AS kept)"
        ))
        _create_index(connection, "ux_runtime_events_event_key", "runtime_events", "event_key", unique=True)


def _migrate_automations() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("scheduled_automations")}
    with engine.begin() as connection:
        if "rrule" not in columns:
            connection.execute(text("ALTER TABLE scheduled_automations ADD COLUMN rrule TEXT"))
        if "timezone" not in columns:
            connection.execute(text("ALTER TABLE scheduled_automations ADD COLUMN timezone VARCHAR(80) NOT NULL DEFAULT 'UTC'"))


def _migrate_remote_jobs() -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("remote_jobs")}
    with engine.begin() as connection:
        if "lease_token" not in columns:
            connection.execute(text("ALTER TABLE remote_jobs ADD COLUMN lease_token VARCHAR(64)"))
        if "lease_expires_at" not in columns:
            connection.execute(text("ALTER TABLE remote_jobs ADD COLUMN lease_expires_at DATETIME"))
        if "attempts" not in columns:
            connection.execute(text("ALTER TABLE remote_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"))
        if "cancel_requested" not in columns:
            connection.execute(text("ALTER TABLE remote_jobs ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT 0"))
        if "started_at" not in columns:
            connection.execute(text("ALTER TABLE remote_jobs ADD COLUMN started_at DATETIME"))
        if "finished_at" not in columns:
            connection.execute(text("ALTER TABLE remote_jobs ADD COLUMN finished_at DATETIME"))
        _create_index(connection, "ix_remote_jobs_lease_token", "remote_jobs", "lease_token")
        _create_index(connection, "ix_remote_jobs_lease_expires_at", "remote_jobs", "lease_expires_at")


def _recover_interrupted_tasks() -> None:
    """Make unfinished work claimable by a new worker after restart."""
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_tasks SET status='queued', error='Recovered after process restart' WHERE status='running'")
        )
        connection.execute(
            text("UPDATE approval_requests SET status='expired', resolved_at=CURRENT_TIMESTAMP WHERE status='pending'")
        )
        connection.execute(
            text("UPDATE agent_tasks SET status='queued', error='Approval expired after process restart' WHERE status='waiting-approval'")
        )
        connection.execute(
            text("UPDATE agent_subtasks SET status='interrupted', error='Recovered after process restart' WHERE status IN ('queued', 'running')")
        )


def _create_index(connection, name: str, table: str, column: str, *, unique: bool = False) -> None:
    existing = {item["name"] for item in inspect(engine).get_indexes(table)}
    if name not in existing:
        qualifier = "UNIQUE " if unique else ""
        connection.execute(text(f"CREATE {qualifier}INDEX {name} ON {table} ({column})"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_anonymous_user(db: Session) -> User:
    user = db.query(User).filter(User.id == 1).first()
    if user:
        return user
    user = User(id=1, username="anonymous", email="anonymous@local", password_hash="")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
