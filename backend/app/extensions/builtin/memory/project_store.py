from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.infra.config import get_settings


class ProjectMemoryStore:
    """Small durable memory store keyed by project identity, not conversation id."""

    _global_lock = threading.RLock()

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root if root is not None else Path(get_settings().data_dir) / "project_memory"
        self.root = Path(configured).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = self._global_lock

    def read(self, project_id: str) -> dict[str, str]:
        with self._lock:
            payload = self._load(project_id)
            return {
                str(key): str(item.get("value") or "")
                for key, item in (payload.get("entries") or {}).items()
                if isinstance(item, dict) and item.get("value")
            }

    def remember(self, project_id: str, key: str, value: str) -> dict[str, str]:
        normalized_key = " ".join(str(key or "").split()).strip()
        normalized_value = str(value or "").strip()
        if not normalized_key or len(normalized_key) > 120:
            raise ValueError("Memory key must contain 1-120 characters")
        if not normalized_value or len(normalized_value) > 4000:
            raise ValueError("Memory value must contain 1-4000 characters")
        with self._lock:
            payload = self._load(project_id)
            entries = payload.setdefault("entries", {})
            entries[normalized_key] = {
                "value": normalized_value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            if len(entries) > 50:
                oldest = sorted(entries, key=lambda item: entries[item].get("updated_at", ""))
                for item in oldest[:-50]:
                    entries.pop(item, None)
            self._save(project_id, payload)
            return self.read(project_id)

    def forget(self, project_id: str, key: str) -> dict[str, str]:
        with self._lock:
            payload = self._load(project_id)
            entries = payload.setdefault("entries", {})
            if key not in entries:
                raise ValueError(f"Project memory key not found: {key}")
            entries.pop(key)
            self._save(project_id, payload)
            return self.read(project_id)

    def format_for_prompt(self, project_id: str) -> str:
        entries = self.read(project_id)
        if not entries:
            return "No durable project memories have been saved yet."
        return "\n".join(f"- {key}: {value}" for key, value in entries.items())

    def _path(self, project_id: str) -> Path:
        if not project_id:
            raise ValueError("Project identity is required")
        digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def _load(self, project_id: str) -> dict:
        path = self._path(project_id)
        if not path.exists():
            return {"version": 1, "project_id": project_id, "entries": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "project_id": project_id, "entries": {}}
        return payload if isinstance(payload, dict) else {"version": 1, "project_id": project_id, "entries": {}}

    def _save(self, project_id: str, payload: dict) -> None:
        path = self._path(project_id)
        temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
