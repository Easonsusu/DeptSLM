"""Fail-closed settings for the extraction worker only."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXTRACTION_TIMEOUT_SECONDS = 120
DEFAULT_MAX_EXTRACTED_BYTES = 104_857_600
DEFAULT_MAX_PDF_PAGES = 1_000
DEFAULT_CHUNK_MAX_CHARS = 1_200
DEFAULT_CHUNK_OVERLAP_CHARS = 200
DEFAULT_MAX_CHUNKS_PER_DOCUMENT = 100_000
DEFAULT_EXTRACTION_LEASE_SECONDS = 300
DEFAULT_WORKER_POLL_SECONDS = 5
DEFAULT_DEPARTMENT_EXTRACTED_QUOTA_BYTES = 4_294_967_296


class WorkerConfigurationError(RuntimeError):
    """Raised when worker configuration is absent or unsafe."""


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    data_dir: Path
    database_url: str
    extraction_timeout_seconds: int
    max_extracted_bytes: int
    max_pdf_pages: int
    chunk_max_chars: int
    chunk_overlap_chars: int
    max_chunks_per_document: int
    extraction_lease_seconds: int
    worker_poll_seconds: int
    department_extracted_quota_bytes: int

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        raw_root = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        if not raw_root:
            raise WorkerConfigurationError("DEPTSLM_DATA_DIR is required for the worker.")
        root = Path(raw_root).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise WorkerConfigurationError(
                "DEPTSLM_DATA_DIR must be an existing absolute directory."
            )
        if stat.S_ISLNK(root.lstat().st_mode):
            raise WorkerConfigurationError("DEPTSLM_DATA_DIR must not be a symlink.")
        root = root.resolve(strict=True)
        if root == Path(root.anchor):
            raise WorkerConfigurationError("DEPTSLM_DATA_DIR must not be the filesystem root.")
        repository = _find_repository_root(Path.cwd()) or _find_repository_root(
            Path(__file__).resolve()
        )
        if repository is not None and _overlaps(root, repository):
            raise WorkerConfigurationError(
                "DEPTSLM_DATA_DIR must be outside the source repository."
            )
        _require_real_directory(root / "uploads", writable=False)
        _require_real_directory(root / "extracted_text", writable=True)

        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url or not database_url.startswith("postgresql+psycopg://"):
            raise WorkerConfigurationError(
                "DATABASE_URL is required and must use postgresql+psycopg."
            )

        timeout = _bounded("DEPTSLM_EXTRACTION_TIMEOUT_SECONDS", 120, 1, 600)
        maximum = _bounded(
            "DEPTSLM_MAX_EXTRACTED_BYTES", DEFAULT_MAX_EXTRACTED_BYTES, 1, 524_288_000
        )
        pages = _bounded("DEPTSLM_MAX_PDF_PAGES", DEFAULT_MAX_PDF_PAGES, 1, 5_000)
        chunk_size = _bounded("DEPTSLM_CHUNK_MAX_CHARS", DEFAULT_CHUNK_MAX_CHARS, 256, 8_192)
        overlap = _bounded(
            "DEPTSLM_CHUNK_OVERLAP_CHARS",
            DEFAULT_CHUNK_OVERLAP_CHARS,
            0,
            4_096,
            allow_zero=True,
        )
        chunk_count = _bounded(
            "DEPTSLM_MAX_CHUNKS_PER_DOCUMENT",
            DEFAULT_MAX_CHUNKS_PER_DOCUMENT,
            1,
            1_000_000,
        )
        lease = _bounded("DEPTSLM_EXTRACTION_LEASE_SECONDS", 300, 1, 3_600)
        poll = _bounded("DEPTSLM_WORKER_POLL_SECONDS", 5, 1, 60)
        quota = _bounded(
            "DEPTSLM_DEPARTMENT_EXTRACTED_QUOTA_BYTES",
            DEFAULT_DEPARTMENT_EXTRACTED_QUOTA_BYTES,
            1,
            1 << 50,
        )
        if overlap >= chunk_size or overlap > chunk_size // 2:
            raise WorkerConfigurationError(
                "DEPTSLM_CHUNK_OVERLAP_CHARS must not exceed half the chunk size."
            )
        if lease < timeout + 30:
            raise WorkerConfigurationError(
                "DEPTSLM_EXTRACTION_LEASE_SECONDS must exceed the timeout by 30 seconds."
            )
        if quota < maximum:
            raise WorkerConfigurationError(
                "DEPTSLM_DEPARTMENT_EXTRACTED_QUOTA_BYTES must be at least the output limit."
            )
        return cls(
            root,
            database_url,
            timeout,
            maximum,
            pages,
            chunk_size,
            overlap,
            chunk_count,
            lease,
            poll,
            quota,
        )


def _bounded(
    name: str, default: int, minimum: int, maximum: int, *, allow_zero: bool = False
) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise WorkerConfigurationError(f"{name} must be an ASCII decimal integer.")
    value = int(raw)
    if value == 0 and allow_zero:
        return value
    if value < minimum or value > maximum:
        raise WorkerConfigurationError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _require_real_directory(path: Path, *, writable: bool) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise WorkerConfigurationError(
            f"Required runtime directory is missing: {path.name}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise WorkerConfigurationError(f"Required runtime directory is unsafe: {path.name}")
    mode = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not os.access(path, mode):
        raise WorkerConfigurationError(f"Required runtime directory is unavailable: {path.name}")


def _find_repository_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (
            (candidate / "AGENTS.md").is_file() and (candidate / "apps" / "api").is_dir()
        ):
            return candidate.resolve()
    return None


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)
