"""Smoke tests for the Phase 0 API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.settings as settings_module
from app.main import app
from app.settings import ConfigurationError


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Start the API with isolated runtime storage."""

    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version(client: TestClient) -> None:
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"name": "DeptSLM", "version": "0.1.0"}


def test_startup_requires_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEPTSLM_DATA_DIR", raising=False)

    with pytest.raises(ConfigurationError, match="DEPTSLM_DATA_DIR is required"):
        with TestClient(app):
            pass


def test_startup_rejects_relative_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", "runtime-data")

    with pytest.raises(ConfigurationError, match="must be an absolute path"):
        with TestClient(app):
            pass


def test_startup_rejects_missing_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_directory = tmp_path / "missing"
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(missing_directory))

    with pytest.raises(ConfigurationError, match="does not exist or is not a directory"):
        with TestClient(app):
            pass


def test_startup_rejects_repository_data_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(repository_root))

    with pytest.raises(ConfigurationError, match="must be outside the source repository"):
        with TestClient(app):
            pass


def test_startup_rejects_filesystem_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(Path("/").resolve()))

    with pytest.raises(ConfigurationError, match="must not be the filesystem root"):
        with TestClient(app):
            pass


def test_startup_rejects_repository_ancestor(monkeypatch: pytest.MonkeyPatch) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(repository_root.parent))

    with pytest.raises(ConfigurationError, match="must not be one of its ancestors"):
        with TestClient(app):
            pass


def test_startup_rejects_resolved_symlink_into_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    repository_link = tmp_path / "repository-link"
    repository_link.symlink_to(repository_root, target_is_directory=True)
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(repository_link))

    with pytest.raises(ConfigurationError, match="must be outside the source repository"):
        with TestClient(app):
            pass


def test_startup_rejects_source_root_without_git_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    image_source_root = tmp_path / "image-app"
    image_package = image_source_root / "app"
    image_package.mkdir(parents=True)

    monkeypatch.setattr(settings_module, "__file__", str(image_package / "settings.py"))
    monkeypatch.setattr(settings_module, "_find_repository_root", lambda _: None)
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(image_source_root))

    with pytest.raises(ConfigurationError, match="must be outside the source repository"):
        with TestClient(app):
            pass


def test_startup_rejects_non_writable_data_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_module.os, "access", lambda *_: False)

    with pytest.raises(ConfigurationError, match="must be writable and searchable"):
        with TestClient(app):
            pass
