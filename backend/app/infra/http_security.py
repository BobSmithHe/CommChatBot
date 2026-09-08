from __future__ import annotations

import re
import uuid


_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class SecurityHeadersMiddleware:
    """Add security and correlation headers without buffering SSE responses."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        request_id = incoming if incoming and _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        path = str(scope.get("path") or "")

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                _set_default(headers, b"x-content-type-options", b"nosniff")
                _set_default(headers, b"x-frame-options", b"DENY")
                _set_default(headers, b"referrer-policy", b"no-referrer")
                _set_default(headers, b"permissions-policy", b"camera=(), microphone=(), geolocation=()")
                _set_default(headers, b"x-request-id", request_id.encode("ascii"))
                if path.startswith("/api/auth/"):
                    _set_default(headers, b"cache-control", b"no-store")
                    _set_default(headers, b"pragma", b"no-cache")
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _set_default(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> None:
    if not any(key.lower() == name for key, _value in headers):
        headers.append((name, value))
