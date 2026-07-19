"""Validated fail-closed API settings for Phase 7 retrieval and model runtime."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from app.vector_index_domain import QDRANT_COLLECTION

LOCAL_ENVIRONMENTS = frozenset({"local", "development", "dev", "test"})
PLACEHOLDERS = frozenset({"changeme", "change-me", "placeholder", "secret", "token", "qdrant-key"})


class RagConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RagSettings:
    qdrant_url: str
    qdrant_api_key: str
    qdrant_timeout_seconds: int
    runtime_url: str
    runtime_token: str
    candidate_limit: int
    max_sources: int
    max_sources_per_document: int
    max_evidence_chars: int
    minimum_score: Decimal
    request_timeout_seconds: int

    @classmethod
    def optional_from_environment(cls, environment: str) -> RagSettings | None:
        required = {
            "DEPTSLM_QDRANT_URL": os.getenv("DEPTSLM_QDRANT_URL", ""),
            "DEPTSLM_QDRANT_API_KEY": os.getenv("DEPTSLM_QDRANT_API_KEY", ""),
            "DEPTSLM_RAG_RUNTIME_URL": os.getenv("DEPTSLM_RAG_RUNTIME_URL", ""),
            "DEPTSLM_RAG_RUNTIME_TOKEN": os.getenv("DEPTSLM_RAG_RUNTIME_TOKEN", ""),
        }
        if not any(value for value in required.values()):
            return None
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RagConfigurationError("Phase 7 RAG settings require: " + ", ".join(missing))
        collection = os.getenv("DEPTSLM_QDRANT_COLLECTION", QDRANT_COLLECTION)
        if collection != QDRANT_COLLECTION:
            raise RagConfigurationError("DEPTSLM_QDRANT_COLLECTION does not match the contract.")
        token = _secret("DEPTSLM_RAG_RUNTIME_TOKEN", required["DEPTSLM_RAG_RUNTIME_TOKEN"], 32)
        qdrant_key = _secret("DEPTSLM_QDRANT_API_KEY", required["DEPTSLM_QDRANT_API_KEY"], 16)
        max_sources = _bounded("DEPTSLM_RAG_MAX_SOURCES", 8, 1, 8)
        per_document = _bounded("DEPTSLM_RAG_MAX_SOURCES_PER_DOCUMENT", 2, 1, 8)
        if per_document > max_sources:
            raise RagConfigurationError(
                "DEPTSLM_RAG_MAX_SOURCES_PER_DOCUMENT cannot exceed DEPTSLM_RAG_MAX_SOURCES."
            )
        return cls(
            qdrant_url=_safe_url(
                "DEPTSLM_QDRANT_URL",
                required["DEPTSLM_QDRANT_URL"],
                environment,
                {"qdrant", "localhost", "127.0.0.1", "::1"},
            ),
            qdrant_api_key=qdrant_key,
            qdrant_timeout_seconds=_bounded("DEPTSLM_QDRANT_TIMEOUT_SECONDS", 30, 1, 300),
            runtime_url=_safe_url(
                "DEPTSLM_RAG_RUNTIME_URL",
                required["DEPTSLM_RAG_RUNTIME_URL"],
                environment,
                {"rag-runtime", "localhost", "127.0.0.1", "::1"},
            ),
            runtime_token=token,
            candidate_limit=_bounded("DEPTSLM_RAG_CANDIDATE_LIMIT", 20, 1, 100),
            max_sources=max_sources,
            max_sources_per_document=per_document,
            max_evidence_chars=_bounded("DEPTSLM_RAG_MAX_EVIDENCE_CHARS", 6000, 1200, 6000),
            minimum_score=_score(),
            request_timeout_seconds=_bounded("DEPTSLM_RAG_REQUEST_TIMEOUT_SECONDS", 30, 1, 300),
        )


def _bounded(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise RagConfigurationError(f"{name} must be an ASCII decimal integer.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise RagConfigurationError(f"{name} is outside the reviewed bounds.")
    return value


def _score() -> Decimal:
    raw = os.getenv("DEPTSLM_RAG_MIN_SCORE", "0.45")
    if raw != raw.strip() or not raw.isascii():
        raise RagConfigurationError("DEPTSLM_RAG_MIN_SCORE is malformed.")
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise RagConfigurationError("DEPTSLM_RAG_MIN_SCORE is malformed.") from error
    if not math.isfinite(float(value)) or not Decimal("-1") <= value <= Decimal("1"):
        raise RagConfigurationError("DEPTSLM_RAG_MIN_SCORE is outside the reviewed bounds.")
    return value


def _secret(name: str, raw: str, minimum: int) -> str:
    if (
        raw != raw.strip()
        or len(raw) < minimum
        or raw.casefold() in PLACEHOLDERS
        or any(character.isspace() for character in raw)
    ):
        raise RagConfigurationError(f"{name} is missing or unsafe.")
    return raw


def _safe_url(name: str, raw: str, environment: str, local_hosts: set[str]) -> str:
    if raw != raw.strip() or any(character.isspace() for character in raw):
        raise RagConfigurationError(f"{name} is malformed.")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RagConfigurationError(f"{name} is unsafe.")
    if parsed.scheme == "http" and (
        environment not in LOCAL_ENVIRONMENTS or parsed.hostname not in local_hosts
    ):
        raise RagConfigurationError(f"Plain HTTP {name} is limited to reviewed local hosts.")
    return raw.rstrip("/")
