from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ..infra.config import get_settings


class HookRunner:
    """Runs administrator-configured argv hooks without shell parsing."""

    def __init__(self, config_path: str | None = None) -> None:
        configured = config_path if config_path is not None else get_settings().hooks_config_path
        self.path = Path(configured).resolve() if configured else None

    def configured(self) -> bool:
        return bool(self.path and self.path.is_file())

    async def run(self, event: str, payload: dict[str, Any], cwd: str | Path | None = None) -> list[dict]:
        if not self.configured():
            return []
        config = json.loads(self.path.read_text(encoding="utf-8"))
        handlers = config.get("hooks", {}).get(event, [])
        results: list[dict] = []
        for handler in handlers:
            matcher = str(handler.get("matcher") or ".*")
            subject = str(payload.get("tool_name") or payload.get("reason") or event)
            if not re.search(matcher, subject):
                continue
            argv = handler.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
                results.append({"allow": False, "reason": "Invalid hook argv", "event": event})
                continue
            timeout = max(1, min(int(handler.get("timeout", 30)), 120))

            def execute() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    argv,
                    cwd=str(cwd) if cwd else None,
                    input=json.dumps(payload, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    shell=False,
                )

            try:
                completed = await asyncio.to_thread(execute)
                parsed: dict[str, Any] = {}
                if completed.stdout.strip():
                    try:
                        parsed = json.loads(completed.stdout)
                    except json.JSONDecodeError:
                        parsed = {"message": completed.stdout.strip()[:2000]}
                parsed.setdefault("allow", completed.returncode == 0)
                if completed.stderr.strip():
                    parsed.setdefault("reason", completed.stderr.strip()[:2000])
                parsed["event"] = event
                results.append(parsed)
            except (OSError, subprocess.TimeoutExpired) as exc:
                results.append({"allow": False, "reason": f"{type(exc).__name__}: {exc}", "event": event})
        return results


hook_runner = HookRunner()
