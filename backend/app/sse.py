from __future__ import annotations

import json
from typing import Any


def sse(event: str, content: Any = None) -> str:
    payload = {"event": event}
    if content is not None:
        payload["content"] = content
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

