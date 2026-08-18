"""Private production adapter-generation HTTP boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_REVISION
from app.adapter_runtime_contract import ADAPTER_RUNTIME_CONTRACT_VERSION
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

_SHA = re.compile(r"^[0-9a-f]{64}$")
_TARGET_FIELDS = (
    "department_id",
    "target_kind",
    "deployment_id",
    "deployment_version",
    "deployment_row_version",
    "base_model_id",
    "base_model_revision",
    "adapter_id",
    "adapter_version",
    "review_id",
    "review_version",
    "evaluation_id",
    "evaluation_version",
    "suite_id",
    "suite_version",
    "registry_attempt_id",
    "registry_attempt_version",
    "registry_publication_attempt_id",
    "registry_attempt_number",
    "registry_execution_scope_id",
    "registry_manifest_sha256",
    "adapter_config_sha256",
    "adapter_config_byte_size",
    "adapter_model_sha256",
    "adapter_model_byte_size",
    "dependency_id",
    "dependency_version",
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = AdapterRuntimeSettings.from_environment()
    supervisor = AdapterRuntimeSupervisor(settings)
    await supervisor.start()
    application.state.settings = settings
    application.state.supervisor = supervisor
    try:
        yield
    finally:
        await supervisor.close()


app = FastAPI(title="DeptSLM private production adapter runtime", lifespan=lifespan)


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
        result = await request.app.state.supervisor.request(
            {"operation": "generate", "target": value}
        )
    except AdapterRuntimeSupervisorError as error:
        if error.code == "adapter_runtime_target_mismatch":
            return JSONResponse(status_code=503, content={"code": error.code})
        return JSONResponse(status_code=503, content={"code": error.code})
    # The supervisor has already compared this fingerprint with the loaded
    # child/session state.  Do not manufacture it by echoing the request.
    return result


def _authorize(request: Request) -> None:
    expected = f"Bearer {request.app.state.settings.token}".encode()
    if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected):
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


def _validate_request(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise HTTPException(400, "Invalid request")
    required = {
        "operation",
        "target",
        "runtime_contract_version",
        "target_fingerprint",
        *_TARGET_FIELDS,
        "question",
        "evidence",
        "prompt_version",
        "answer_contract_version",
    }
    if set(value) != required or value["operation"] != "generate" or value["target"] != "adapter":
        raise HTTPException(400, "Invalid request")
    if value["runtime_contract_version"] != ADAPTER_RUNTIME_CONTRACT_VERSION:
        raise HTTPException(400, "Invalid request")
    if (
        value["target_kind"] != "adapter"
        or value["base_model_id"] != BASE_MODEL_ID
        or value["base_model_revision"] != BASE_MODEL_REVISION
    ):
        raise HTTPException(400, "Invalid request")
    if not isinstance(value["target_fingerprint"], str) or not _SHA.fullmatch(
        value["target_fingerprint"]
    ):
        raise HTTPException(400, "Invalid request")
    canonical = {name: value[name] for name in _TARGET_FIELDS}
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    if hashlib.sha256(encoded).hexdigest() != value["target_fingerprint"]:
        raise HTTPException(400, "Invalid request")
    for name in (
        "department_id",
        "adapter_id",
        "deployment_id",
        "review_id",
        "evaluation_id",
        "suite_id",
        "registry_attempt_id",
        "registry_publication_attempt_id",
        "registry_execution_scope_id",
        "dependency_id",
    ):
        try:
            parsed = UUID(str(value[name]))
            if value[name] is None or parsed.int == 0 or str(parsed) != value[name]:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid request") from None
    for name in (
        "registry_manifest_sha256",
        "adapter_config_sha256",
        "adapter_model_sha256",
    ):
        if not isinstance(value[name], str) or not _SHA.fullmatch(value[name]):
            raise HTTPException(400, "Invalid request")
    for name in (
        "adapter_version",
        "deployment_version",
        "deployment_row_version",
        "review_version",
        "evaluation_version",
        "suite_version",
        "registry_attempt_version",
        "registry_attempt_number",
        "adapter_config_byte_size",
        "adapter_model_byte_size",
        "dependency_version",
    ):
        if type(value[name]) is not int or value[name] <= 0:
            raise HTTPException(400, "Invalid request")
    if (
        value["prompt_version"] != PROMPT_VERSION
        or value["answer_contract_version"] != ANSWER_CONTRACT_VERSION
    ):
        raise HTTPException(400, "Invalid request")
    try:
        question = normalize_question(value["question"])
        if question != value["question"] or not isinstance(value["evidence"], list):
            raise ValueError
        labels: list[str] = []
        total = 0
        for item in value["evidence"]:
            if not isinstance(item, dict) or set(item) != {"source_id", "text"}:
                raise ValueError
            label, text = item["source_id"], item["text"]
            if (
                not isinstance(label, str)
                or SOURCE_LABEL.fullmatch(label) is None
                or label in labels
            ):
                raise ValueError
            validate_safe_text(text, field="evidence", max_chars=1200)
            labels.append(label)
            total += len(text)
        if (
            not 1 <= len(labels) <= 8
            or labels != [f"S{i}" for i in range(1, len(labels) + 1)]
            or total > 6000
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid request") from None
    return value
