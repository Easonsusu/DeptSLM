"""Focused Phase 12.1C contract and storage-boundary checks."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from app.adapter_registry_artifacts import (
    REGISTRY_STAGE_MARKER,
    REGISTRY_STAGE_MARKER_BYTES,
    AdapterRegistryArtifactError,
    AdapterRegistryArtifactStore,
)
from app.adapter_registry_child import AdapterRegistryChildError, _exact_request
from app.adapter_registry_domain import (
    AdapterRegistryDomainError,
    canonical_json_bytes,
    parse_registry_manifest,
)
from app.adapter_registry_worker import _settings
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


def test_registry_manifest_bytes_have_one_trailing_lf_and_duplicate_keys_fail() -> None:
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}\n'
    with pytest.raises(AdapterRegistryDomainError):
        parse_registry_manifest(b'{"a":1,"a":2}\n')
    with pytest.raises(AdapterRegistryDomainError):
        parse_registry_manifest(b"{}\n\n")


def test_registry_stage_marker_is_fixed_and_partial_marker_does_not_authorize_final_publish() -> (
    None
):
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _layout(root)
        department = DepartmentScope(uuid4())
        adapter_id = uuid4()
        publication_attempt_id = uuid4()
        with AdapterRegistryArtifactStore(root) as store:
            stage = store.prepare_registry_stage(department, adapter_id, publication_attempt_id)
            marker = (
                Path(raw)
                / "adapters"
                / ".staging"
                / "registry"
                / str(department)
                / str(adapter_id)
                / str(publication_attempt_id)
                / REGISTRY_STAGE_MARKER
            )
            assert marker.read_bytes() == REGISTRY_STAGE_MARKER_BYTES
            marker.write_bytes(b"")
            with pytest.raises(AdapterRegistryArtifactError):
                store.publish_registry(stage)
            stage.close()


def test_registry_stage_cleanup_uses_exact_attempt_and_leaves_sibling() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        _layout(root)
        department = DepartmentScope(uuid4())
        adapter_id = uuid4()
        attempt_a = uuid4()
        attempt_b = uuid4()
        base = root / "adapters" / ".staging" / "registry" / str(department) / str(adapter_id)
        base.mkdir(parents=True, mode=0o700)
        os.chmod(base.parent, 0o700)
        os.chmod(base, 0o700)
        for attempt in (attempt_a, attempt_b):
            directory = base / str(attempt)
            directory.mkdir(mode=0o700)
            (directory / "partial").write_bytes(b"opaque")
            os.chmod(directory / "partial", 0o600)
        with AdapterRegistryArtifactStore(root) as store:
            assert store.remove_owned_registry_stage(department, adapter_id, attempt_a)
        assert not (base / str(attempt_a)).exists()
        assert (base / str(attempt_b) / "partial").exists()


def test_child_request_rejects_extra_fields_and_boolean_descriptor_values() -> None:
    with pytest.raises(AdapterRegistryChildError):
        _exact_request({"operation": "build_registry"})
    request = {
        key: 1
        for key in (
            "source_config_fd",
            "source_model_fd",
            "source_manifest_fd",
            "training_manifest_fd",
            "stage_fd",
        )
    }
    request.update(
        {
            "source_config_size": 1,
            "source_model_size": 1,
            "source_manifest_size": 1,
            "training_manifest_size": 1,
            "department_id": str(uuid4()),
            "adapter_id": str(uuid4()),
            "publication_attempt_id": str(uuid4()),
            "attempt_number": True,
            "code_revision": "a" * 40,
            "source": {},
            "governance_lineage": {},
        }
    )
    with pytest.raises(AdapterRegistryChildError):
        _exact_request(request)


def test_worker_requires_registry_storage_and_exact_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
