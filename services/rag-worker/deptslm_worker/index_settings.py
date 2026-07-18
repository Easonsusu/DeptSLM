"""Fail-closed settings for the Phase 6 indexing worker."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.vector_index_domain import (
    EMBEDDING_MODEL_REVISION,
    QDRANT_COLLECTION,
)

LOCAL_ENVIRONMENTS = frozenset({"local", "development", "dev", "test"})
KNOWN_ENVIRONMENTS = LOCAL_ENVIRONMENTS | frozenset(
    {"preview", "staging", "production"}
)
COLLECTION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,127}$")
PLACEHOLDERS = frozenset(
    {"changeme", "change-me", "placeholder", "secret", "qdrant-key"}
)


class IndexConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QdrantSettings:
    qdrant_url: str
    qdrant_api_key: str
    qdrant_collection: str
    qdrant_timeout_seconds: int
    environment: str

    @classmethod
    def from_environment(cls) -> QdrantSettings:
        environment = os.getenv("ENVIRONMENT", "").strip().lower()
        if environment not in KNOWN_ENVIRONMENTS:
            raise IndexConfigurationError(
                "ENVIRONMENT must be an explicit reviewed value."
            )
        qdrant_url = _qdrant_url(os.getenv("DEPTSLM_QDRANT_URL", ""), environment)
        qdrant_api_key = os.getenv("DEPTSLM_QDRANT_API_KEY", "").strip()
        if (
            len(qdrant_api_key) < 16
            or qdrant_api_key.lower() in PLACEHOLDERS
            or any(character.isspace() for character in qdrant_api_key)
        ):
            raise IndexConfigurationError(
                "DEPTSLM_QDRANT_API_KEY is missing or unsafe."
            )
        collection = os.getenv("DEPTSLM_QDRANT_COLLECTION", "").strip()
        if collection != QDRANT_COLLECTION or not COLLECTION_PATTERN.fullmatch(
            collection
        ):
            raise IndexConfigurationError(
                "DEPTSLM_QDRANT_COLLECTION does not match the contract."
            )
        return cls(
            qdrant_url=qdrant_url,
            qdrant_api_key=qdrant_api_key,
            qdrant_collection=collection,
            qdrant_timeout_seconds=_bounded(
                "DEPTSLM_QDRANT_TIMEOUT_SECONDS", 30, 1, 300
            ),
            environment=environment,
        )


@dataclass(frozen=True, slots=True)
class IndexSettings(QdrantSettings):
    data_dir: Path
    database_url: str
    lease_seconds: int
    poll_seconds: int
    batch_size: int
    max_batch_chars: int
    embedding_timeout_seconds: int
    embedding_model_revision: str
    embedding_provider: str

    @classmethod
    def from_environment(cls) -> IndexSettings:
        qdrant = QdrantSettings.from_environment()
        data_dir = _data_root(os.getenv("DEPTSLM_DATA_DIR", ""))
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise IndexConfigurationError(
                "DATABASE_URL is required and must use postgresql+psycopg."
            )
        revision = os.getenv("DEPTSLM_EMBEDDING_MODEL_REVISION", "").strip()
        if revision != EMBEDDING_MODEL_REVISION:
            raise IndexConfigurationError(
                "DEPTSLM_EMBEDDING_MODEL_REVISION does not match the reviewed revision."
            )
        provider = os.getenv("DEPTSLM_EMBEDDING_PROVIDER", "real").strip().lower()
        if provider not in {"real", "fake"}:
            raise IndexConfigurationError("DEPTSLM_EMBEDDING_PROVIDER is invalid.")
        if provider == "fake" and qdrant.environment != "test":
            raise IndexConfigurationError("Fake embeddings are permitted only in test.")
        timeout = _bounded("DEPTSLM_EMBEDDING_TIMEOUT_SECONDS", 120, 1, 3600)
        lease = _bounded("DEPTSLM_VECTOR_INDEX_LEASE_SECONDS", 300, 31, 7200)
        if lease < timeout + 30:
            raise IndexConfigurationError(
                "DEPTSLM_VECTOR_INDEX_LEASE_SECONDS must exceed embedding timeout by 30."
            )
        return cls(
            qdrant_url=qdrant.qdrant_url,
            qdrant_api_key=qdrant.qdrant_api_key,
            qdrant_collection=qdrant.qdrant_collection,
            qdrant_timeout_seconds=qdrant.qdrant_timeout_seconds,
            environment=qdrant.environment,
            data_dir=data_dir,
            database_url=database_url,
            lease_seconds=lease,
            poll_seconds=_bounded("DEPTSLM_VECTOR_INDEX_POLL_SECONDS", 5, 1, 60),
            batch_size=_bounded("DEPTSLM_EMBEDDING_BATCH_SIZE", 8, 1, 64),
            max_batch_chars=_bounded(
                "DEPTSLM_EMBEDDING_MAX_BATCH_CHARS", 8192, 256, 131072
            ),
            embedding_timeout_seconds=timeout,
            embedding_model_revision=revision,
            embedding_provider=provider,
        )


def _data_root(
    raw: str,
    *,
    required_directories: tuple[str, ...] = ("extracted_text", "model_cache"),
) -> Path:
    value = raw.strip()
    if not value:
        raise IndexConfigurationError("DEPTSLM_DATA_DIR is required.")
    root = Path(value).expanduser()
    if not root.is_absolute():
        raise IndexConfigurationError("DEPTSLM_DATA_DIR must be absolute.")
    try:
        metadata = root.lstat()
    except FileNotFoundError as error:
        raise IndexConfigurationError("DEPTSLM_DATA_DIR is unavailable.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise IndexConfigurationError("DEPTSLM_DATA_DIR must be a real directory.")
    resolved = root.resolve(strict=True)
    if resolved == Path(resolved.anchor):
        raise IndexConfigurationError(
            "DEPTSLM_DATA_DIR must not be the filesystem root."
        )
    repository = _find_repository_root(Path(__file__).resolve())
    if repository is not None and (
        resolved == repository
        or repository in resolved.parents
        or resolved in repository.parents
    ):
        raise IndexConfigurationError(
            "DEPTSLM_DATA_DIR must be external to the repository."
        )
    for name in required_directories:
        child = resolved / name
        try:
            child_metadata = child.lstat()
        except FileNotFoundError as error:
            raise IndexConfigurationError(
                f"Required runtime directory is missing: {name}"
            ) from error
        if stat.S_ISLNK(child_metadata.st_mode) or not stat.S_ISDIR(
            child_metadata.st_mode
        ):
            raise IndexConfigurationError(
                f"Required runtime directory is unsafe: {name}"
            )
    return resolved


def _qdrant_url(raw: str, environment: str) -> str:
    value = raw.strip()
    if not value or value != raw or any(character.isspace() for character in value):
        raise IndexConfigurationError("DEPTSLM_QDRANT_URL is missing or malformed.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.path not in {"", "/"})
    ):
        raise IndexConfigurationError("DEPTSLM_QDRANT_URL is unsafe.")
    if parsed.scheme == "http":
        local_hosts = {"127.0.0.1", "localhost", "::1", "qdrant"}
        if environment not in LOCAL_ENVIRONMENTS or parsed.hostname not in local_hosts:
            raise IndexConfigurationError(
                "Plain HTTP Qdrant is limited to reviewed local/test hosts."
            )
    return value.rstrip("/")


def _find_repository_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate.resolve()
    return None


def _bounded(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise IndexConfigurationError(f"{name} must be an ASCII decimal.")
    value = int(raw)
    if value < minimum or value > maximum:
        raise IndexConfigurationError(f"{name} is outside the reviewed bounds.")
    return value
