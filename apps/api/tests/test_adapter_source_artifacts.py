"""Private descriptor-bound adapter source publication tests."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapter_contract import (
    ADAPTER_CONFIG_CONTRACT_VERSION,
    ADAPTER_INTAKE_CONTRACT_VERSION,
    ADAPTER_SOURCE_CONTRACT_VERSION,
    ADAPTER_TENSOR_CONTRACT_VERSION,
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    EXPECTED_TENSOR_NAMES,
    EXPECTED_TENSOR_SHAPES,
    PEFT_FORMAT_REFERENCE_VERSION,
    SAFETENSORS_FORMAT_REFERENCE_VERSION,
    canonical_adapter_config_bytes,
)
from app.adapter_source_artifacts import (
    FINAL_FILES,
    AdapterSourceArtifactError,
    AdapterSourceArtifactStore,
)
from app.authorization import DepartmentScope


def _files(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "adapter_config.json"
    model = tmp_path / "adapter_model.safetensors"
    config.write_bytes(canonical_adapter_config_bytes())
    offset = 0
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    for name in EXPECTED_TENSOR_NAMES:
        shape = EXPECTED_TENSOR_SHAPES[name]
        size = shape[0] * shape[1] * 2
        header[name] = {
            "dtype": "F16",
            "shape": list(shape),
            "data_offsets": [offset, offset + size],
        }
        offset += size
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    with model.open("wb") as handle:
        handle.write(len(raw).to_bytes(8, "little"))
        handle.write(raw)
        handle.truncate(8 + len(raw) + offset)
    os.chmod(config, 0o600)
    os.chmod(model, 0o600)
    return config, model


def _manifest(
    department_id, source_id, attempt_id, publication_id, config, model, attempt_number=1
):
    return {
        "source_contract_version": ADAPTER_SOURCE_CONTRACT_VERSION,
        "intake_contract_version": ADAPTER_INTAKE_CONTRACT_VERSION,
        "config_contract_version": ADAPTER_CONFIG_CONTRACT_VERSION,
        "tensor_contract_version": ADAPTER_TENSOR_CONTRACT_VERSION,
        "department_id": str(department_id),
        "source_bundle_id": str(source_id),
        "import_attempt_id": str(attempt_id),
        "publication_attempt_id": str(publication_id),
        "attempt_number": attempt_number,
        "imported_by_user_id": str(uuid4()),
        "code_revision": "a" * 40,
        "base_model_id": BASE_MODEL_ID,
        "base_model_revision": BASE_MODEL_REVISION,
        "base_model_license": BASE_MODEL_LICENSE,
        "peft_version": PEFT_FORMAT_REFERENCE_VERSION,
        "safetensors_format": SAFETENSORS_FORMAT_REFERENCE_VERSION,
        "tensor_dtype": "F16",
        "tensor_count": 392,
        "tensor_element_count": 10_092_544,
        "tensor_payload_byte_size": 20_185_088,
        "files": {
            "adapter_config.json": {
                "sha256": sha256(config.read_bytes()).hexdigest(),
                "byte_size": config.stat().st_size,
            },
            "adapter_model.safetensors": {
                "sha256": sha256(model.read_bytes()).hexdigest(),
                "byte_size": model.stat().st_size,
            },
        },
    }


def test_publish_has_exact_private_allowlist_and_uuid_layout(tmp_path: Path) -> None:
    (tmp_path / "adapters").mkdir(mode=0o700)
    config, model = _files(tmp_path)
    department_id, source_id, attempt_id, publication_id = (uuid4() for _ in range(4))
    with AdapterSourceArtifactStore(tmp_path) as store:
        config_input, model_input = store.open_external_inputs(config, model)
        staged = store.stage(
            DepartmentScope(department_id),
            source_id,
            attempt_id,
            publication_id,
            1,
            config_input,
            model_input,
            _manifest(department_id, source_id, attempt_id, publication_id, config, model),
        )
        try:
            store.publish(staged)
            staged.recheck_identity()
            assert set(os.listdir(staged.stage_fd)) == set(FINAL_FILES)
            assert os.fstat(staged.stage_fd).st_mode & 0o777 == 0o700
            assert all(
                stat_mode(os.open(name, os.O_RDONLY, dir_fd=staged.stage_fd)) == 0o600
                for name in FINAL_FILES
            )
        finally:
            staged.close()
            config_input.close()
            model_input.close()
    final = tmp_path / "adapters" / "imports" / str(department_id) / str(source_id)
    assert set(item.name for item in final.iterdir()) == set(FINAL_FILES)
    assert not (final / ".deptslm-adapter-stage-owner").exists()


def test_existing_final_source_is_never_replaced(tmp_path: Path) -> None:
    (tmp_path / "adapters").mkdir(mode=0o700)
    config, model = _files(tmp_path)
    department_id, source_id, attempt_id, publication_id = (uuid4() for _ in range(4))
    with AdapterSourceArtifactStore(tmp_path) as store:
        config_input, model_input = store.open_external_inputs(config, model)
        first = store.stage(
            DepartmentScope(department_id),
            source_id,
            attempt_id,
            publication_id,
            1,
            config_input,
            model_input,
            _manifest(department_id, source_id, attempt_id, publication_id, config, model),
        )
        store.publish(first)
        first.close()
        config_input.close()
        model_input.close()
        config_input, model_input = store.open_external_inputs(config, model)
        second_attempt_id, second_publication_id = uuid4(), uuid4()
        second = store.stage(
            DepartmentScope(department_id),
            source_id,
            second_attempt_id,
            second_publication_id,
            2,
            config_input,
            model_input,
            _manifest(
                department_id,
                source_id,
                second_attempt_id,
                second_publication_id,
                config,
                model,
                2,
            ),
        )
        with pytest.raises(AdapterSourceArtifactError):
            store.publish(second)
        second.close()
        config_input.close()
        model_input.close()


def stat_mode(descriptor: int) -> int:
    try:
        return os.fstat(descriptor).st_mode & 0o777
    finally:
        os.close(descriptor)
