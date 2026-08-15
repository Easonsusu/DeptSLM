"""Focused Phase 12.3 governance-worker boundary tests."""

import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from app.adapter_contract import (
    EXPECTED_TENSOR_NAMES,
    EXPECTED_TENSOR_SHAPES,
    TENSOR_DTYPE_BYTES,
    canonical_adapter_config_bytes,
)
from app.adapter_governance_child import AdapterGovernanceChildError, validate_descriptors
from app.adapter_governance_supervision import (
    _validate_response,
    run_adapter_governance_validation_child,
)
from app.adapter_governance_worker import AdapterGovernanceWorkerSettings
from app.adapter_registry_artifacts import (
    REGISTRY_FINAL_FILES,
    AdapterRegistryArtifactError,
    AdapterRegistryFinalReader,
)
from app.authorization import DepartmentScope


def _registry_layout(root: Path) -> tuple[DepartmentScope, object]:
    registry = root / "adapters" / "registry"
    registry.mkdir(parents=True, mode=0o700)
    os.chmod(root / "adapters", 0o700)
    department = DepartmentScope(uuid4())
    adapter_id = uuid4()
    department_path = registry / str(department)
    department_path.mkdir(mode=0o700)
    os.chmod(department_path, 0o700)
    final = department_path / str(adapter_id)
    final.mkdir(parents=True, mode=0o700)
    for name in REGISTRY_FINAL_FILES:
        (final / name).write_bytes(b"{}\n")
        os.chmod(final / name, 0o600)
    return department, adapter_id


def test_governance_settings_require_only_external_registry_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "adapters" / "registry").mkdir(parents=True, mode=0o700)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db/deptslm")
        monkeypatch.setenv("DEPTSLM_DATA_DIR", str(root))
        settings = AdapterGovernanceWorkerSettings.from_environment()
        assert settings.data_dir == root
        assert not (root / "uploads").exists()


def test_governance_reader_opens_registry_without_import_or_staging_trees() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        department, adapter_id = _registry_layout(root)
        with AdapterRegistryFinalReader(root) as reader:
            retained = reader.open_registry_final(department, adapter_id)
            assert frozenset(name for name, _fd, _meta in retained.files) == REGISTRY_FINAL_FILES
            assert retained.allowlist == REGISTRY_FINAL_FILES
            descriptors = [retained.directory_fd, retained.parent_fd] + [
                descriptor for _name, descriptor, _metadata in retained.files
            ]
            retained.close()
            for descriptor in descriptors:
                with pytest.raises(OSError):
                    os.fstat(descriptor)


@pytest.mark.parametrize("mutation", ["unknown", "missing"])
def test_governance_reader_rejects_non_exact_registry_allowlist(
    mutation: str,
) -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        department, adapter_id = _registry_layout(root)
        final = root / "adapters" / "registry" / str(department.value) / str(adapter_id)
        if mutation == "unknown":
            (final / "unexpected.bin").write_bytes(b"x")
            os.chmod(final / "unexpected.bin", 0o600)
        else:
            (final / "manifest.json").unlink()
        with AdapterRegistryFinalReader(root) as reader:
            with pytest.raises(AdapterRegistryArtifactError):
                reader.open_registry_final(department, adapter_id)


def test_governance_reader_rejects_repository_local_data_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db/deptslm")
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(Path(__file__).parents[3]))
    with pytest.raises(ValueError):
        AdapterGovernanceWorkerSettings.from_environment()


def test_governance_settings_reject_root_symlink(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as raw:
        target = Path(raw) / "target"
        (target / "adapters" / "registry").mkdir(parents=True, mode=0o700)
        link = Path(raw) / "link"
        link.symlink_to(target, target_is_directory=True)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@db/deptslm")
        monkeypatch.setenv("DEPTSLM_DATA_DIR", str(link))
        with pytest.raises(ValueError):
            AdapterGovernanceWorkerSettings.from_environment()


def test_governance_child_rejects_reused_descriptor_and_bad_response_schema() -> None:
    with pytest.raises(AdapterGovernanceChildError):
        validate_descriptors({"config_fd": 3, "model_fd": 3, "config_size": 1, "model_size": 1})
    with pytest.raises(AdapterRegistryArtifactError):
        _validate_response({"status": "ok", "result": {"unexpected": 1}})


def _write_sparse_valid_safetensors(path: Path) -> int:
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    offset = 0
    for name in EXPECTED_TENSOR_NAMES:
        shape = EXPECTED_TENSOR_SHAPES[name]
        size = shape[0] * shape[1] * TENSOR_DTYPE_BYTES["F16"]
        header[name] = {
            "dtype": "F16",
            "shape": list(shape),
            "data_offsets": [offset, offset + size],
        }
        offset += size
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(len(raw).to_bytes(8, "little") + raw)
    with path.open("ab") as handle:
        handle.truncate(8 + len(raw) + offset)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path.stat().st_size


def test_governance_child_validates_the_retained_descriptors(tmp_path: Path) -> None:
    config = tmp_path / "adapter_config.json"
    config.write_bytes(canonical_adapter_config_bytes())
    os.chmod(config, 0o600)
    model = tmp_path / "adapter_model.safetensors"
    model_size = _write_sparse_valid_safetensors(model)
    config_fd = os.open(config, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    model_fd = os.open(model, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        result = run_adapter_governance_validation_child(
            config_fd=config_fd,
            model_fd=model_fd,
            config_size=config.stat().st_size,
            model_size=model_size,
        )
    finally:
        os.close(config_fd)
        os.close(model_fd)
    assert result["config_contract_version"] == "phase12-adapter-config-v1"
    assert result["tensor_contract_version"] == "phase12-adapter-tensors-v1"
    assert result["tensor_count"] == len(EXPECTED_TENSOR_NAMES)
    assert result["tensor_dtype"] == "F16"
