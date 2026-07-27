"""Bound raw control-plane SFT request bodies before JSON decoding."""

from __future__ import annotations

import json

from fastapi import Request

SFT_REVIEW_BODY_MAX_BYTES = 1024
SFT_CANCEL_BODY_MAX_BYTES = 1024


class SftBodyError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def read_bounded_sft_object(request: Request, *, maximum_bytes: int) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise SftBodyError(415, "Invalid SFT request")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise SftBodyError(400, "Invalid SFT request")
        if int(content_length) > maximum_bytes:
            raise SftBodyError(413, "SFT request is too large")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > maximum_bytes:
            raise SftBodyError(413, "SFT request is too large")
        data.extend(chunk)
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SftBodyError(400, "Invalid SFT request") from None
    if not isinstance(value, dict):
        raise SftBodyError(400, "Invalid SFT request")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value
