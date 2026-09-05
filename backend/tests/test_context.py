from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.context import ConversationContextManager, estimate_tokens, fit_llm_messages
from app.infra.database import Base, Conversation, Message
from app.packages.ai import LLMMessage


def test_fit_llm_messages_respects_budget_and_keeps_latest() -> None:
    messages = [LLMMessage("system", "summary " * 500)]
    messages.extend(LLMMessage("user", f"message-{index} " * 300) for index in range(15))
    result = fit_llm_messages(messages, 700)
    assert result[0].role == "system"
    assert "message-14" in str(result[-1].content)
    assert sum(estimate_tokens(item.content) + 8 for item in result) <= 705


def test_long_conversation_is_summarized_and_persisted(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'context.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        conversation = Conversation(user_id=1, mode="chatbot", title="long")
        db.add(conversation)
        db.commit()
        for index in range(30):
            db.add(Message(
                user_id=1,
                conversation_id=conversation.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"important fact {index}: " + ("context " * 20),
            ))
        db.commit()

        history = ConversationContextManager().prepare(db, conversation.id)
        db.refresh(conversation)
        assert history[0]["role"] == "system"
        assert "important fact 0" in conversation.context_summary
        assert conversation.summary_until_message_id is not None
        assert len(history) == 13
