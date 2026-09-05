from __future__ import annotations

from sqlalchemy.orm import Session

from ..database import Conversation
from ..ports import WorkspaceHandle


class ConversationNotFound(LookupError):
    pass


class ConversationModeConflict(ValueError):
    pass


class WorkspaceUnavailable(ValueError):
    pass


class ConversationService:
    """Conversation/workspace use cases shared by HTTP, workers and scheduler."""

    def __init__(self, workspace_manager, context_manager) -> None:
        self.workspace_manager = workspace_manager
        self.context_manager = context_manager

    def get_or_create(
        self, db: Session, user_id: int, conversation_id: int | None, mode: str,
    ) -> Conversation:
        if conversation_id is not None:
            conversation = db.query(Conversation).filter(
                Conversation.id == conversation_id, Conversation.user_id == user_id,
            ).first()
            if not conversation:
                raise ConversationNotFound("Conversation not found")
            if conversation.mode != mode:
                raise ConversationModeConflict("Conversation belongs to a different mode")
            if conversation.mode == "coding-agent":
                self.ensure_workspace(db, conversation)
            return conversation
        workspace_id = self.workspace_manager.create() if mode == "coding-agent" else None
        conversation = Conversation(
            user_id=user_id, mode=mode, workspace_id=workspace_id, title="New Conversation",
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    @staticmethod
    def require(db: Session, user_id: int, conversation_id: int) -> Conversation:
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id, Conversation.user_id == user_id,
        ).first()
        if not conversation:
            raise ConversationNotFound("Conversation not found")
        return conversation

    def ensure_workspace(self, db: Session, conversation: Conversation) -> WorkspaceHandle:
        if conversation.mode != "coding-agent":
            raise WorkspaceUnavailable("Workspace is only available in Coding Agent mode")
        if not conversation.workspace_id:
            conversation.workspace_id = self.workspace_manager.create()
            db.commit()
            db.refresh(conversation)
        return self.workspace_manager.editor(conversation.workspace_id)

    def history(self, db: Session, conversation_id: int) -> list[dict]:
        return self.context_manager.prepare(db, conversation_id)
