from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.attachments import ChatAttachmentStore


def test_chat_attachment_round_trip(tmp_path):
    store = ChatAttachmentStore(tmp_path)

    uploaded = store.add("notes.md", "OFDM uses orthogonal subcarriers.".encode("utf-8"))
    loaded = store.load_many([uploaded["id"]])

    assert uploaded["name"] == "notes.md"
    assert loaded[0]["name"] == "notes.md"
    assert "OFDM" in loaded[0]["content"]


def test_chat_attachment_rejects_unsupported_type(tmp_path):
    store = ChatAttachmentStore(tmp_path)

    with pytest.raises(ValueError, match="Unsupported"):
        store.add("archive.exe", b"not allowed")


def test_chat_attachment_rejects_invalid_id(tmp_path):
    store = ChatAttachmentStore(tmp_path)

    with pytest.raises(ValueError, match="Invalid"):
        store.load_many(["../secret"])
