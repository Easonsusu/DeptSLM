"""Behavioral storage and argument-forwarding tests for worker entrypoints."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKER_ENTRYPOINTS = (
    REPOSITORY_ROOT / "services" / "rag-worker" / "entrypoint.sh",
    REPOSITORY_ROOT / "services" / "training-worker" / "entrypoint.sh",
)


def run_entrypoint(entrypoint: Path, data_dir: str | None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if data_dir is None:
        environment.pop("DEPTSLM_DATA_DIR", None)
    else:
        environment["DEPTSLM_DATA_DIR"] = data_dir

    arguments = [str(entrypoint)]
    if entrypoint.parent.name == "rag-worker" or not entrypoint.with_name("worker.py").exists():
        arguments.extend([sys.executable, "-c", "print('worker-forwarded')"])
    return subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def copy_worker_source(entrypoint: Path, destination: Path) -> Path:
    """Copy a worker into a container-like source tree without Git metadata."""

    destination.mkdir()
    copied_entrypoint = destination / "entrypoint.sh"
    shutil.copy2(entrypoint, copied_entrypoint)
    worker_file = entrypoint.with_name("worker.py")
    if worker_file.exists():
        shutil.copy2(worker_file, destination / "worker.py")
    return copied_entrypoint


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_accepts_temporary_storage(entrypoint: Path, tmp_path: Path) -> None:
    result = run_entrypoint(entrypoint, str(tmp_path))

    assert result.returncode == 0
    expected = "worker-forwarded" if entrypoint.parent.name == "rag-worker" else "placeholder"
    assert expected in result.stdout


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_requires_storage(entrypoint: Path) -> None:
    result = run_entrypoint(entrypoint, None)

    assert result.returncode != 0
    assert "DEPTSLM_DATA_DIR is required" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_rejects_relative_storage(entrypoint: Path) -> None:
    result = run_entrypoint(entrypoint, "runtime-data")

    assert result.returncode != 0
    assert "must be an absolute path" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_rejects_missing_storage(entrypoint: Path, tmp_path: Path) -> None:
    result = run_entrypoint(entrypoint, str(tmp_path / "missing"))

    assert result.returncode != 0
    assert "does not exist or is not a directory" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_rejects_filesystem_root(entrypoint: Path) -> None:
    result = run_entrypoint(entrypoint, "/")

    assert result.returncode != 0
    assert "must not be the filesystem root" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_rejects_repository_storage(entrypoint: Path) -> None:
    result = run_entrypoint(entrypoint, str(REPOSITORY_ROOT))

    assert result.returncode != 0
    assert "must be outside the source repository" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_rejects_repository_ancestor(entrypoint: Path) -> None:
    result = run_entrypoint(entrypoint, str(REPOSITORY_ROOT.parent))

    assert result.returncode != 0
    assert "must not contain the source repository" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_worker_rejects_unwritable_storage(entrypoint: Path, tmp_path: Path) -> None:
    data_dir = tmp_path / "read-only"
    data_dir.mkdir()
    data_dir.chmod(0o500)

    if os.access(data_dir, os.W_OK):
        data_dir.chmod(0o700)
        pytest.skip("current user can write to a mode-0500 directory")

    try:
        result = run_entrypoint(entrypoint, str(data_dir))
    finally:
        data_dir.chmod(0o700)

    assert result.returncode != 0
    assert "is not writable and searchable" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_copied_worker_accepts_external_sibling_storage(entrypoint: Path, tmp_path: Path) -> None:
    copied_entrypoint = copy_worker_source(entrypoint, tmp_path / "container-app")
    data_dir = tmp_path / "runtime-data"
    data_dir.mkdir()

    result = run_entrypoint(copied_entrypoint, str(data_dir))

    assert result.returncode == 0
    expected = (
        "worker-forwarded"
        if not copied_entrypoint.with_name("worker.py").exists()
        else "placeholder"
    )
    assert expected in result.stdout


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
@pytest.mark.parametrize("data_dir_name", (None, "runtime-data"))
def test_copied_worker_rejects_source_tree_storage(
    entrypoint: Path, tmp_path: Path, data_dir_name: str | None
) -> None:
    source_dir = tmp_path / "container-app"
    copied_entrypoint = copy_worker_source(entrypoint, source_dir)
    data_dir = source_dir if data_dir_name is None else source_dir / data_dir_name
    data_dir.mkdir(exist_ok=True)

    result = run_entrypoint(copied_entrypoint, str(data_dir))

    assert result.returncode != 0
    assert "must be outside the worker source tree" in result.stderr


@pytest.mark.parametrize("entrypoint", WORKER_ENTRYPOINTS)
def test_copied_worker_rejects_source_tree_ancestor(entrypoint: Path, tmp_path: Path) -> None:
    copied_entrypoint = copy_worker_source(entrypoint, tmp_path / "container-app")

    result = run_entrypoint(copied_entrypoint, str(tmp_path))

    assert result.returncode != 0
    assert "must not contain the worker source tree" in result.stderr
