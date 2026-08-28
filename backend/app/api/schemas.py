from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=6, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class ConversationCreateRequest(BaseModel):
    mode: Literal["chatbot", "coding-agent"] = "chatbot"


class ConversationPermissionRequest(BaseModel):
    permission_mode: Literal["read-only", "workspace-write", "full-access"]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: int | None = None
    mode: Literal["chatbot", "coding-agent"] = "chatbot"
    use_rag: bool = True
    use_web: bool = False
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    system_context: str | None = None
    resume_task_id: str | None = Field(default=None, min_length=32, max_length=32)


class CodeExecuteRequest(BaseModel):
    code: str
    language: str = "python"


class WorkspaceFileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = ""


class WorkspaceDirectoryCreateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class WorkspaceRunRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class TerminalCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)


class WorkspaceMoveRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)


class ApprovalDecisionRequest(BaseModel):
    approved: bool


class GitInitRequest(BaseModel):
    initial_branch: str = Field(default="main", min_length=1, max_length=120)


class GitRestoreRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class GitCheckpointRequest(BaseModel):
    message: str = Field(default="Agent checkpoint", min_length=1, max_length=200)


class WorkspaceTrashRestoreRequest(BaseModel):
    trash_id: str = Field(min_length=32, max_length=32)
