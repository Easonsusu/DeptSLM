"""Descriptor-lifetime tests for the Phase 12.4 production adapter loader."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

LOADER_PATH = (
    Path(__file__).parents[3] / "services/adapter-runtime/deptslm_adapter_runtime/loader.py"
)
_spec = importlib.util.spec_from_file_location("phase12_4_production_loader", LOADER_PATH)
assert _spec is not None and _spec.loader is not None
loader = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = loader
_spec.loader.exec_module(loader)


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    department_id = uuid4()
    adapter_id = uuid4()
    publication_id = uuid4()
    registry = tmp_path / "registry"
    final = registry / str(department_id) / str(adapter_id)
    final.mkdir(parents=True)
    os.chmod(registry, 0o700)
    os.chmod(registry / str(department_id), 0o700)
    os.chmod(final, 0o700)
    config = b'{"r":1}'
    model = b"safe-model-bytes"
    manifest = {
        "department_id": str(department_id),
        "adapter_id": str(adapter_id),
        "publication_attempt_id": str(publication_id),
        "attempt_number": 1,
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
    manifest_path = final / "manifest.json"
    manifest_path.write_bytes(loader.canonical_json_bytes(manifest) + b"\n")
    (final / "adapter_config.json").write_bytes(config)
    (final / "adapter_model.safetensors").write_bytes(model)
    for path in final.iterdir():
        os.chmod(path, 0o600)

    monkeypatch.setattr(loader, "parse_registry_manifest", lambda raw: json.loads(raw))
    monkeypatch.setattr(loader, "validate_adapter_config", lambda _raw: None)
    monkeypatch.setattr(loader, "validate_safetensors_header", lambda _file, _size: None)
    scratch = tmp_path / "private-copy"
    scratch.mkdir()
    os.chmod(scratch, 0o700)
    sequence = 0

    def private_copy() -> Path:
        nonlocal sequence
        sequence += 1
        destination = scratch / f"target-{sequence}"
        destination.mkdir(mode=0o700)
        return destination

    monkeypatch.setattr(loader, "_private_copy_directory", private_copy)
    return (
        registry,
        final,
        manifest_path,
        department_id,
        adapter_id,
        publication_id,
        config,
        model,
        manifest,
        scratch,
    )


def _copy(values, **kwargs):
    (
        registry,
        _final,
        _manifest_path,
        department_id,
        adapter_id,
        publication_id,
        config,
        model,
        manifest,
        _scratch,
    ) = values
    return loader.verify_and_copy_adapter(
        registry,
        department_id=department_id,
        adapter_id=adapter_id,
        adapter_version=1,
        registry_publication_attempt_id=publication_id,
        registry_attempt_number=1,
        expected_manifest_sha256=hashlib.sha256(loader.canonical_json_bytes(manifest)).hexdigest(),
        expected_config_sha256=hashlib.sha256(config).hexdigest(),
        expected_config_byte_size=len(config),
        expected_model_sha256=hashlib.sha256(model).hexdigest(),
        expected_model_byte_size=len(model),
        **kwargs,
    )


def test_production_loader_retains_manifest_and_publishes_exact_private_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    copy = _copy(values)
    try:
        assert copy.config_path.read_bytes() == values[6]
        assert copy.model_path.read_bytes() == values[7]
    finally:
        copy.close()
    assert not any(values[9].iterdir())


@pytest.mark.parametrize("mutation", ["same_inode", "replacement", "during_copy"])
def test_manifest_mutation_after_initial_verification_cleans_private_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    (
        _registry,
        final,
        manifest_path,
        _department,
        _adapter,
        _publication,
        _config,
        _model,
        _manifest,
        scratch,
    ) = values
    changed = False

    def mutate(destination: Path, total: int) -> None:
        nonlocal changed
        if changed or destination.name not in {"adapter_config.json", "adapter_model.safetensors"}:
            return
        if mutation == "during_copy" and destination.name != "adapter_model.safetensors":
            return
        if total != 0:
            return
        changed = True
        if mutation == "same_inode" or mutation == "during_copy":
            descriptor = os.open(manifest_path, os.O_WRONLY)
            try:
                raw = manifest_path.read_bytes()
                os.pwrite(descriptor, raw.replace(b'"attempt_number":1', b'"attempt_number":2'), 0)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            parked = final / "manifest.parked"
            manifest_path.rename(parked)
            manifest_path.write_bytes(loader.canonical_json_bytes(values[8]) + b"\n")
            os.chmod(manifest_path, 0o600)

    with pytest.raises(loader.AdapterRuntimeError, match="adapter_authority_changed"):
        _copy(values, copy_hook=mutate)
    assert changed
    assert not any(scratch.iterdir())
    assert final.is_dir()
    assert (final / "adapter_config.json").read_bytes() == values[6]
    assert (final / "adapter_model.safetensors").read_bytes() == values[7]


def test_final_directory_substitution_is_rejected_without_copy_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = _fixture(tmp_path, monkeypatch)
    (
        _registry,
        final,
        _manifest_path,
        _department,
        adapter,
        _publication,
        _config,
        _model,
        _manifest,
        scratch,
    ) = values
    department = final.parent

    def replace(_destination: Path, marker: object) -> None:
        if marker != "after-copy":
            return
        parked = department / f"{adapter}.parked"
        final.rename(parked)
        replacement = department / str(adapter)
        replacement.mkdir(mode=0o700)

    with pytest.raises(loader.AdapterRuntimeError, match="adapter_authority_changed"):
        _copy(values, copy_hook=replace)
    assert (department / f"{adapter}.parked").is_dir()
    assert (department / str(adapter)).is_dir()
    assert not any(scratch.iterdir())
