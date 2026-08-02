"""Worker configuration boundary tests for Phase 12.1C."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from app.adapter_registry_worker import _settings


def _layout(root: Path) -> None:
    for path in (
        root / "adapters",
        root / "adapters" / "imports",
        root / "adapters" / "registry",
        root / "adapters" / ".staging",
        root / "adapters" / ".staging" / "registry",
    ):
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)


def test_worker_requires_storage_and_exact_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _layout(root)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/db")
        monkeypatch.setenv("DEPTSLM_DATA_DIR", str(root))
        monkeypatch.setenv("DEPTSLM_ADAPTER_REGISTRY_WORKER_ID", str(uuid4()))
        monkeypatch.setenv("DEPTSLM_ADAPTER_REGISTRY_CODE_REVISION", "a" * 40)
        settings = _settings()
        assert settings[1] == root
        monkeypatch.setenv("DEPTSLM_ADAPTER_REGISTRY_CODE_REVISION", "not-a-revision")
        with pytest.raises(ValueError):
            _settings()
