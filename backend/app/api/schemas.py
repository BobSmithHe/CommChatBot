from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=80)
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=500)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=500)


class PasswordForgotRequest(BaseModel):
    identity: str = Field(min_length=2, max_length=200)


class PasswordResetRequest(BaseModel):
    reset_token: str = Field(min_length=32, max_length=500)
    new_password: str = Field(min_length=8, max_length=200)


class ConversationCreateRequest(BaseModel):
    mode: Literal["chatbot", "coding-agent"] = "chatbot"


class ConversationForkRequest(BaseModel):
    from_message_id: int | None = Field(default=None, ge=1)
    branch_name: str = Field(default="branch", min_length=1, max_length=120)


class ConversationTrustRequest(BaseModel):
    trusted: bool


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
    expected_version: str | None = Field(default=None, max_length=80)
    force: bool = False


class WorkspaceDirectoryCreateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class WorkspaceRunRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class TerminalCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=4000)


class LanguageDocumentRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(default="", max_length=1_000_000)


class LanguagePositionRequest(LanguageDocumentRequest):
    line: int = Field(ge=1, le=1_000_000)
    column: int = Field(ge=1, le=100_000)


class LanguageRenameRequest(LanguagePositionRequest):
    new_name: str = Field(min_length=1, max_length=200)


class WorkspaceMoveRequest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    target: str = Field(min_length=1, max_length=500)


class WorkspaceImportRequest(BaseModel):
    source: str = Field(min_length=1, max_length=2000)
    kind: Literal["local", "clone", "worktree"] = "local"
    branch: str = Field(default="main", min_length=1, max_length=120)


class GitBranchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    create: bool = False


class ApprovalDecisionRequest(BaseModel):
    approved: bool


class TaskMessageRequest(BaseModel):
    kind: Literal["steering", "follow_up"] = "steering"
    content: str = Field(min_length=1, max_length=20_000)


class MemoryCreateRequest(BaseModel):
    scope: Literal["user", "project", "conversation", "task"] = "user"
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=8000)
    category: Literal["preference", "fact", "decision", "workflow", "constraint"] = "fact"
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    conversation_id: int | None = Field(default=None, ge=1)
    task_id: str | None = Field(default=None, min_length=32, max_length=32)
    ttl_days: int = Field(default=0, ge=0, le=3650)


class MemoryUpdateRequest(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=200)
    value: str | None = Field(default=None, min_length=1, max_length=8000)
    category: Literal["preference", "fact", "decision", "workflow", "constraint"] | None = None
    importance: float | None = Field(default=None, ge=0, le=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    ttl_days: int | None = Field(default=None, ge=0, le=3650)


class MemorySettingsRequest(BaseModel):
    enabled: bool | None = None
    auto_capture: bool | None = None
    semantic_recall: bool | None = None
    user_scope: bool | None = None
    project_scope: bool | None = None
    conversation_scope: bool | None = None
    task_scope: bool | None = None


class GitInitRequest(BaseModel):
    initial_branch: str = Field(default="main", min_length=1, max_length=120)


class GitRestoreRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class GitCheckpointRequest(BaseModel):
    message: str = Field(default="Agent checkpoint", min_length=1, max_length=200)


class WorkspaceTrashRestoreRequest(BaseModel):
    trash_id: str = Field(min_length=32, max_length=32)


class AutomationCreateRequest(BaseModel):
    conversation_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=20_000)
    interval_seconds: int = Field(default=3600, ge=60, le=31_536_000)
    rrule: str | None = Field(default=None, max_length=1000)
    timezone: str = Field(default="UTC", min_length=1, max_length=80)
    notification_policy: Literal["completion", "failure", "none"] = "completion"


class AutomationUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    prompt: str | None = Field(default=None, min_length=1, max_length=20_000)
    interval_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    rrule: str | None = Field(default=None, max_length=1000)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    status: Literal["active", "paused"] | None = None
    notification_policy: Literal["completion", "failure", "none"] | None = None


class NotificationEndpointCreateRequest(BaseModel):
    kind: Literal["webhook", "email"]
    name: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=3, max_length=2000)
    secret: str | None = Field(default=None, max_length=255)


class NotificationEndpointUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    target: str | None = Field(default=None, min_length=3, max_length=2000)
    secret: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class GitHubSubscriptionRequest(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=250)


class GitHubInlineReviewRequest(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=250)
    number: int = Field(ge=1)
    body: str = Field(default="", max_length=65_000)
    event: Literal["COMMENT", "APPROVE", "REQUEST_CHANGES"] = "COMMENT"
    commit_id: str | None = Field(default=None, max_length=80)
    comments: list[dict] = Field(default_factory=list, max_length=100)


class RemoteRunnerHeartbeatRequest(BaseModel):
    runner_id: str | None = Field(default=None, min_length=32, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    capabilities: dict = Field(default_factory=dict)


class RemoteJobCreateRequest(BaseModel):
    runner_id: str = Field(min_length=32, max_length=32)
    argv: list[str] = Field(min_length=1, max_length=100)
    timeout: int = Field(default=300, ge=1, le=3600)


class RemoteJobCompleteRequest(BaseModel):
    output: str = Field(default="", max_length=2_000_000)
    exit_code: int
