"""Private, internal-only adapter evaluation runtime supervisor."""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_REVISION
from app.rag_domain import (
    ANSWER_CONTRACT_VERSION,
    MAX_RUNTIME_BODY_BYTES,
    PROMPT_VERSION,
    SOURCE_LABEL,
    normalize_question,
    validate_safe_text,
)
from deptslm_adapter_runtime.settings import AdapterRuntimeSettings
from deptslm_adapter_runtime.supervisor import (
    AdapterRuntimeSupervisor,
    AdapterRuntimeSupervisorError,
)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = AdapterRuntimeSettings.from_environment()
    supervisor = AdapterRuntimeSupervisor(settings)
    await supervisor.start()
    application.state.settings = settings
    application.state.supervisor = supervisor
    try:
        yield
    finally:
        await supervisor.close()


app = FastAPI(title="DeptSLM private adapter evaluation runtime", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    if not app.state.supervisor.ready:
        raise HTTPException(503, "Runtime unavailable")
    return {"status": "ready"}


@app.post("/internal/v1/generate")
async def generate(request: Request) -> dict[str, object]:
    _authorize(request)
    value = await _body(request)
    _validate_request(value)
    try:
        return await app.state.supervisor.request(value)
    except AdapterRuntimeSupervisorError as error:
        if error.code == "invalid_request":
            raise HTTPException(400, "Invalid request") from None
        if error.code in {
            "candidate_adapter_load_failed",
            "candidate_runtime_timeout",
            "candidate_runtime_unavailable",
            "invalid_generation_response",
        }:
            return JSONResponse(status_code=503, content={"code": error.code})
        raise HTTPException(503, "Candidate runtime unavailable") from None


def _authorize(request: Request) -> None:
    expected = f"Bearer {request.app.state.settings.token}"
    if not hmac.compare_digest(
        request.headers.get("authorization", "").encode(), expected.encode()
    ):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


async def _body(request: Request) -> object:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_RUNTIME_BODY_BYTES:
            raise HTTPException(413, "Request too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Invalid request") from None


def _validate_request(value: object) -> None:
    required = {
        "operation",
        "target",
        "base_model_id",
        "base_model_revision",
        "department_id",
        "adapter_id",
        "adapter_version",
        "registry_publication_attempt_id",
        "registry_attempt_number",
        "registry_manifest_sha256",
        "adapter_config_sha256",
        "adapter_config_byte_size",
        "adapter_model_sha256",
        "adapter_model_byte_size",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise HTTPException(400, "Invalid request")
    if value["operation"] not in {"generate", "verify"}:
        raise HTTPException(400, "Invalid request")
    target_fields = {
        "target",
        "base_model_id",
        "base_model_revision",
        "department_id",
        "adapter_id",
        "adapter_version",
        "registry_publication_attempt_id",
        "registry_attempt_number",
        "registry_manifest_sha256",
        "adapter_config_sha256",
        "adapter_config_byte_size",
        "adapter_model_sha256",
        "adapter_model_byte_size",
    }
    if value["operation"] == "verify":
        if set(value) != {"operation", *target_fields}:
            raise HTTPException(400, "Invalid request")
    else:
        if set(value) != {
            "operation",
            *target_fields,
            "question",
            "evidence",
            "prompt_version",
            "answer_contract_version",
            "seed",
        }:
            raise HTTPException(400, "Invalid request")
    if (
        value.get("prompt_version") not in {None, PROMPT_VERSION}
        or value.get("answer_contract_version") not in {None, ANSWER_CONTRACT_VERSION}
        or value["target"] != "candidate"
        or value["base_model_id"] != BASE_MODEL_ID
        or value["base_model_revision"] != BASE_MODEL_REVISION
    ):
        raise HTTPException(400, "Invalid request")
    try:
        for name in (
            "department_id",
            "adapter_id",
            "registry_publication_attempt_id",
        ):
            parsed = UUID(value[name])
            if parsed.int == 0:
                raise ValueError
        for name in (
            "registry_manifest_sha256",
            "adapter_config_sha256",
            "adapter_model_sha256",
        ):
            if (
                not isinstance(value[name], str)
                or re.fullmatch(r"[0-9a-f]{64}", value[name]) is None
            ):
                raise ValueError
        for name in ("adapter_version", "registry_attempt_number"):
            if type(value[name]) is not int or value[name] <= 0:
                raise ValueError
        for name in ("adapter_config_byte_size", "adapter_model_byte_size"):
            if type(value[name]) is not int or value[name] <= 0:
                raise ValueError
        if value["operation"] == "generate":
            question = normalize_question(value["question"])
            if question != value["question"]:
                raise ValueError
            evidence = value["evidence"]
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
                raise ValueError
            labels: list[str] = []
            total = 0
            for item in evidence:
                if not isinstance(item, dict) or set(item) != {"source_id", "text"}:
                    raise ValueError
                label, text = item["source_id"], item["text"]
                if (
                    not isinstance(label, str)
                    or SOURCE_LABEL.fullmatch(label) is None
                    or label in labels
                    or not isinstance(text, str)
                ):
                    raise ValueError
                validate_safe_text(text, field="evidence", max_chars=1200)
                labels.append(label)
                total += len(text)
            if labels != [f"S{index}" for index in range(1, len(labels) + 1)] or total > 6000:
                raise ValueError
            if (
                isinstance(value["seed"], bool)
                or not isinstance(value["seed"], int)
                or not 0 <= value["seed"] < 1 << 63
            ):
                raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid request") from None
