"""Bounded request-body transport protection for non-upload API routes."""

from __future__ import annotations

import json
import re
from uuid import UUID

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_NON_UPLOAD_REQUEST_BODY_BYTES = 65_536
_UPLOAD_PATH_PARTS = 4
_UUID_PATH = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_raw_document_upload(scope: Scope) -> bool:
    """Return whether a request is the exact Phase 4 streaming upload route."""

    if scope.get("method") != "POST":
        return False
    path = scope.get("path", "")
    parts = path.split("/")
    if len(parts) != _UPLOAD_PATH_PARTS or parts[1] != "departments" or parts[3] != "documents":
        return False
    if _UUID_PATH.fullmatch(parts[2]) is None:
        return False
    try:
        UUID(parts[2])
    except (ValueError, AttributeError):
        return False
    return True


class NonUploadBodyLimitMiddleware:
    """Read at most limit+1 bytes, then replay only the bounded body downstream."""

    def __init__(self, app: ASGIApp, *, limit: int = MAX_NON_UPLOAD_REQUEST_BODY_BYTES) -> None:
        if limit <= 0:
            raise ValueError("body limit must be positive")
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or is_raw_document_upload(scope):
            await self.app(scope, receive, send)
            return

        declared_length = _content_length(scope.get("headers", []))
        if declared_length is not None and declared_length > self.limit:
            await _send_too_large(send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                await self.app(scope, _replay_body(bytes(body), disconnected=True), send)
                return
            if message["type"] != "http.request":
                await self.app(scope, _replay_body(bytes(body)), send)
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                await _send_too_large(send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        await self.app(scope, _replay_body(bytes(body)), send)


def _content_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    values: list[int] = []
    for key, value in headers:
        if key.lower() != b"content-length":
            continue
        try:
            parsed = int(value.decode("ascii"), 10)
        except (UnicodeDecodeError, ValueError):
            return None
        if parsed < 0:
            return None
        values.append(parsed)
    return max(values) if values else None


def _replay_body(body: bytes, *, disconnected: bool = False) -> Receive:
    delivered = False
    disconnect_delivered = False

    async def receive() -> Message:
        nonlocal delivered, disconnect_delivered
        if delivered:
            if disconnected and not disconnect_delivered:
                disconnect_delivered = True
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {
            "type": "http.request",
            "body": body,
            "more_body": disconnected,
        }

    return receive


async def _send_too_large(send: Send) -> None:
    payload = json.dumps({"detail": "Request body too large"}, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


__all__ = [
    "MAX_NON_UPLOAD_REQUEST_BODY_BYTES",
    "NonUploadBodyLimitMiddleware",
    "is_raw_document_upload",
]
