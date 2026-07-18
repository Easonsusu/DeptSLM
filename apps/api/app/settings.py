"""Runtime configuration for the DeptSLM API."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.rag_settings import RagConfigurationError, RagSettings

DEFAULT_DOCUMENT_MAX_BYTES = 26_214_400
DEFAULT_DEPARTMENT_DOCUMENT_QUOTA_BYTES = 1_073_741_824
DOCUMENT_MAX_BYTES_HARD_LIMIT = 104_857_600

ALLOWED_HS256_ENVIRONMENTS = frozenset({"local", "development", "dev", "test"})
DISALLOWED_AUTH_SECRET_PLACEHOLDERS = frozenset(
    {
        "changeme",
        "change-me",
        "example",
        "replace-me",
        "replace-with-a-local-development-secret",
        "secret",
        "your-secret",
    }
)


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings loaded from the process environment."""

    data_dir: Path
    database_url: str
    environment: str
    auth_mode: str
    auth_issuer: str | None
    auth_audience: str | None
    auth_secret: str | None
    document_max_bytes: int
    department_document_quota_bytes: int
    rag: RagSettings | None

    @classmethod
    def from_environment(cls) -> Settings:
        """Load settings without falling back to repository-local storage."""

        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        if not raw_data_dir:
            raise ConfigurationError(
                "DEPTSLM_DATA_DIR is required. Point it to the external DeptSLM "
                "runtime folder (for example, the DeptSLM folder in Google Drive)."
            )

        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute():
            raise ConfigurationError(
                "DEPTSLM_DATA_DIR must be an absolute path outside the source repository."
            )

        if not data_dir.is_dir():
            raise ConfigurationError(
                f"DEPTSLM_DATA_DIR does not exist or is not a directory: {data_dir}"
            )

        normalized_data_dir = Path(os.path.abspath(data_dir))
        resolved_data_dir = data_dir.resolve()

        if resolved_data_dir == Path(resolved_data_dir.anchor):
            raise ConfigurationError("DEPTSLM_DATA_DIR must not be the filesystem root.")

        source_file = Path(__file__).resolve()
        source_roots = {_application_source_root(source_file)}

        repository_root = _find_repository_root(source_file)
        if repository_root is not None:
            source_roots.add(repository_root)

        monorepo_root = _find_monorepo_root(source_file)
        if monorepo_root is not None:
            source_roots.add(monorepo_root)

        if any(
            _paths_overlap(candidate, source_root)
            for candidate in (normalized_data_dir, resolved_data_dir)
            for source_root in source_roots
        ):
            raise ConfigurationError(
                "DEPTSLM_DATA_DIR must be outside the source repository and must not "
                "be one of its ancestors; "
                f"received: {resolved_data_dir}"
            )

        if not os.access(resolved_data_dir, os.W_OK | os.X_OK):
            raise ConfigurationError(
                "DEPTSLM_DATA_DIR must be writable and searchable by the current process; "
                f"received: {resolved_data_dir}"
            )

        _validate_uploads_root(resolved_data_dir)

        document_max_bytes = _positive_ascii_decimal(
            "DEPTSLM_DOCUMENT_MAX_BYTES", DEFAULT_DOCUMENT_MAX_BYTES
        )
        if document_max_bytes > DOCUMENT_MAX_BYTES_HARD_LIMIT:
            raise ConfigurationError("DEPTSLM_DOCUMENT_MAX_BYTES must not exceed 104857600 bytes.")
        department_document_quota_bytes = _positive_ascii_decimal(
            "DEPTSLM_DEPARTMENT_DOCUMENT_QUOTA_BYTES",
            DEFAULT_DEPARTMENT_DOCUMENT_QUOTA_BYTES,
        )
        if department_document_quota_bytes < document_max_bytes:
            raise ConfigurationError(
                "DEPTSLM_DEPARTMENT_DOCUMENT_QUOTA_BYTES must be greater than or equal "
                "to DEPTSLM_DOCUMENT_MAX_BYTES."
            )

        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigurationError("DATABASE_URL is required.")
        if not database_url.startswith("postgresql+psycopg://"):
            raise ConfigurationError("DATABASE_URL must use the postgresql+psycopg driver.")

        raw_environment = os.getenv("ENVIRONMENT")
        environment = (raw_environment or "local").strip() or "local"
        auth_mode = os.getenv("DEPTSLM_AUTH_MODE", "disabled").strip().lower()
        if auth_mode not in {"disabled", "hs256"}:
            raise ConfigurationError("DEPTSLM_AUTH_MODE must be 'disabled' or 'hs256'.")

        auth_issuer = _optional_environment("DEPTSLM_AUTH_ISSUER")
        auth_audience = _optional_environment("DEPTSLM_AUTH_AUDIENCE")
        auth_secret = _optional_environment("DEPTSLM_AUTH_SECRET")
        if auth_mode == "hs256":
            environment = _validate_hs256_environment(raw_environment)
            _validate_hs256_configuration(auth_issuer, auth_audience, auth_secret)

        try:
            rag = RagSettings.optional_from_environment(environment)
        except RagConfigurationError as error:
            raise ConfigurationError(str(error)) from error
        if rag is not None:
            _validate_extracted_root(resolved_data_dir)

        return cls(
            data_dir=resolved_data_dir,
            database_url=database_url,
            environment=environment,
            auth_mode=auth_mode,
            auth_issuer=auth_issuer,
            auth_audience=auth_audience,
            auth_secret=auth_secret,
            document_max_bytes=document_max_bytes,
            department_document_quota_bytes=department_document_quota_bytes,
            rag=rag,
        )


def _positive_ascii_decimal(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise ConfigurationError(f"{name} must be a positive ASCII decimal integer.")
    value = int(raw)
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _validate_uploads_root(data_dir: Path) -> None:
    uploads = data_dir / "uploads"
    try:
        metadata = uploads.lstat()
    except FileNotFoundError as error:
        raise ConfigurationError(
            "DEPTSLM_DATA_DIR/uploads must already exist as a real writable directory."
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ConfigurationError(
            "DEPTSLM_DATA_DIR/uploads must be a real directory, not a symlink."
        )
    if not os.access(uploads, os.W_OK | os.X_OK):
        raise ConfigurationError("DEPTSLM_DATA_DIR/uploads must be writable and searchable.")


def _validate_extracted_root(data_dir: Path) -> None:
    extracted = data_dir / "extracted_text"
    try:
        metadata = extracted.lstat()
    except FileNotFoundError as error:
        raise ConfigurationError(
            "DEPTSLM_DATA_DIR/extracted_text must already exist as a real directory."
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ConfigurationError(
            "DEPTSLM_DATA_DIR/extracted_text must be a real directory, not a symlink."
        )
    if not os.access(extracted, os.R_OK | os.X_OK):
        raise ConfigurationError("DEPTSLM_DATA_DIR/extracted_text must be readable and searchable.")


def _optional_environment(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _validate_hs256_environment(raw_environment: str | None) -> str:
    if raw_environment is None or not raw_environment.strip():
        raise ConfigurationError("ENVIRONMENT must be explicitly set when DEPTSLM_AUTH_MODE=hs256.")
    environment = raw_environment.strip()
    if environment not in ALLOWED_HS256_ENVIRONMENTS:
        allowed = ", ".join(sorted(ALLOWED_HS256_ENVIRONMENTS))
        raise ConfigurationError(
            "Development HS256 authentication is allowed only when ENVIRONMENT is "
            f"one of: {allowed}."
        )
    return environment


def _validate_hs256_configuration(
    issuer: str | None, audience: str | None, secret: str | None
) -> None:
    missing = [
        name
        for name, value in (
            ("DEPTSLM_AUTH_ISSUER", issuer),
            ("DEPTSLM_AUTH_AUDIENCE", audience),
            ("DEPTSLM_AUTH_SECRET", secret),
        )
        if value is None
    ]
    if missing:
        raise ConfigurationError("HS256 authentication requires: " + ", ".join(missing) + ".")
    if secret is None:
        raise ConfigurationError("HS256 authentication requires DEPTSLM_AUTH_SECRET.")
    if len(secret.encode("utf-8")) < 32:
        raise ConfigurationError("DEPTSLM_AUTH_SECRET must contain at least 32 UTF-8 bytes.")
    if secret.casefold() in DISALLOWED_AUTH_SECRET_PLACEHOLDERS:
        raise ConfigurationError("DEPTSLM_AUTH_SECRET must not use a placeholder value.")


def _find_repository_root(start: Path) -> Path | None:
    """Return the nearest Git checkout root when running from source."""

    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate.resolve()
    return None


def _find_monorepo_root(source_file: Path) -> Path | None:
    """Find the DeptSLM root without relying on Git metadata being present."""

    relative_source_file = Path("apps/api/app/settings.py")
    for candidate in source_file.parents:
        if candidate / relative_source_file == source_file:
            return candidate.resolve()
    return None


def _application_source_root(source_file: Path) -> Path:
    """Return the API source root, including the flattened Docker image layout."""

    return source_file.parents[1].resolve()


def _paths_overlap(left: Path, right: Path) -> bool:
    """Return whether either path contains the other."""

    return left == right or left.is_relative_to(right) or right.is_relative_to(left)
