from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker

from .config import get_settings


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
    status = Column(String(30), nullable=False, default="running", index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RuntimeEventRecord(Base):
    __tablename__ = "runtime_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(32), ForeignKey("agent_tasks.id"), nullable=False, index=True)
    event_type = Column(String(40), nullable=False)
    content_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


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


settings = get_settings()
Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{settings.sqlite_path}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_conversations()
    _migrate_messages()
    _migrate_agent_tasks()
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
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_conversations_mode ON conversations (mode)")
            )
        if "workspace_id" not in columns:
            connection.execute(text("ALTER TABLE conversations ADD COLUMN workspace_id VARCHAR(64)"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_conversations_workspace_id ON conversations (workspace_id)")
            )
        if "permission_mode" not in columns:
            connection.execute(
                text("ALTER TABLE conversations ADD COLUMN permission_mode VARCHAR(30) NOT NULL DEFAULT 'workspace-write'")
            )
        if "is_archived" not in columns:
            connection.execute(
                text("ALTER TABLE conversations ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT 0")
            )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_conversations_is_archived ON conversations (is_archived)")
            )


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


def _recover_interrupted_tasks() -> None:
    """A process restart cannot keep in-memory model/tool executions alive."""
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_tasks SET status='interrupted', error='Backend restarted' WHERE status IN ('running', 'waiting-approval')")
        )
        connection.execute(
            text("UPDATE approval_requests SET status='expired', resolved_at=CURRENT_TIMESTAMP WHERE status='pending'")
        )


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
