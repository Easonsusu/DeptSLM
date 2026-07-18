"""Fail-closed runtime configuration with no database or Qdrant settings."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.rag_domain import GENERATION_MODEL_REVISION
from app.vector_index_domain import EMBEDDING_MODEL_REVISION

PLACEHOLDERS = frozenset({"changeme", "change-me", "placeholder", "secret", "token"})


class RuntimeConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    data_dir: Path
    token: str
    provider: str
    environment: str
    max_concurrency: int

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        if any(
            os.getenv(name)
            for name in ("DATABASE_URL", "DEPTSLM_QDRANT_URL", "DEPTSLM_QDRANT_API_KEY")
        ):
            raise RuntimeConfigurationError(
                "Database and Qdrant configuration is forbidden."
            )
        root = Path(os.getenv("DEPTSLM_DATA_DIR", "").strip()).expanduser()
        if not root.is_absolute():
            raise RuntimeConfigurationError(
                "DEPTSLM_DATA_DIR must be an absolute runtime root."
            )
        try:
            metadata = root.lstat()
            cache_metadata = (root / "model_cache").lstat()
        except FileNotFoundError as error:
            raise RuntimeConfigurationError(
                "External model cache is unavailable."
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(cache_metadata.st_mode)
            or not stat.S_ISDIR(cache_metadata.st_mode)
        ):
            raise RuntimeConfigurationError("External model cache is unsafe.")
        resolved = root.resolve(strict=True)
        repository = _find_repository_root(Path(__file__).resolve())
        if repository is not None and (
            resolved == repository
            or repository in resolved.parents
            or resolved in repository.parents
        ):
            raise RuntimeConfigurationError(
                "DEPTSLM_DATA_DIR must be external to the repository."
            )
        token = os.getenv("DEPTSLM_RAG_RUNTIME_TOKEN", "")
        if (
            token != token.strip()
            or len(token) < 32
            or token.casefold() in PLACEHOLDERS
            or any(character.isspace() for character in token)
        ):
            raise RuntimeConfigurationError(
                "Internal runtime token is missing or unsafe."
            )
        environment = os.getenv("ENVIRONMENT", "").strip().lower()
        if environment not in {
            "local",
            "development",
            "dev",
            "test",
            "preview",
            "staging",
            "production",
        }:
            raise RuntimeConfigurationError("ENVIRONMENT must be explicit.")
        provider = os.getenv("DEPTSLM_RAG_RUNTIME_PROVIDER", "real").strip().lower()
        if provider not in {"real", "fake"} or (
            provider == "fake" and environment != "test"
        ):
            raise RuntimeConfigurationError("Fake model runtime is test-only.")
        if (
            os.getenv("DEPTSLM_EMBEDDING_MODEL_REVISION", "")
            != EMBEDDING_MODEL_REVISION
        ):
            raise RuntimeConfigurationError(
                "Embedding revision does not match the contract."
            )
        if (
            os.getenv("DEPTSLM_GENERATION_MODEL_REVISION", "")
            != GENERATION_MODEL_REVISION
        ):
            raise RuntimeConfigurationError(
                "Generation revision does not match the contract."
            )
        raw_concurrency = os.getenv("DEPTSLM_RAG_RUNTIME_MAX_CONCURRENCY", "1")
        if not raw_concurrency.isascii() or not raw_concurrency.isdecimal():
            raise RuntimeConfigurationError(
                "Runtime concurrency must be an ASCII decimal."
            )
        concurrency = int(raw_concurrency)
        if not 1 <= concurrency <= 4:
            raise RuntimeConfigurationError(
                "Runtime concurrency is outside reviewed bounds."
            )
        return cls(resolved, token, provider, environment, concurrency)


def _find_repository_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (
            (candidate / "AGENTS.md").is_file()
            and (candidate / "apps" / "api").is_dir()
        ):
            return candidate.resolve()
    return None
