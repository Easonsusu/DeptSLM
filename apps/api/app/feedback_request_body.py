"""Bounded incremental JSON request parsing for structured Phase 8 feedback."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request

FEEDBACK_SUBMIT_BODY_MAX_BYTES = 4096
FEEDBACK_REVIEW_BODY_MAX_BYTES = 2048


class FeedbackBodyError(Exception):
    """A content-free feedback transport or JSON failure."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _declared_content_length(request: Request, *, maximum_bytes: int) -> int | None:
    values = [
        value
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if len(values) > 1:
        raise FeedbackBodyError(400, "Invalid feedback request body")
    if not values:
        return None
    raw = values[0]
    if not raw or any(byte < ord("0") or byte > ord("9") for byte in raw):
        raise FeedbackBodyError(400, "Invalid feedback request body")
    if len(raw) > 20:
        raise FeedbackBodyError(413, "Feedback request body too large")
    declared = int(raw)
    if declared > maximum_bytes:
        raise FeedbackBodyError(413, "Feedback request body too large")
    return declared


async def read_bounded_json_object(request: Request, *, maximum_bytes: int) -> dict[str, Any]:
    """Read one strict UTF-8 JSON object without buffering beyond the reviewed limit."""

    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be a positive integer")
    declared = _declared_content_length(request, maximum_bytes=maximum_bytes)
    body = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        if len(chunk) > maximum_bytes - len(body):
            raise FeedbackBodyError(413, "Feedback request body too large")
        body.extend(chunk)
    if declared is not None and declared != len(body):
        raise FeedbackBodyError(400, "Invalid feedback request body")
    if not body:
        raise FeedbackBodyError(400, "Invalid feedback request body")
    try:
        decoded = body.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FeedbackBodyError(400, "Invalid feedback request body") from None
    if not isinstance(value, dict):
        raise FeedbackBodyError(400, "Invalid feedback request body")
    return value
