"""Narrow non-public HTTP surface for query embedding and grounded generation."""

from __future__ import annotations

import hmac
import json
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
    validate_safe_text,
)
from deptslm_runtime.settings import RuntimeSettings
from deptslm_runtime.supervisor import (
    ModelSupervisor,
    RecoverableModelRequestError,
    RuntimeBusyError,
    RuntimeSupervisorError,
    run_until_disconnect,
)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = RuntimeSettings.from_environment()
    application.state.settings = settings
    supervisor = ModelSupervisor(settings)
    application.state.supervisor = supervisor
    await supervisor.start()
    try:
        yield
    finally:
        await supervisor.close()


app = FastAPI(title="DeptSLM internal RAG runtime", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    if not app.state.supervisor.ready:
        raise HTTPException(503, "Runtime unavailable")
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
    return await _run_model(request, "query_embedding", {"question": question})


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
        try:
            validate_safe_text(text, field="evidence", max_chars=MAX_SOURCE_CHARS)
        except ValueError:
            raise HTTPException(400, "Invalid request") from None
    if labels != [f"S{index}" for index in range(1, len(labels) + 1)] or total > 6000:
        raise HTTPException(400, "Invalid request")
    return await _run_model(request, "generate", {"question": question, "evidence": evidence})


def _authorize(request: Request) -> None:
    raw = request.headers.get("authorization", "")
    expected = f"Bearer {request.app.state.settings.token}"
    if not hmac.compare_digest(raw.encode(), expected.encode()):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


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


async def _run_model(request: Request, operation: str, payload: dict):
    try:
        return await run_until_disconnect(
            request.app.state.supervisor.request(operation, payload),
            request.is_disconnected,
        )
    except RuntimeBusyError:
        raise HTTPException(503, "Runtime busy") from None
    except RecoverableModelRequestError:
        raise HTTPException(422, "Model input exceeds the reviewed token budget") from None
    except RuntimeSupervisorError:
        raise HTTPException(503, "Runtime operation failed") from None
