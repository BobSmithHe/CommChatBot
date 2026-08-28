from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.conversation_utils import get_or_create_conversation


class _Query:
    def __init__(self, value):
        self.value = value

    def filter(self, *_args):
        return self

    def first(self):
        return self.value


class _Db:
    def __init__(self, value):
        self.value = value

    def query(self, *_args):
        return _Query(self.value)


def test_existing_chat_conversation_does_not_require_coding_workspace():
    conversation = SimpleNamespace(
        id=7,
        user_id=1,
        mode="chatbot",
        workspace_id=None,
    )

    result = get_or_create_conversation(
        _Db(conversation),
        user_id=1,
        conversation_id=7,
        mode="chatbot",
    )

    assert result is conversation
