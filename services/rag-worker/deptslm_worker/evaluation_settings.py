"""Strict settings for the dedicated Phase 9 evaluator worker."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.evaluation_artifacts import EvaluationArtifactStore
from app.evaluation_domain import EvaluationContractError
from app.rag_settings import RagConfigurationError, RagSettings


class EvaluationConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EvaluationSettings:
    database_url: str
    data_dir: Path
    environment: str
    worker_id: UUID
    poll_seconds: int
    lease_seconds: int
    heartbeat_seconds: int
    operation_timeout_seconds: int
    code_revision: str
    rag: RagSettings

    @classmethod
    def from_environment(cls) -> EvaluationSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise EvaluationConfigurationError(
                "DATABASE_URL must use the postgresql+psycopg driver."
            )
        raw_root = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        if not raw_root:
            raise EvaluationConfigurationError("DEPTSLM_DATA_DIR is required.")
        root = Path(raw_root).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise EvaluationConfigurationError(
                "DEPTSLM_DATA_DIR must be an existing absolute directory."
            )
        root = root.resolve()
        _readable_directory(root / "extracted_text")
        try:
            EvaluationArtifactStore(root)
        except EvaluationContractError as error:
            raise EvaluationConfigurationError(
                "DEPTSLM_DATA_DIR/eval_results is unavailable."
            ) from error
        environment = os.getenv("ENVIRONMENT", "").strip()
        if not environment:
            raise EvaluationConfigurationError("ENVIRONMENT is required.")
        raw_worker_id = os.getenv("DEPTSLM_EVALUATION_WORKER_ID", "").strip()
        try:
            worker_id = UUID(raw_worker_id)
        except ValueError as error:
            raise EvaluationConfigurationError(
                "DEPTSLM_EVALUATION_WORKER_ID must be a UUID."
            ) from error
        if worker_id.int == 0:
            raise EvaluationConfigurationError("DEPTSLM_EVALUATION_WORKER_ID must be non-zero.")
        lease = _bounded("DEPTSLM_EVALUATION_LEASE_SECONDS", 300, 30, 3600)
        heartbeat = _bounded("DEPTSLM_EVALUATION_HEARTBEAT_SECONDS", 30, 1, 600)
        operation = _bounded("DEPTSLM_EVALUATION_OPERATION_TIMEOUT_SECONDS", 120, 1, 1800)
        if heartbeat >= lease:
            raise EvaluationConfigurationError(
                "DEPTSLM_EVALUATION_HEARTBEAT_SECONDS must be below the lease."
            )
        if operation >= lease:
            raise EvaluationConfigurationError(
                "DEPTSLM_EVALUATION_OPERATION_TIMEOUT_SECONDS must be below the lease."
            )
        revision = os.getenv("DEPTSLM_EVALUATION_CODE_REVISION", "").strip()
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise EvaluationConfigurationError(
                "DEPTSLM_EVALUATION_CODE_REVISION must be an exact lowercase Git SHA."
            )
        try:
            rag = RagSettings.optional_from_environment(environment)
        except RagConfigurationError as error:
            raise EvaluationConfigurationError(str(error)) from error
        if rag is None:
            raise EvaluationConfigurationError("Phase 7 RAG settings are required.")
        return cls(
            database_url=database_url,
            data_dir=root,
            environment=environment,
            worker_id=worker_id,
            poll_seconds=_bounded("DEPTSLM_EVALUATION_POLL_SECONDS", 5, 1, 300),
            lease_seconds=lease,
            heartbeat_seconds=heartbeat,
            operation_timeout_seconds=operation,
            code_revision=revision,
            rag=rag,
        )


def _bounded(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise EvaluationConfigurationError(f"{name} must be an ASCII decimal integer.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise EvaluationConfigurationError(f"{name} is outside the reviewed bounds.")
    return value


def _readable_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise EvaluationConfigurationError("DEPTSLM_DATA_DIR/extracted_text must exist.") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EvaluationConfigurationError(
            "DEPTSLM_DATA_DIR/extracted_text must be a real directory."
        )
    if not os.access(path, os.R_OK | os.X_OK):
        raise EvaluationConfigurationError("DEPTSLM_DATA_DIR/extracted_text must be readable.")
