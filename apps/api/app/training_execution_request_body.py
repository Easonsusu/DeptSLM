"""Bounded closed JSON bodies for Phase 14.1 execution routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request

TRAINING_EXECUTION_BODY_MAX_BYTES = 2048
TRAINING_EXECUTION_MUTATION_BODY_MAX_BYTES = 1024


class TrainingExecutionBodyError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TrainingExecutionBodyError(422, "Invalid training execution request")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError()


async def read_bounded_training_execution_object(
    request: Request, *, maximum_bytes: int
) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) < 0 or int(content_length) > maximum_bytes:
                raise TrainingExecutionBodyError(413, "Training execution request is too large")
        except ValueError as error:
            raise TrainingExecutionBodyError(400, "Invalid Content-Length") from error
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > maximum_bytes:
            raise TrainingExecutionBodyError(413, "Training execution request is too large")
        chunks.append(chunk)
    try:
        value = json.loads(
            b"".join(chunks),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise TrainingExecutionBodyError(400, "Invalid training execution request") from error
    if not isinstance(value, dict):
        raise TrainingExecutionBodyError(422, "Invalid training execution request")
    return value
