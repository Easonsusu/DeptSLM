"""Child request and static-boundary tests for Phase 12.1C."""

import hashlib
import json
from uuid import uuid4

import pytest

from app.adapter_registry_child import (
    AdapterRegistryChildError,
    _exact_request,
    _validate_source_manifest,
)


def test_child_request_rejects_extra_fields_and_boolean_attempt_number() -> None:
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


def _source_manifest_fixture() -> tuple[bytes, dict[str, object], dict[str, object]]:
    department_id = str(uuid4())
    source_id = str(uuid4())
    attempt_id = str(uuid4())
    publication_attempt_id = str(uuid4())
    imported_by = str(uuid4())
    source = {
        "source_contract_version": "phase12-adapter-source-v1",
        "intake_contract_version": "phase12-adapter-intake-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": "0.18.1",
        "safetensors_format": "0.7.0",
        "tensor_dtype": "F16",
        "tensor_count": 392,
        "tensor_element_count": 10092544,
        "tensor_payload_byte_size": 20185088,
        "intake_manifest_sha256": "0" * 64,
        "intake_manifest_byte_size": 1,
        "authoritative_attempt_id": attempt_id,
        "publication_attempt_id": publication_attempt_id,
        "attempt_number": 1,
        "code_revision": "a" * 40,
        "imported_by_user_id": imported_by,
        "source_bundle_id": source_id,
        "adapter_config_sha256": "a" * 64,
        "adapter_config_byte_size": 1,
        "adapter_model_sha256": "b" * 64,
        "adapter_model_byte_size": 2,
    }
    manifest = {
        "source_contract_version": source["source_contract_version"],
        "intake_contract_version": source["intake_contract_version"],
        "config_contract_version": source["config_contract_version"],
        "tensor_contract_version": source["tensor_contract_version"],
        "department_id": department_id,
        "source_bundle_id": source_id,
        "import_attempt_id": attempt_id,
        "publication_attempt_id": publication_attempt_id,
        "attempt_number": 1,
        "imported_by_user_id": imported_by,
        "code_revision": source["code_revision"],
        "base_model_id": source["base_model_id"],
        "base_model_revision": source["base_model_revision"],
        "base_model_license": source["base_model_license"],
        "peft_version": source["peft_version"],
        "safetensors_format": source["safetensors_format"],
        "tensor_dtype": source["tensor_dtype"],
        "tensor_count": source["tensor_count"],
        "tensor_element_count": source["tensor_element_count"],
        "tensor_payload_byte_size": source["tensor_payload_byte_size"],
        "files": {
            "adapter_config.json": {"sha256": "a" * 64, "byte_size": 1},
            "adapter_model.safetensors": {"sha256": "b" * 64, "byte_size": 2},
        },
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    source["intake_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    source["intake_manifest_byte_size"] = len(raw)
    request = {"department_id": department_id}
    return raw, request, source


def test_child_accepts_exact_source_manifest_authority() -> None:
    raw, request, source = _source_manifest_fixture()
    _validate_source_manifest(raw, request, source, 1, 2, "a" * 64)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda source: source.update(
            {"intake_manifest_byte_size": source["intake_manifest_byte_size"] + 1}
        ),
        lambda source: source.update({"tensor_dtype": "BF16"}),
        lambda source: source.update({"intake_manifest_sha256": "f" * 64}),
    ],
)
def test_child_rejects_manifest_or_snapshot_mutation(mutation) -> None:
    raw, request, source = _source_manifest_fixture()
    mutation(source)
    with pytest.raises(ValueError):
        _validate_source_manifest(raw, request, source, 1, 2, "a" * 64)
