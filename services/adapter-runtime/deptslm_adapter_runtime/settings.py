"""Fail-closed production adapter-runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.adapter_contract import BASE_MODEL_REVISION


class AdapterRuntimeConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterRuntimeSettings:
    data_dir: Path
    model_cache: Path
    registry: Path
    token: str
    provider: str

    @classmethod
    def from_environment(cls) -> AdapterRuntimeSettings:
        environment = os.getenv("ENVIRONMENT", "").strip().lower()
        if environment not in {"test", "development", "staging", "production"}:
            raise AdapterRuntimeConfigurationError("ENVIRONMENT must be explicit.")
        provider = os.getenv("DEPTSLM_ADAPTER_RUNTIME_PROVIDER", "real").strip().lower()
        if provider not in {"real", "fake"} or (provider == "fake" and environment != "test"):
            raise AdapterRuntimeConfigurationError("Fake adapter runtime is test-only.")
        token = os.getenv("DEPTSLM_ADAPTER_RUNTIME_TOKEN", "")
        if len(token) < 32 or token != token.strip() or any(char.isspace() for char in token):
            raise AdapterRuntimeConfigurationError("Production runtime token is unsafe.")
        root = Path(os.getenv("DEPTSLM_DATA_DIR", "")).expanduser()
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise AdapterRuntimeConfigurationError("DEPTSLM_DATA_DIR is unavailable.")
        model_cache = root / "model_cache"
        registry = root / "adapters" / "registry"
        if (
            not model_cache.is_dir()
            or model_cache.is_symlink()
            or not registry.is_dir()
            or registry.is_symlink()
        ):
            raise AdapterRuntimeConfigurationError("Runtime mounts are unavailable.")
        if os.getenv("DEPTSLM_ADAPTER_RUNTIME_BASE_REVISION") != BASE_MODEL_REVISION:
            raise AdapterRuntimeConfigurationError("Base model revision is not reviewed.")
        forbidden = (
            "DATABASE_URL",
            "DEPTSLM_QDRANT_URL",
            "DEPTSLM_QDRANT_API_KEY",
            "DEPTSLM_AUTH_SECRET",
            "DEPTSLM_RAG_RUNTIME_TOKEN",
            "DEPTSLM_ADAPTER_EVAL_RUNTIME_TOKEN",
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        )
        if any(os.getenv(name) for name in forbidden):
            raise AdapterRuntimeConfigurationError("Forbidden runtime configuration is present.")
        return cls(root.resolve(), model_cache.resolve(), registry.resolve(), token, provider)

    def child_environment(self) -> dict[str, str]:
        return {
            "DEPTSLM_DATA_DIR": str(self.data_dir),
            "DEPTSLM_ADAPTER_RUNTIME_PROVIDER": self.provider,
            "DEPTSLM_ADAPTER_RUNTIME_BASE_REVISION": BASE_MODEL_REVISION,
            "ENVIRONMENT": os.getenv("ENVIRONMENT", ""),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
