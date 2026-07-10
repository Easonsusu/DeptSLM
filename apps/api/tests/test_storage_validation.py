"""Behavioral tests for the host storage validator."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_data_dir.sh"
REQUIRED_DIRECTORIES = (
    "uploads",
    "extracted_text",
    "vector_snapshots",
    "training_datasets",
    "adapters",
    "model_cache",
    "eval_results",
    "logs",
    "exports",
    "service_state/postgres",
    "service_state/qdrant",
)


def run_validator(
    data_dir: str | None, *, require_compose_layout: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the validator with an isolated environment value."""

    environment = os.environ.copy()
    if data_dir is None:
        environment.pop("DEPTSLM_DATA_DIR", None)
    else:
        environment["DEPTSLM_DATA_DIR"] = data_dir

    command = [str(VALIDATOR)]
    if require_compose_layout:
        command.append("--require-compose-layout")

    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
    )


def create_compose_layout(root: Path) -> None:
    """Create only the empty directories required by Compose."""

    for relative_directory in REQUIRED_DIRECTORIES:
        (root / relative_directory).mkdir(parents=True, exist_ok=True)


def test_validator_accepts_complete_temporary_layout(tmp_path: Path) -> None:
    create_compose_layout(tmp_path)

    result = run_validator(str(tmp_path), require_compose_layout=True)

    assert result.returncode == 0
    assert Path(result.stdout.strip()) == tmp_path.resolve()


def test_validator_requires_data_directory() -> None:
    result = run_validator(None)

    assert result.returncode != 0
    assert "DEPTSLM_DATA_DIR is required" in result.stderr


def test_validator_rejects_relative_data_directory() -> None:
    result = run_validator("runtime-data")

    assert result.returncode != 0
    assert "must be an absolute path" in result.stderr


def test_validator_rejects_filesystem_root() -> None:
    result = run_validator("/")

    assert result.returncode != 0
    assert "must not be the filesystem root" in result.stderr


@pytest.mark.parametrize("data_dir", (REPOSITORY_ROOT, REPOSITORY_ROOT.parent))
def test_validator_rejects_source_overlap(data_dir: Path) -> None:
    result = run_validator(str(data_dir))

    assert result.returncode != 0
    assert "source repository" in result.stderr


def test_validator_rejects_symlink_into_repository(tmp_path: Path) -> None:
    repository_link = tmp_path / "repository-link"
    repository_link.symlink_to(REPOSITORY_ROOT, target_is_directory=True)

    result = run_validator(str(repository_link))

    assert result.returncode != 0
    assert "must be outside the source repository" in result.stderr


def test_validator_rejects_incomplete_compose_layout(tmp_path: Path) -> None:
    result = run_validator(str(tmp_path), require_compose_layout=True)

    assert result.returncode != 0
    assert "required Compose storage directory is missing" in result.stderr


def test_validator_rejects_unwritable_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "read-only"
    data_dir.mkdir()
    data_dir.chmod(0o500)

    if os.access(data_dir, os.W_OK):
        data_dir.chmod(0o700)
        pytest.skip("current user can write to a mode-0500 directory")

    try:
        result = run_validator(str(data_dir))
    finally:
        data_dir.chmod(0o700)

    assert result.returncode != 0
    assert "not writable and searchable" in result.stderr
