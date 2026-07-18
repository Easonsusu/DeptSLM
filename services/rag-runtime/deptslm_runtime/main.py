"""Narrow non-public HTTP surface for query embedding and grounded generation."""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from app.rag_domain import (
    ANSWER_CONTRACT_VERSION,
    MAX_RUNTIME_BODY_BYTES,
    MAX_SOURCE_CHARS,
    PROMPT_VERSION,
    SOURCE_LABEL,
    normalize_question,
)
from deptslm_runtime.models import RuntimeModelError, RuntimeModels
from deptslm_runtime.settings import RuntimeSettings


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = RuntimeSettings.from_environment()
    application.state.settings = settings
    application.state.models = RuntimeModels(settings.data_dir, settings.provider)
    application.state.capacity = threading.BoundedSemaphore(settings.max_concurrency)
    yield


app = FastAPI(title="DeptSLM internal RAG runtime", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/internal/v1/query-embedding")
async def query_embedding(request: Request) -> dict[str, list[float]]:
    _authorize(request)
    value = await _json_body(request)
    if not isinstance(value, dict) or set(value) != {"question"}:
        raise HTTPException(400, "Invalid request")
    try:
        question = normalize_question(value["question"])
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid request") from None
    if question != value["question"]:
        raise HTTPException(400, "Invalid request")
    return await asyncio.to_thread(
        _with_capacity,
        request,
        lambda: {"vector": request.app.state.models.embed_question(question)},
    )


@app.post("/internal/v1/generate")
async def generate(request: Request) -> dict:
    _authorize(request)
    value = await _json_body(request)
    if not isinstance(value, dict) or set(value) != {
        "question",
        "evidence",
        "prompt_version",
        "answer_contract_version",
    }:
        raise HTTPException(400, "Invalid request")
    try:
        question = normalize_question(value["question"])
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid request") from None
    evidence = value["evidence"]
    if (
        question != value["question"]
        or value["prompt_version"] != PROMPT_VERSION
        or value["answer_contract_version"] != ANSWER_CONTRACT_VERSION
        or not isinstance(evidence, list)
        or not 1 <= len(evidence) <= 8
    ):
        raise HTTPException(400, "Invalid request")
    total = 0
    labels = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"source_id", "text"}:
            raise HTTPException(400, "Invalid request")
        label, text = item["source_id"], item["text"]
        if (
            not isinstance(label, str)
            or SOURCE_LABEL.fullmatch(label) is None
            or label in labels
            or not isinstance(text, str)
            or not text
            or len(text) > MAX_SOURCE_CHARS
        ):
            raise HTTPException(400, "Invalid request")
        labels.append(label)
        total += len(text)
    if labels != [f"S{index}" for index in range(1, len(labels) + 1)] or total > 6000:
        raise HTTPException(400, "Invalid request")
    return await asyncio.to_thread(
        _with_capacity,
        request,
        lambda: request.app.state.models.generate(question, evidence),
    )


def _authorize(request: Request) -> None:
    raw = request.headers.get("authorization", "")
    expected = f"Bearer {request.app.state.settings.token}"
    if not hmac.compare_digest(raw.encode(), expected.encode()):
        raise HTTPException(
            401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
        )


async def _json_body(request: Request):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise HTTPException(400, "Invalid request")
        if int(content_length) > MAX_RUNTIME_BODY_BYTES:
            raise HTTPException(413, "Request too large")
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_RUNTIME_BODY_BYTES:
            raise HTTPException(413, "Request too large")
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Invalid request") from None


def _with_capacity(request: Request, operation):
    capacity = request.app.state.capacity
    if not capacity.acquire(blocking=False):
        raise HTTPException(503, "Runtime busy")
    try:
        return operation()
    except RuntimeModelError:
        raise HTTPException(503, "Runtime operation failed") from None
    finally:
        capacity.release()
