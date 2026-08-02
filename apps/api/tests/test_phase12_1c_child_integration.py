"""Full fixed-child registry builds using synthetic temporary artifacts."""

from __future__ import annotations

import hashlib
import json
import os
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
from app.adapter_registry_child import (
    FINAL_FILES,
    AdapterRegistryChildError,
    build_registry_stage,
)
from app.training_job_domain import ValidatedDataset, build_bundle


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def _model(path: Path, dtype: str) -> tuple[int, str, int]:
    payload_size = sum(
        shape[0] * shape[1] * TENSOR_DTYPE_BYTES[dtype] for shape in EXPECTED_TENSOR_SHAPES.values()
    )
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    offset = 0
    for name in EXPECTED_TENSOR_NAMES:
        shape = EXPECTED_TENSOR_SHAPES[name]
        size = shape[0] * shape[1] * TENSOR_DTYPE_BYTES[dtype]
        header[name] = {
            "dtype": dtype,
            "shape": list(shape),
            "data_offsets": [offset, offset + size],
        }
        offset += size
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    with path.open("wb") as handle:
        handle.write(len(raw).to_bytes(8, "little"))
        handle.write(raw)
        handle.truncate(8 + len(raw) + payload_size)
    os.chmod(path, 0o600)
    size = path.stat().st_size
    return size, _digest(path), payload_size


def _child_inputs(
    root: Path, dtype: str
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    department_id = uuid4()
    adapter_id = uuid4()
    publication_attempt_id = uuid4()
    source_bundle_id = uuid4()
    source_attempt_id = uuid4()
    source_publication_id = uuid4()
    imported_by = uuid4()
    training_job_id = uuid4()
    training_publication_id = uuid4()
    training_scope_id = uuid4()
    dataset_id = uuid4()
    dataset_source_id = uuid4()
    dataset_publication_id = uuid4()
    source_code_revision = "1" * 40
    training_code_revision = "2" * 40
    registry_code_revision = "3" * 40

    config_path = root / "adapter_config.json"
    config_path.write_bytes(canonical_adapter_config_bytes())
    os.chmod(config_path, 0o600)
    model_path = root / "adapter_model.safetensors"
    model_size, model_sha, payload_size = _model(model_path, dtype)
    config_raw = config_path.read_bytes()
    config_sha = hashlib.sha256(config_raw).hexdigest()

    source_manifest = {
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "department_id": str(department_id),
        "source_bundle_id": str(source_bundle_id),
        "import_attempt_id": str(source_attempt_id),
        "publication_attempt_id": str(source_publication_id),
        "attempt_number": 1,
        "imported_by_user_id": str(imported_by),
        "code_revision": source_code_revision,
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": dtype,
        "tensor_count": 392,
        "tensor_element_count": 10092544,
        "tensor_payload_byte_size": payload_size,
        "files": {
            "adapter_config.json": {"sha256": config_sha, "byte_size": len(config_raw)},
            "adapter_model.safetensors": {"sha256": model_sha, "byte_size": model_size},
        },
    }
    source_manifest_raw = (
        json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )

    train = b'{"messages":[{"role":"user","content":"q"}]}\n'
    validation = b'{"messages":[{"role":"user","content":"v"}]}\n'
    dataset = ValidatedDataset(
        train_count=1,
        validation_count=1,
        train_sha256=hashlib.sha256(train).hexdigest(),
        validation_sha256=hashlib.sha256(validation).hexdigest(),
        train_byte_size=len(train),
        validation_byte_size=len(validation),
    )
    bundle = build_bundle(
        department_id=department_id,
        training_job_id=training_job_id,
        dataset_build_id=dataset_id,
        publication_attempt_id=training_publication_id,
        execution_scope_id=training_scope_id,
        attempt_number=1,
        code_revision=training_code_revision,
        dataset_build_version=1,
        dataset_manifest_sha256="d" * 64,
        dataset_artifact_contract_version="phase10-sft-dataset-v1",
        dataset_example_contract_version="phase10-sft-example-v1",
        dataset_normalization_version="phase10-sft-normalization-v1",
        dataset_split_version="phase10-sft-group-split-v1",
        profile_id="phase11-qwen3-0.6b-lora-v1",
        dataset_rights_attested=True,
        evaluation_contamination_reviewed=True,
        dataset=dataset,
    )
    training_manifest = json.loads(bundle.manifest)
    training_files = training_manifest["files"]
    # The persisted Phase 11 result hash covers the exact newline-terminated file.
    governance = {
        "training_job_id": str(training_job_id),
        "training_job_version": 1,
        "training_job_publication_attempt_id": str(training_publication_id),
        "training_job_attempt_number": 1,
        "training_job_code_revision": training_code_revision,
        "training_job_manifest_sha256": hashlib.sha256(bundle.manifest).hexdigest(),
        "training_job_manifest_byte_size": len(bundle.manifest),
        "training_job_execution_scope_id": str(training_scope_id),
        "training_job_config_sha256": training_files["training.yaml"]["sha256"],
        "training_job_config_byte_size": training_files["training.yaml"]["byte_size"],
        "training_job_dataset_info_sha256": training_files["dataset_info.json"]["sha256"],
        "training_job_dataset_info_byte_size": training_files["dataset_info.json"]["byte_size"],
        "training_job_train_sha256": training_files["train.jsonl"]["sha256"],
        "training_job_train_byte_size": training_files["train.jsonl"]["byte_size"],
        "training_job_validation_sha256": training_files["validation.jsonl"]["sha256"],
        "training_job_validation_byte_size": training_files["validation.jsonl"]["byte_size"],
        "training_job_profile_id": training_manifest["profile_id"],
        "training_job_artifact_contract_version": training_manifest["artifact_contract_version"],
        "training_job_manifest_contract_version": training_manifest["manifest_contract_version"],
        "training_configuration_contract_version": training_manifest[
            "configuration_contract_version"
        ],
        "training_dataset_info_contract_version": training_manifest[
            "dataset_info_contract_version"
        ],
        "training_execution_profile_contract_version": training_manifest[
            "execution_profile_contract_version"
        ],
        "llamafactory_version": training_manifest["llamafactory_version"],
        "dataset_build_id": str(dataset_id),
        "dataset_build_version": 1,
        "dataset_publication_attempt_id": str(dataset_publication_id),
        "dataset_publication_attempt_number": 1,
        "dataset_code_revision": "4" * 40,
        "dataset_manifest_sha256": "d" * 64,
        "dataset_source_bundle_id": str(dataset_source_id),
        "dataset_artifact_contract_version": "phase10-sft-dataset-v1",
        "dataset_example_contract_version": "phase10-sft-example-v1",
        "dataset_normalization_version": "phase10-sft-normalization-v1",
        "dataset_split_version": "phase10-sft-group-split-v1",
        "dataset_train_sha256": "a" * 64,
        "dataset_train_byte_size": 1,
        "dataset_validation_sha256": "b" * 64,
        "dataset_validation_byte_size": 1,
        "dataset_provenance_sha256": "c" * 64,
        "dataset_provenance_byte_size": 1,
        "dataset_train_example_count": 1,
        "dataset_validation_example_count": 1,
        "dataset_source_example_count": 2,
        "dataset_source_group_count": 2,
        "dataset_source_reference_count": 2,
        "dataset_rights_attested": True,
        "evaluation_contamination_reviewed": True,
    }
    source = {
        "source_bundle_id": str(source_bundle_id),
        "authoritative_attempt_id": str(source_attempt_id),
        "publication_attempt_id": str(source_publication_id),
        "attempt_number": 1,
        "version": 2,
        "code_revision": source_code_revision,
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "intake_manifest_sha256": hashlib.sha256(source_manifest_raw).hexdigest(),
        "intake_manifest_byte_size": len(source_manifest_raw),
        "adapter_config_sha256": config_sha,
        "adapter_config_byte_size": len(config_raw),
        "adapter_model_sha256": model_sha,
        "adapter_model_byte_size": model_size,
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": dtype,
        "tensor_count": 392,
        "tensor_element_count": 10092544,
        "tensor_payload_byte_size": payload_size,
        "imported_by_user_id": str(imported_by),
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
    }
    stage = root / "stage"
    stage.mkdir(mode=0o700)
    os.chmod(stage, 0o700)
    descriptors = {
        "source_config_fd": os.open(config_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)),
        "source_model_fd": os.open(model_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)),
        "source_manifest_fd": -1,
        "training_manifest_fd": -1,
        "stage_fd": os.open(stage, os.O_RDONLY | os.O_DIRECTORY),
    }
    source_manifest_path = root / "source-manifest.json"
    source_manifest_path.write_bytes(source_manifest_raw)
    os.chmod(source_manifest_path, 0o600)
    training_manifest_path = root / "training-manifest.json"
    training_manifest_path.write_bytes(bundle.manifest)
    os.chmod(training_manifest_path, 0o600)
    descriptors["source_manifest_fd"] = os.open(
        source_manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors["training_manifest_fd"] = os.open(
        training_manifest_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    request = {
        **descriptors,
        "source_config_size": len(config_raw),
        "source_model_size": model_size,
        "source_manifest_size": len(source_manifest_raw),
        "training_manifest_size": len(bundle.manifest),
        "department_id": str(department_id),
        "adapter_id": str(adapter_id),
        "publication_attempt_id": str(publication_attempt_id),
        "attempt_number": 1,
        "code_revision": registry_code_revision,
        "source": source,
        "governance_lineage": governance,
    }
    return request, source, governance


@pytest.mark.parametrize("dtype", ["F16", "BF16", "F32"])
def test_full_child_builds_exact_registry_for_all_reviewed_dtypes(dtype: str) -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        request, source, governance = _child_inputs(root, dtype)
        try:
            result = build_registry_stage(request)
        finally:
            for key in (
                "source_config_fd",
                "source_model_fd",
                "source_manifest_fd",
                "training_manifest_fd",
                "stage_fd",
            ):
                os.close(request[key])
        assert set(path.name for path in (root / "stage").iterdir()) == FINAL_FILES
        assert result["tensor_dtype"] == dtype
        assert result["tensor_count"] == 392
        assert result["tensor_element_count"] == 10092544
        assert result["registry_adapter_model_byte_size"] == source["adapter_model_byte_size"]
        assert set(governance) == set(request["governance_lineage"])


def test_child_rejects_changed_source_authority_before_writing_stage() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        request, source, _ = _child_inputs(root, "F16")
        source["adapter_model_byte_size"] += 1
        with pytest.raises(AdapterRegistryChildError) as error:
            build_registry_stage(request)
        for key in (
            "source_config_fd",
            "source_model_fd",
            "source_manifest_fd",
            "training_manifest_fd",
            "stage_fd",
        ):
            os.close(request[key])
        assert error.value.code == "adapter_source_authority_changed"
        assert not any((root / "stage").iterdir())


def test_child_rejects_source_snapshot_dtype_mismatch_without_registry_files() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        request, source, _ = _child_inputs(root, "F16")
        source["tensor_dtype"] = "BF16"
        with pytest.raises(AdapterRegistryChildError) as error:
            build_registry_stage(request)
        for key in (
            "source_config_fd",
            "source_model_fd",
            "source_manifest_fd",
            "training_manifest_fd",
            "stage_fd",
        ):
            os.close(request[key])
        assert error.value.code == "adapter_source_authority_changed"
        assert not any((root / "stage").iterdir())


def test_child_rejects_training_manifest_authority_mutation() -> None:
    with TemporaryDirectory() as raw:
        root = Path(raw)
        request, _source, governance = _child_inputs(root, "F16")
        governance["training_job_manifest_sha256"] = "0" * 64
        with pytest.raises(AdapterRegistryChildError) as error:
            build_registry_stage(request)
        for key in (
            "source_config_fd",
            "source_model_fd",
            "source_manifest_fd",
            "training_manifest_fd",
            "stage_fd",
        ):
            os.close(request[key])
        assert error.value.code == "training_job_artifact_mismatch"
