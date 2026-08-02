"""Descriptor and stale-surface tests for Phase 12.1C artifacts."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from app.adapter_registry_artifacts import (
    AdapterRegistryArtifactError,
    AdapterRegistryArtifactStore,
)
from app.authorization import DepartmentScope


def _layout(root: Path) -> None:
    for path in (
        root / "adapters",
        root / "adapters" / "imports",
        root / "adapters" / "registry",
        root / "adapters" / ".staging",
        root / "adapters" / ".staging" / "registry",
        root / "training_datasets",
        root / "training_datasets" / "jobs",
    ):
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)


def test_partial_marker_cannot_publish_and_stale_stage_is_not_reused() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _layout(root)
        department = DepartmentScope(uuid4())
        adapter_id = uuid4()
        publication_attempt_id = uuid4()
        with AdapterRegistryArtifactStore(root) as store:
            stage = store.prepare_registry_stage(department, adapter_id, publication_attempt_id)
            marker = (
                root
                / "adapters"
                / ".staging"
                / "registry"
                / str(department)
                / str(adapter_id)
                / str(publication_attempt_id)
                / ".deptslm-adapter-registry-stage-owner"
            )
            marker.write_bytes(b"")
            with pytest.raises(AdapterRegistryArtifactError):
                store.publish_registry(stage)
            stage.close()
            with pytest.raises(AdapterRegistryArtifactError):
                store.prepare_registry_stage(department, adapter_id, publication_attempt_id)
        assert marker.exists()
