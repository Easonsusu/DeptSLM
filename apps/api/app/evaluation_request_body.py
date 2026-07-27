"""Bound evaluation control bodies before strict JSON decoding."""

from __future__ import annotations

import json

from fastapi import Request


class EvaluationBodyError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def read_bounded_evaluation_object(
    request: Request, *, maximum_bytes: int
) -> dict[str, object]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise EvaluationBodyError(400, "Invalid evaluation request")
        if int(content_length) > maximum_bytes:
            raise EvaluationBodyError(413, "Evaluation request is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise EvaluationBodyError(413, "Evaluation request is too large")
        body.extend(chunk)
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EvaluationBodyError(400, "Invalid evaluation request") from None
    if not isinstance(value, dict):
        raise EvaluationBodyError(400, "Invalid evaluation request")
    return value
