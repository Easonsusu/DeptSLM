"""Focused Phase 12.1E-A storage and CLI-boundary tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapter_maintenance_artifacts import (
    AdapterMaintenanceArtifactError,
    AdapterMaintenanceArtifactStore,
    physical_surface_identifier,
)


def _runtime_root(tmp_path: Path) -> Path:
    for relative in (
        "adapters",
        "adapters/.staging",
        "adapters/.deleting",
        "adapters/imports",
        "adapters/registry",
        "adapters/.staging/imports",
        "adapters/.staging/registry",
        "adapters/.deleting/source_stage",
        "adapters/.deleting/source_final",
        "adapters/.deleting/registry_stage",
        "adapters/.deleting/registry_final",
    ):
        path = tmp_path / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    return tmp_path


def _file(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    path.chmod(0o600)


def _source_stage(root: Path, department: str, source: str, attempt: str) -> Path:
    stage = root / "adapters" / ".staging" / "imports" / department / source / attempt
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    return stage


def _address(surface: str, department, resource, attempt=None):
    return physical_surface_identifier(surface, department, resource, attempt)


def _move(
    store,
    surface,
    department,
    resource,
    attempt,
    item,
    *,
    expected=None,
    expected_manifest_sha256=None,
    expected_manifest_byte_size=None,
):
    address = _address(
        surface, department, resource, attempt if surface.endswith("_stage") else None
    )
    inspected = store.inspect_surface(
        address,
        item,
        expected_manifest=expected,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_manifest_byte_size=expected_manifest_byte_size,
    )
    assert inspected is not None
    return store.move_verified_surface_to_tombstone(
        inspected,
        expected_tombstone_namespace={
            "surface_type": surface,
            "department_id": str(department),
            "resource_id": str(resource),
            "item_id": str(item),
        },
    )


def test_partial_marker_and_payload_are_recoverable(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / ".deptslm-adapter-stage-owner", b"")
    _file(stage / "adapter_config.json", b"partial")

    with AdapterMaintenanceArtifactStore(root) as store:
        bound = _move(store, "source_stage", department, source, attempt, item)
        assert bound is not None
        assert not stage.exists()
        for entry in bound.deletion_plan:
            store.unlink_committed_tombstone_entry(bound, str(entry["name"]), allow_missing=False)
        store.remove_committed_tombstone_directory(bound)
    assert not (
        root / "adapters" / ".deleting" / "source_stage" / str(department) / str(source) / str(item)
    ).exists()


@pytest.mark.parametrize("marker", [b"truncated", b"partial\nbytes"])
def test_interrupted_marker_states_do_not_block_cleanup(tmp_path: Path, marker: bytes) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / ".deptslm-adapter-stage-owner", marker)
    with AdapterMaintenanceArtifactStore(root) as store:
        bound = _move(store, "source_stage", department, source, attempt, item)
        assert bound is not None


def test_symlinked_stage_entry_is_blocked_and_untouched(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    target = tmp_path / "outside"
    _file(target, b"outside")
    (stage / "adapter_config.json").symlink_to(target)
    with AdapterMaintenanceArtifactStore(root) as store:
        with pytest.raises(AdapterMaintenanceArtifactError):
            _move(store, "source_stage", department, source, attempt, item)
    assert target.exists()
    assert stage.exists()


def test_directory_substitution_is_rejected_before_move(tmp_path: Path, monkeypatch) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / "adapter_config.json", b"partial")
    with AdapterMaintenanceArtifactStore(root) as store:
        original_inspect = store._inspect

        def replace_after_inspect(*args, **kwargs):
            value = original_inspect(*args, **kwargs)
            parent = stage.parent
            parked = parent / f"{attempt}.parked"
            os.rename(stage, parked)
            (parent / str(attempt)).mkdir(mode=0o700)
            return value

        monkeypatch.setattr(store, "_inspect", replace_after_inspect)
        with pytest.raises(AdapterMaintenanceArtifactError) as error:
            _move(store, "source_stage", department, source, attempt, item)
        assert error.value.code == "artifact_authority_changed"
    assert (stage.parent / str(attempt)).exists()
    assert (stage.parent / f"{attempt}.parked").exists()
    assert not (
        root / "adapters" / ".deleting" / "source_stage" / str(department) / str(source) / str(item)
    ).exists()


def test_unreviewed_entry_added_before_move_is_rejected(tmp_path: Path, monkeypatch) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / "adapter_config.json", b"partial")
    with AdapterMaintenanceArtifactStore(root) as store:
        original_inspect = store._inspect

        def add_after_inspect(*args, **kwargs):
            value = original_inspect(*args, **kwargs)
            _file(stage / "adapter_model.safetensors", b"late")
            return value

        monkeypatch.setattr(store, "_inspect", add_after_inspect)
        with pytest.raises(AdapterMaintenanceArtifactError) as error:
            _move(store, "source_stage", department, source, attempt, item)
        assert error.value.code == "artifact_authority_changed"
    assert stage.exists()
    assert (stage / "adapter_model.safetensors").exists()


def test_final_surface_requires_closed_manifest_and_exact_digests(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    final = root / "adapters" / "imports" / str(department) / str(source)
    final.mkdir(mode=0o700, parents=True)
    for path in (final.parent, final):
        path.chmod(0o700)
    config = b"{}\n"
    model = b"opaque-model-bytes"
    _file(final / "adapter_config.json", config)
    _file(final / "adapter_model.safetensors", model)
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
        "tensor_element_count": 10_092_544,
        "tensor_payload_byte_size": 20_185_088,
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
    # The manifest must be the exact closed value retained by the authority.
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    _file(final / "intake_manifest.json", manifest_bytes)
    with AdapterMaintenanceArtifactStore(root) as store:
        bound = _move(
            store,
            "source_final",
            department,
            source,
            None,
            item,
            expected=manifest,
            expected_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_manifest_byte_size=len(manifest_bytes),
        )
        assert bound is not None


def test_inspection_is_read_only_and_move_requires_explicit_namespace(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / "adapter_config.json", b"partial")
    with AdapterMaintenanceArtifactStore(root) as store:
        address = _address("source_stage", department, source, attempt)
        inspected = store.inspect_surface(address, item, expected_manifest=None)
        assert inspected is not None
        assert stage.exists()
        assert not store.tombstone_exists(address, item)
        with pytest.raises(AdapterMaintenanceArtifactError) as error:
            store.move_verified_surface_to_tombstone(
                inspected,
                expected_tombstone_namespace={"item_id": str(item)},
            )
        assert error.value.code == "artifact_ownership_mismatch"
        bound = store.move_verified_surface_to_tombstone(
            inspected,
            expected_tombstone_namespace={
                "surface_type": "source_stage",
                "department_id": str(department),
                "resource_id": str(source),
                "item_id": str(item),
            },
        )
        assert bound is not None
        assert not stage.exists()
        store.open_committed_tombstone(bound)


def test_valid_looking_unbound_tombstone_is_refused_and_preserved(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / "adapter_config.json", b"original")
    tombstone = (
        root / "adapters" / ".deleting" / "source_stage" / str(department) / str(source) / str(item)
    )
    tombstone.mkdir(mode=0o700, parents=True)
    for path in (
        tombstone.parent.parent.parent,
        tombstone.parent.parent,
        tombstone.parent,
        tombstone,
    ):
        path.chmod(0o700)
    _file(tombstone / "adapter_config.json", b"looks-owned")
    with AdapterMaintenanceArtifactStore(root) as store:
        inspected = store.inspect_surface(
            _address("source_stage", department, source, attempt),
            item,
            expected_manifest=None,
        )
        assert inspected is not None
        with pytest.raises(AdapterMaintenanceArtifactError) as error:
            store.move_verified_surface_to_tombstone(
                inspected,
                expected_tombstone_namespace={
                    "surface_type": "source_stage",
                    "department_id": str(department),
                    "resource_id": str(source),
                    "item_id": str(item),
                },
            )
        assert error.value.code == "artifact_tombstone_conflict"
    assert stage.exists()
    assert tombstone.exists()


def test_authorized_move_recovery_does_not_rebuild_plan(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / ".deptslm-adapter-stage-owner", b"partial")
    with AdapterMaintenanceArtifactStore(root) as store:
        inspected = store.inspect_surface(
            _address("source_stage", department, source, attempt),
            item,
            expected_manifest=None,
        )
        assert inspected is not None
        bound = store.move_verified_surface_to_tombstone(
            inspected,
            expected_tombstone_namespace={
                "surface_type": "source_stage",
                "department_id": str(department),
                "resource_id": str(source),
                "item_id": str(item),
            },
        )
        assert bound is not None
        recovered = store.recover_authorized_move(
            inspected,
            expected_tombstone_namespace={
                "surface_type": "source_stage",
                "department_id": str(department),
                "resource_id": str(source),
                "item_id": str(item),
            },
        )
        assert recovered.deletion_plan == inspected.deletion_plan
        assert recovered.tombstone_identity["entries"] == [
            entry["identity"] for entry in inspected.deletion_plan
        ]


def test_registry_stage_uses_publication_attempt_path(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, adapter, registry_attempt, publication_attempt, item = (uuid4() for _ in range(5))
    stage = root / "adapters" / ".staging" / "registry" / str(department) / str(adapter)
    wrong = stage / str(registry_attempt)
    exact = stage / str(publication_attempt)
    wrong.mkdir(mode=0o700, parents=True)
    exact.mkdir(mode=0o700)
    for path in (stage.parent, stage, wrong, exact):
        path.chmod(0o700)
    _file(exact / "adapter_config.json", b"exact")
    _file(wrong / "adapter_config.json", b"wrong")
    with AdapterMaintenanceArtifactStore(root) as store:
        address = _address("registry_stage", department, adapter, publication_attempt)
        inspected = store.inspect_surface(address, item, expected_manifest=None)
        assert inspected is not None
        assert inspected.address.path_attempt_id == publication_attempt
        bound = store.move_verified_surface_to_tombstone(
            inspected,
            expected_tombstone_namespace={
                "surface_type": "registry_stage",
                "department_id": str(department),
                "resource_id": str(adapter),
                "item_id": str(item),
            },
        )
        assert bound is not None
    assert not exact.exists()
    assert wrong.exists()


def test_department_tombstone_enumeration_rejects_unknown_resource_and_symlink(
    tmp_path: Path,
) -> None:
    root = _runtime_root(tmp_path)
    department, resource, other_resource, item = (uuid4() for _ in range(4))
    base = root / "adapters" / ".deleting" / "source_stage" / str(department)
    known = base / str(resource) / str(item)
    unknown = base / str(other_resource) / str(uuid4())
    known.mkdir(mode=0o700, parents=True)
    unknown.mkdir(mode=0o700, parents=True)
    for path in (base, known.parent, known, unknown.parent, unknown):
        path.chmod(0o700)
    with AdapterMaintenanceArtifactStore(root) as store:
        rows = store.enumerate_department_tombstones("source_stage", department)
        assert (resource, item) in rows
        assert any(value[0] == other_resource for value in rows)
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        (base / "bad").symlink_to(outside, target_is_directory=True)
        with pytest.raises(AdapterMaintenanceArtifactError) as error:
            store.enumerate_department_tombstones("source_stage", department)
        assert error.value.code in {"staging_path_unsafe", "artifact_tombstone_conflict"}
