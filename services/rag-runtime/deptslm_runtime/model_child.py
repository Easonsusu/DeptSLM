"""Persistent model-only child using a bounded length-delimited protocol."""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

from app.rag_domain import GENERATION_MODEL_REVISION, MAX_CHILD_FRAME_BYTES, normalize_question
from app.vector_index_domain import EMBEDDING_MODEL_REVISION
from deptslm_runtime.models import RuntimeModelError, RuntimeModels
from deptslm_runtime.settings import CHILD_ENVIRONMENT_NAMES

_HEADER = struct.Struct(">I")


def main() -> int:
    if not set(os.environ) <= CHILD_ENVIRONMENT_NAMES:
        return 2
    provider = os.getenv("DEPTSLM_RAG_RUNTIME_PROVIDER", "")
    environment = os.getenv("ENVIRONMENT", "")
    if provider not in {"real", "fake"} or (provider == "fake" and environment != "test"):
        return 2
    if (
        os.getenv("DEPTSLM_EMBEDDING_MODEL_REVISION") != EMBEDDING_MODEL_REVISION
        or os.getenv("DEPTSLM_GENERATION_MODEL_REVISION") != GENERATION_MODEL_REVISION
    ):
        return 2
    root = Path(os.getenv("DEPTSLM_DATA_DIR", ""))
    try:
        models = RuntimeModels(root, provider)
        _write_frame(sys.stdout.buffer, {"ready": True}, 4096)
        while True:
            request = _read_frame(sys.stdin.buffer, MAX_CHILD_FRAME_BYTES)
            if request is None:
                return 0
            try:
                result = _execute(models, request)
                response = {"ok": True, "result": result}
            except RuntimeModelError as error:
                response = {"ok": False, "code": error.code}
            except Exception:
                response = {"ok": False, "code": "model_operation_failed"}
            _write_frame(sys.stdout.buffer, response, 256 * 1024)
    except Exception:
        return 2


def _execute(models: RuntimeModels, request: Any) -> Any:
    if not isinstance(request, dict) or set(request) != {"operation", "payload"}:
        raise RuntimeModelError("invalid_request")
    operation, payload = request["operation"], request["payload"]
    if not isinstance(payload, dict):
        raise RuntimeModelError("invalid_request")
    if operation == "query_embedding" and set(payload) == {"question"}:
        question = _question(payload["question"])
        return {"vector": models.embed_question(question)}
    if operation == "generate" and set(payload) == {"question", "evidence"}:
        question = _question(payload["question"])
        evidence = payload["evidence"]
        if not isinstance(evidence, list):
            raise RuntimeModelError("invalid_request")
        return models.generate(question, evidence)
    raise RuntimeModelError("invalid_request")


def _question(value: Any) -> str:
    try:
        normalized = normalize_question(value)
    except (TypeError, ValueError) as error:
        raise RuntimeModelError("invalid_request") from error
    if normalized != value:
        raise RuntimeModelError("invalid_request")
    return normalized


def _read_frame(stream: BinaryIO, maximum: int) -> Any | None:
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) != _HEADER.size:
        raise RuntimeModelError("invalid_request")
    size = _HEADER.unpack(header)[0]
    if not 1 <= size <= maximum:
        raise RuntimeModelError("invalid_request")
    payload = stream.read(size)
    if len(payload) != size:
        raise RuntimeModelError("invalid_request")
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeModelError("invalid_request") from error


def _write_frame(stream: BinaryIO, value: Any, maximum: int) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(payload) <= maximum:
        raise RuntimeModelError("model_operation_failed")
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
