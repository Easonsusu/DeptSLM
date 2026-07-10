"""Runtime configuration for the DeptSLM API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated settings loaded from the process environment."""

    data_dir: Path
    environment: str

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

        return cls(
            data_dir=resolved_data_dir,
            environment=os.getenv("ENVIRONMENT", "local").strip() or "local",
        )


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
