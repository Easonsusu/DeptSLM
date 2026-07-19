"""Fail-closed runtime configuration with no database or Qdrant settings."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.rag_domain import GENERATION_MODEL_REVISION
from app.vector_index_domain import EMBEDDING_MODEL_REVISION

PLACEHOLDERS = frozenset({"changeme", "change-me", "placeholder", "secret", "token"})
FORBIDDEN_SUPERVISOR_VARIABLES = frozenset(
    {
        "DATABASE_URL",
        "DATABASE_TEST_URL",
        "DEPTSLM_QDRANT_URL",
        "DEPTSLM_QDRANT_API_KEY",
        "DEPTSLM_AUTH_MODE",
        "DEPTSLM_AUTH_ISSUER",
        "DEPTSLM_AUTH_AUDIENCE",
        "DEPTSLM_AUTH_SECRET",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AZURE_CLIENT_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_API_KEY",
    }
)
CHILD_ENVIRONMENT_NAMES = frozenset(
    {
        "DEPTSLM_DATA_DIR",
        "DEPTSLM_RAG_RUNTIME_PROVIDER",
        "DEPTSLM_EMBEDDING_MODEL_REVISION",
        "DEPTSLM_GENERATION_MODEL_REVISION",
        "ENVIRONMENT",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "TOKENIZERS_PARALLELISM",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "__CF_USER_TEXT_ENCODING",
    }
)


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
        present = sorted(name for name in FORBIDDEN_SUPERVISOR_VARIABLES if os.getenv(name))
        if present:
            raise RuntimeConfigurationError("Forbidden runtime configuration is present.")
        root = Path(os.getenv("DEPTSLM_DATA_DIR", "").strip()).expanduser()
        if not root.is_absolute():
            raise RuntimeConfigurationError("DEPTSLM_DATA_DIR must be an absolute runtime root.")
        try:
            metadata = root.lstat()
            cache_metadata = (root / "model_cache").lstat()
        except FileNotFoundError as error:
            raise RuntimeConfigurationError("External model cache is unavailable.") from error
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
            raise RuntimeConfigurationError("DEPTSLM_DATA_DIR must be external to the repository.")
        token = os.getenv("DEPTSLM_RAG_RUNTIME_TOKEN", "")
        if (
            token != token.strip()
            or len(token) < 32
            or token.casefold() in PLACEHOLDERS
            or any(character.isspace() for character in token)
        ):
            raise RuntimeConfigurationError("Internal runtime token is missing or unsafe.")
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
        if provider not in {"real", "fake"} or (provider == "fake" and environment != "test"):
            raise RuntimeConfigurationError("Fake model runtime is test-only.")
        if os.getenv("DEPTSLM_EMBEDDING_MODEL_REVISION", "") != EMBEDDING_MODEL_REVISION:
            raise RuntimeConfigurationError("Embedding revision does not match the contract.")
        if os.getenv("DEPTSLM_GENERATION_MODEL_REVISION", "") != GENERATION_MODEL_REVISION:
            raise RuntimeConfigurationError("Generation revision does not match the contract.")
        raw_concurrency = os.getenv("DEPTSLM_RAG_RUNTIME_MAX_CONCURRENCY", "1")
        if not raw_concurrency.isascii() or not raw_concurrency.isdecimal():
            raise RuntimeConfigurationError("Runtime concurrency must be an ASCII decimal.")
        concurrency = int(raw_concurrency)
        if concurrency != 1:
            raise RuntimeConfigurationError("Runtime concurrency must remain one.")
        return cls(resolved, token, provider, environment, concurrency)

    def child_environment(self) -> dict[str, str]:
        """Build the exact model-child allowlist without the HTTP bearer token."""

        values = {
            "DEPTSLM_DATA_DIR": str(self.data_dir),
            "DEPTSLM_RAG_RUNTIME_PROVIDER": self.provider,
            "DEPTSLM_EMBEDDING_MODEL_REVISION": EMBEDDING_MODEL_REVISION,
            "DEPTSLM_GENERATION_MODEL_REVISION": GENERATION_MODEL_REVISION,
            "ENVIRONMENT": self.environment,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        source_path = _source_pythonpath(Path(__file__).resolve())
        if source_path is not None:
            values["PYTHONPATH"] = source_path
        if not set(values) <= CHILD_ENVIRONMENT_NAMES:
            raise RuntimeConfigurationError("Model child environment is outside the allowlist.")
        return values


def _find_repository_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (
            (candidate / "AGENTS.md").is_file() and (candidate / "apps" / "api").is_dir()
        ):
            return candidate.resolve()
    return None


def _source_pythonpath(start: Path) -> str | None:
    repository = _find_repository_root(start)
    if repository is None:
        return None
    roots = (
        repository / "apps" / "api",
        repository / "services" / "rag-worker",
        repository / "services" / "rag-runtime",
    )
    if not all(root.is_dir() for root in roots):
        return None
    return os.pathsep.join(str(root) for root in roots)
