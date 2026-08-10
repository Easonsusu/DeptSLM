"""Focused descriptor and namespace tests for Phase 12.1E-B purge storage."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapter_maintenance_artifacts import (
    AdapterMaintenanceArtifactError,
    AdapterPurgeArtifactStore,
    RetryablePurgeTombstoneNamespaceConflict,
    physical_surface_identifier,
)
from app.adapter_source_artifacts import canonical_manifest_bytes, parse_source_manifest


def _runtime_root(tmp_path: Path) -> Path:
    for relative in (
        "adapters",
        "adapters/.staging",
        "adapters/.deleting",
        "adapters/.purge-deleting",
        "adapters/imports",
        "adapters/registry",
        "adapters/.staging/imports",
        "adapters/.staging/registry",
        "adapters/.deleting/source_stage",
        "adapters/.deleting/source_final",
        "adapters/.deleting/registry_stage",
        "adapters/.deleting/registry_final",
        "adapters/.purge-deleting/source_stage",
        "adapters/.purge-deleting/source_final",
        "adapters/.purge-deleting/registry_stage",
        "adapters/.purge-deleting/registry_final",
    ):
        path = tmp_path / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    return tmp_path


def _file(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    path.chmod(0o600)


def _source_final(
    root: Path, department, source, attempt, *, valid: bool = True
) -> tuple[Path, dict]:
    config = b"{}"
    model = b"model"
    manifest = {
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "department_id": str(department),
        "source_bundle_id": str(source),
        "import_attempt_id": str(attempt),
        "publication_attempt_id": str(uuid4()),
        "attempt_number": 1,
        "imported_by_user_id": str(uuid4()),
        "code_revision": "a" * 40,
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": "F16",
        "tensor_count": 392,
        "tensor_element_count": 10092544,
        "tensor_payload_byte_size": 20185088,
        "files": {
            "adapter_config.json": {
                "sha256": hashlib.sha256(config).hexdigest(),
                "byte_size": len(config),
            },
            "adapter_model.safetensors": {
                "sha256": hashlib.sha256(model).hexdigest(),
                "byte_size": len(model),
            },
        },
    }
    raw = canonical_manifest_bytes(manifest)
    parse_source_manifest(raw)
    final = root / "adapters" / "imports" / str(department) / str(source)
    final.mkdir(mode=0o700, parents=True)
    final.parent.chmod(0o700)
    final.chmod(0o700)
    _file(final / "adapter_config.json", config)
    _file(final / "adapter_model.safetensors", model)
    _file(final / "intake_manifest.json", raw if valid else raw[:-2])
    return final, manifest


def _move_source(store, department, source, item, manifest, *, expected_digest=None):
    address = physical_surface_identifier("source_final", department, source, None)
    raw = canonical_manifest_bytes(manifest)
    inspected = store.inspect_surface(
        address,
        item,
        expected_manifest=manifest,
        expected_manifest_sha256=expected_digest or hashlib.sha256(raw).hexdigest(),
        expected_manifest_byte_size=len(raw),
    )
    assert inspected is not None
    return store.move_verified_surface_to_tombstone(
        inspected,
        expected_tombstone_namespace={
            "surface_type": "source_final",
            "department_id": str(department),
            "resource_id": str(source),
            "item_id": str(item),
        },
    )


def test_purge_namespace_is_separate_and_exactly_deletable(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    final, manifest = _source_final(root, department, source, attempt)
    with AdapterPurgeArtifactStore(root) as store:
        bound = _move_source(store, department, source, item, manifest)
        assert bound is not None
        assert not final.exists()
        purge_tombstone = (
            root
            / "adapters"
            / ".purge-deleting"
            / "source_final"
            / str(department)
            / str(source)
            / str(item)
        )
        assert purge_tombstone.is_dir()
        assert not (
            root
            / "adapters"
            / ".deleting"
            / "source_final"
            / str(department)
            / str(source)
            / str(item)
        ).exists()
        for entry in bound.deletion_plan:
            store.unlink_committed_tombstone_entry(bound, entry["name"], allow_missing=False)
        store.remove_committed_tombstone_directory(bound)
        with pytest.raises(AdapterMaintenanceArtifactError):
            store.remove_committed_tombstone_directory(bound)


def test_purge_tombstone_substitution_is_denied_and_preserved(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    _final, manifest = _source_final(root, department, source, attempt)
    with AdapterPurgeArtifactStore(root) as store:
        bound = _move_source(store, department, source, item, manifest)
        assert bound is not None
        tombstone = (
            root
            / "adapters"
            / ".purge-deleting"
            / "source_final"
            / str(department)
            / str(source)
            / str(item)
        )
        parked = tombstone.with_name(f"{item}.parked")
        os.rename(tombstone, parked)
        tombstone.mkdir(mode=0o700)
        tombstone.chmod(0o700)
        with pytest.raises(AdapterMaintenanceArtifactError):
            store.remove_committed_tombstone_directory(bound)
        assert tombstone.exists()
        assert parked.exists()


def test_purge_rebinds_a_tombstone_after_original_move(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    final, manifest = _source_final(root, department, source, attempt)
    address = physical_surface_identifier("source_final", department, source, None)
    raw = canonical_manifest_bytes(manifest)
    with AdapterPurgeArtifactStore(root) as store:
        inspected = store.inspect_surface(
            address,
            item,
            expected_manifest=manifest,
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            expected_manifest_byte_size=len(raw),
        )
        assert inspected is not None
        bound = store.move_verified_surface_to_tombstone(
            inspected,
            expected_tombstone_namespace={
                "surface_type": "source_final",
                "department_id": str(department),
                "resource_id": str(source),
                "item_id": str(item),
            },
        )
        assert bound is not None and not final.exists()
        recovered = store.recover_authorized_move(
            inspected,
            expected_tombstone_namespace={
                "surface_type": "source_final",
                "department_id": str(department),
                "resource_id": str(source),
                "item_id": str(item),
            },
        )
        assert recovered.tombstone_identity == bound.tombstone_identity


def test_purge_recovery_rejects_unknown_private_sibling_without_mutation(tmp_path: Path) -> None:
    """An unbound expected tombstone cannot coexist with an unknown sibling."""

    root = _runtime_root(tmp_path)
    department, source, attempt, item, unknown = (uuid4() for _ in range(5))
    _final, manifest = _source_final(root, department, source, attempt)
    address = physical_surface_identifier("source_final", department, source, None)
    raw = canonical_manifest_bytes(manifest)
    namespace = {
        "surface_type": "source_final",
        "department_id": str(department),
        "resource_id": str(source),
        "item_id": str(item),
    }
    with AdapterPurgeArtifactStore(root) as store:
        inspected = store.inspect_surface(
            address,
            item,
            expected_manifest=manifest,
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            expected_manifest_byte_size=len(raw),
        )
        assert inspected is not None
        bound = store.move_verified_surface_to_tombstone(
            inspected,
            expected_tombstone_namespace=namespace,
        )
        assert bound is not None
        tombstone_parent = (
            root / "adapters" / ".purge-deleting" / "source_final" / str(department) / str(source)
        )
        expected = tombstone_parent / str(item)
        sibling = tombstone_parent / str(unknown)
        sibling.mkdir(mode=0o700)
        sibling.chmod(0o700)
        payload = sibling / "opaque.bin"
        _file(payload, b"unowned")
        expected_identity = expected.stat()
        with pytest.raises(RetryablePurgeTombstoneNamespaceConflict):
            store.open_exact_authorized_recovery_tombstone(
                inspected,
                expected_tombstone_namespace=namespace,
            )
        assert expected.is_dir()
        assert (expected.stat().st_dev, expected.stat().st_ino) == (
            expected_identity.st_dev,
            expected_identity.st_ino,
        )
        assert {path.name for path in expected.iterdir()} == {
            entry["name"] for entry in bound.deletion_plan
        }
        assert sibling.is_dir() and payload.read_bytes() == b"unowned"


def test_purge_recovery_namespace_conflict_resumes_same_move_intent(tmp_path: Path) -> None:
    """Removing only an unknown sibling restores exact recovery authority."""

    root = _runtime_root(tmp_path)
    department, source, attempt, item, unknown = (uuid4() for _ in range(5))
    _final, manifest = _source_final(root, department, source, attempt)
    address = physical_surface_identifier("source_final", department, source, None)
    raw = canonical_manifest_bytes(manifest)
    namespace = {
        "surface_type": "source_final",
        "department_id": str(department),
        "resource_id": str(source),
        "item_id": str(item),
    }
    with AdapterPurgeArtifactStore(root) as store:
        inspected = store.inspect_surface(
            address,
            item,
            expected_manifest=manifest,
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            expected_manifest_byte_size=len(raw),
        )
        assert inspected is not None
        bound = store.move_verified_surface_to_tombstone(
            inspected,
            expected_tombstone_namespace=namespace,
        )
        assert bound is not None
        sibling = (
            root
            / "adapters"
            / ".purge-deleting"
            / "source_final"
            / str(department)
            / str(source)
            / str(unknown)
        )
        sibling.mkdir(mode=0o700)
        sibling.chmod(0o700)
        _file(sibling / "opaque.bin", b"unowned")
        with pytest.raises(RetryablePurgeTombstoneNamespaceConflict):
            store.open_exact_authorized_recovery_tombstone(
                inspected,
                expected_tombstone_namespace=namespace,
            )
        # Model a reviewed external operator action. The expected directory,
        # item ID, observation, and deletion plan are never replaced.
        (sibling / "opaque.bin").unlink()
        sibling.rmdir()
        recovered = store.open_exact_authorized_recovery_tombstone(
            inspected,
            expected_tombstone_namespace=namespace,
        )
        assert recovered.item_id == item
        assert recovered.observed_identity == inspected.observed_identity
        assert recovered.deletion_plan == bound.deletion_plan
        assert recovered.tombstone_identity == bound.tombstone_identity


def test_purge_final_requires_closed_manifest_and_exact_digest(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    final, manifest = _source_final(root, department, source, attempt, valid=False)
    with AdapterPurgeArtifactStore(root) as store:
        with pytest.raises(AdapterMaintenanceArtifactError):
            _move_source(store, department, source, item, manifest)
    assert final.exists()


def test_purge_symlinked_final_is_blocked_without_following(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    final, manifest = _source_final(root, department, source, attempt)
    outside = tmp_path / "outside"
    _file(outside, b"outside")
    (final / "adapter_config.json").unlink()
    (final / "adapter_config.json").symlink_to(outside)
    with AdapterPurgeArtifactStore(root) as store:
        with pytest.raises(AdapterMaintenanceArtifactError):
            _move_source(store, department, source, item, manifest)
    assert outside.exists()
    assert final.exists()
