"""Focused Phase 12.1E-A storage and CLI-boundary tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapter_maintenance_artifacts import (
    AdapterMaintenanceArtifactError,
    AdapterMaintenanceArtifactStore,
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


def test_partial_marker_and_payload_are_recoverable(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / ".deptslm-adapter-stage-owner", b"")
    _file(stage / "adapter_config.json", b"partial")

    with AdapterMaintenanceArtifactStore(root) as store:
        bound = store.bind_tombstone(
            "source_stage", department, source, attempt, item, expected_manifest=None
        )
        assert bound is not None
        assert not stage.exists()
        for entry in bound.deletion_plan:
            store.unlink_tombstone_entry(bound, str(entry["name"]), allow_missing=False)
        store.remove_tombstone_directory(bound)
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
        bound = store.bind_tombstone(
            "source_stage", department, source, attempt, item, expected_manifest=None
        )
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
            store.bind_tombstone(
                "source_stage", department, source, attempt, item, expected_manifest=None
            )
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
            store.bind_tombstone(
                "source_stage", department, source, attempt, item, expected_manifest=None
            )
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
            store.bind_tombstone(
                "source_stage", department, source, attempt, item, expected_manifest=None
            )
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
        "department_id": str(department),
        "source_bundle_id": str(source),
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
    _file(final / "intake_manifest.json", b"{}\n")
    # The manifest must be the exact closed value retained by the authority.
    _file(final / "intake_manifest.json", __import__("json").dumps(manifest).encode())
    with AdapterMaintenanceArtifactStore(root) as store:
        bound = store.bind_tombstone(
            "source_final", department, source, attempt, item, expected_manifest=manifest
        )
        assert bound is not None


def test_inspection_is_read_only_and_move_requires_explicit_namespace(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    department, source, attempt, item = (uuid4() for _ in range(4))
    stage = _source_stage(root, str(department), str(source), str(attempt))
    _file(stage / "adapter_config.json", b"partial")
    with AdapterMaintenanceArtifactStore(root) as store:
        inspected = store.inspect_surface(
            "source_stage", department, source, attempt, item, expected_manifest=None
        )
        assert inspected is not None
        assert stage.exists()
        assert not store.tombstone_exists("source_stage", department, source, item)
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
            "source_stage", department, source, attempt, item, expected_manifest=None
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
            "source_stage", department, source, attempt, item, expected_manifest=None
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
