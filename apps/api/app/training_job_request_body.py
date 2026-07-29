"""Bounded raw JSON decoding for Phase 11 metadata-only request bodies."""

from __future__ import annotations

import json

from fastapi import Request

TRAINING_JOB_BODY_MAX_BYTES = 2_048
TRAINING_JOB_MUTATION_BODY_MAX_BYTES = 1_024


class TrainingJobBodyError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def read_bounded_training_job_object(
    request: Request, *, maximum_bytes: int
) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise TrainingJobBodyError(415, "Invalid training job request")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise TrainingJobBodyError(400, "Invalid training job request")
        if int(content_length) > maximum_bytes:
            raise TrainingJobBodyError(413, "Training job request is too large")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > maximum_bytes:
            raise TrainingJobBodyError(413, "Training job request is too large")
        raw.extend(chunk)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise TrainingJobBodyError(400, "Invalid training job request") from None
    if not isinstance(value, dict):
        raise TrainingJobBodyError(400, "Invalid training job request")
    return value


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
