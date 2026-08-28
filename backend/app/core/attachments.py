from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from ..infra.config import get_settings
from .documents import extract_text


ALLOWED_SUFFIXES = {".txt", ".md", ".pdf", ".py", ".json", ".csv"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_CHARS = 100_000
ATTACHMENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")


class ChatAttachmentStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        base = Path(directory) if directory is not None else Path(get_settings().data_dir) / "chat_attachments"
        self.directory = base.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def add(self, filename: str, raw: bytes, user_id: int | None = None) -> dict:
        name = Path(filename or "attachment.txt").name
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise ValueError("Unsupported attachment type")
        if not raw:
            raise ValueError("Empty attachment")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ValueError("Attachment exceeds 10 MB")
        content = extract_text(name, raw).strip()
        if not content:
            raise ValueError("No text extracted from attachment")
        attachment_id = uuid.uuid4().hex
        payload = {"id": attachment_id, "name": name, "content": content[:MAX_ATTACHMENT_CHARS], "user_id": user_id}
        path = self.directory / f"{attachment_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return {"id": attachment_id, "name": name, "characters": len(payload["content"])}

    def load_many(self, attachment_ids: list[str], limit: int = 5, user_id: int | None = None) -> list[dict]:
        if len(attachment_ids) > limit:
            raise ValueError(f"At most {limit} attachments are allowed")
        attachments: list[dict] = []
        for attachment_id in attachment_ids:
            if not ATTACHMENT_ID_RE.fullmatch(attachment_id):
                raise ValueError("Invalid attachment id")
            path = self.directory / f"{attachment_id}.json"
            if not path.is_file():
                raise ValueError("Attachment not found")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if user_id is not None and payload.get("user_id") not in {None, user_id}:
                raise ValueError("Attachment does not belong to the current user")
            attachments.append({"id": attachment_id, "name": payload["name"], "content": payload["content"]})
        return attachments
