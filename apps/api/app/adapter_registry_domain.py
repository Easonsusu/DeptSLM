"""Pure, dependency-free Phase 12.1C registry manifest contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from uuid import UUID

# Keep this contract dependency-free.  The values are frozen here rather than
# imported from a mutable application module so a historical manifest parser
# cannot drift when a later phase changes its implementation constants.
ADAPTER_ARTIFACT_CONTRACT_VERSION = "phase12-adapter-artifact-v1"
ADAPTER_CONFIG_CONTRACT_VERSION = "phase12-adapter-config-v1"
ADAPTER_SOURCE_CONTRACT_VERSION = "phase12-adapter-source-v1"
ADAPTER_TENSOR_CONTRACT_VERSION = "phase12-adapter-tensors-v1"
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
BASE_MODEL_LICENSE = "Apache-2.0"
EXPECTED_TENSOR_COUNT = 392
EXPECTED_TENSOR_ELEMENTS = 10_092_544
EXPECTED_TENSOR_BYTES = {
    "F16": 20_185_088,
    "BF16": 20_185_088,
    "F32": 40_370_176,
}
PEFT_FORMAT_REFERENCE_VERSION = "0.18.1"
SAFETENSORS_FORMAT_REFERENCE_VERSION = "0.7.0"

ADAPTER_REGISTRY_MANIFEST_CONTRACT_VERSION = "phase12-adapter-manifest-v1"
_SHA = re.compile(r"\A[0-9a-f]{64}\Z")
_REVISION = re.compile(r"\A[0-9a-f]{40}\Z")
_UUID = re.compile(r"\A[0-9a-f-]{36}\Z")

SAFE_ERROR_CODES = frozenset(
    {
        "adapter_registry_manifest_invalid",
        "adapter_config_invalid",
        "adapter_config_unsupported",
        "adapter_header_invalid",
        "adapter_header_too_large",
        "adapter_file_too_large",
        "adapter_tensor_set_invalid",
        "adapter_tensor_shape_invalid",
        "adapter_tensor_dtype_invalid",
        "adapter_tensor_offsets_invalid",
        "adapter_tensor_size_invalid",
        "adapter_registry_authority_changed",
        "adapter_registry_publication_failed",
    }
)

TOP_LEVEL_KEYS = frozenset(
    {
        "artifact_contract_version",
        "manifest_contract_version",
        "department_id",
        "adapter_id",
        "publication_attempt_id",
        "attempt_number",
        "code_revision",
        "source",
        "governance_lineage",
        "declared_external_training_association",
        "verified_governance_lineage",
        "verified_artifact_compatibility",
        "training_provenance_verified",
        "compatibility",
        "files",
    }
)
SOURCE_KEYS = frozenset(
    {
        "source_bundle_id",
        "authoritative_import_attempt_id",
        "import_publication_attempt_id",
        "import_attempt_number",
        "source_code_revision",
        "source_contract_version",
        "intake_contract_version",
        "intake_manifest_sha256",
        "external_adapter_config_sha256",
        "external_adapter_config_byte_size",
        "external_adapter_model_sha256",
        "external_adapter_model_byte_size",
    }
)
GOVERNANCE_KEYS = frozenset(
    {
        "training_job_id",
        "training_job_version",
        "training_job_publication_attempt_id",
        "training_job_attempt_number",
        "training_job_code_revision",
        "training_job_manifest_sha256",
        "profile_id",
        "training_job_artifact_contract_version",
        "training_job_manifest_contract_version",
        "training_configuration_contract_version",
        "training_dataset_info_contract_version",
        "training_execution_profile_contract_version",
        "llamafactory_version",
        "dataset_build_id",
        "dataset_build_version",
        "dataset_publication_attempt_id",
        "dataset_publication_attempt_number",
        "dataset_code_revision",
        "dataset_manifest_sha256",
        "dataset_source_bundle_id",
        "dataset_artifact_contract_version",
        "dataset_example_contract_version",
        "dataset_normalization_version",
        "dataset_split_version",
        "dataset_train_sha256",
        "dataset_train_byte_size",
        "dataset_validation_sha256",
        "dataset_validation_byte_size",
        "dataset_provenance_sha256",
        "dataset_provenance_byte_size",
        "dataset_train_example_count",
        "dataset_validation_example_count",
        "dataset_source_example_count",
        "dataset_source_group_count",
        "dataset_source_reference_count",
        "dataset_rights_attested",
        "evaluation_contamination_reviewed",
    }
)
COMPATIBILITY_KEYS = frozenset(
    {
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "peft_version",
        "safetensors_format",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
        "adapter_config_contract_version",
        "adapter_tensor_contract_version",
    }
)
FILES_KEYS = frozenset({"adapter_config.json", "adapter_model.safetensors"})


class AdapterRegistryDomainError(ValueError):
    """Fixed, content-free registry contract failure."""

    def __init__(self, code: str = "adapter_registry_manifest_invalid") -> None:
        self.code = code if code in SAFE_ERROR_CODES else "adapter_registry_manifest_invalid"
        super().__init__(self.code)


def _fail(code: str = "adapter_registry_manifest_invalid") -> None:
    raise AdapterRegistryDomainError(code)


def canonical_json_bytes(value: object) -> bytes:
    """Serialize closed metadata with exactly one trailing LF."""

    try:
        return (
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError):
        _fail()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def parse_registry_manifest(raw: bytes | bytearray | memoryview | str) -> dict[str, object]:
    """Parse and require byte-stable JSON; never retain unknown fields."""

    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    elif isinstance(raw, (bytearray, memoryview)):
        raw = bytes(raw)
    if not isinstance(raw, bytes) or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        _fail()
    try:
        value = json.loads(
            raw[:-1].decode("utf-8"), object_pairs_hook=_pairs, parse_constant=lambda _: _fail()
        )
    except (
        UnicodeDecodeError,
        UnicodeError,
        ValueError,
        TypeError,
        RecursionError,
        json.JSONDecodeError,
    ):
        _fail()
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        _fail()
    validate_registry_manifest(value)
    return value


def _uuid(value: object) -> None:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        _fail()
    try:
        if UUID(value).int == 0 or str(UUID(value)) != value:
            _fail()
    except (TypeError, ValueError):
        _fail()


def _sha(value: object, *, nonempty: bool = True) -> None:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        _fail()
    if not nonempty and value == "":
        _fail()


def _revision(value: object) -> None:
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        _fail()


def _positive(value: object) -> None:
    if type(value) is not int or value <= 0:
        _fail()


def _bool(value: object, expected: bool) -> None:
    if type(value) is not bool or value is not expected:
        _fail()


def _validate_files(files: object) -> None:
    if not isinstance(files, dict) or set(files) != FILES_KEYS:
        _fail()
    for value in files.values():
        if not isinstance(value, dict) or set(value) != {"sha256", "byte_size"}:
            _fail()
        _sha(value.get("sha256"))
        _positive(value.get("byte_size"))


def _validate_source(source: object) -> None:
    if not isinstance(source, dict) or set(source) != SOURCE_KEYS:
        _fail()
    for key in (
        "source_bundle_id",
        "authoritative_import_attempt_id",
        "import_publication_attempt_id",
    ):
        _uuid(source[key])
    _positive(source["import_attempt_number"])
    _revision(source["source_code_revision"])
    if source["source_contract_version"] != ADAPTER_SOURCE_CONTRACT_VERSION:
        _fail()
    if source["intake_contract_version"] != "phase12-adapter-intake-v1":
        _fail()
    for key in (
        "intake_manifest_sha256",
        "external_adapter_config_sha256",
        "external_adapter_model_sha256",
    ):
        _sha(source[key])
    for key in ("external_adapter_config_byte_size", "external_adapter_model_byte_size"):
        _positive(source[key])


def _validate_governance(lineage: object) -> None:
    if not isinstance(lineage, dict) or set(lineage) != GOVERNANCE_KEYS:
        _fail()
    for key in (
        "training_job_id",
        "training_job_publication_attempt_id",
        "dataset_build_id",
        "dataset_publication_attempt_id",
        "dataset_source_bundle_id",
    ):
        _uuid(lineage[key])
    for key in (
        "training_job_version",
        "training_job_attempt_number",
        "dataset_build_version",
        "dataset_publication_attempt_number",
        "dataset_train_byte_size",
        "dataset_validation_byte_size",
        "dataset_provenance_byte_size",
        "dataset_train_example_count",
        "dataset_validation_example_count",
        "dataset_source_example_count",
        "dataset_source_group_count",
        "dataset_source_reference_count",
    ):
        _positive(lineage[key])
    for key in (
        "training_job_code_revision",
        "dataset_code_revision",
    ):
        _revision(lineage[key])
    for key in (
        "training_job_manifest_sha256",
        "dataset_manifest_sha256",
        "dataset_train_sha256",
        "dataset_validation_sha256",
        "dataset_provenance_sha256",
    ):
        _sha(lineage[key])
    if (
        lineage["dataset_rights_attested"] is not True
        or lineage["evaluation_contamination_reviewed"] is not True
    ):
        _fail()
    exact_strings = {
        "training_job_artifact_contract_version": "phase11-training-job-v1",
        "training_job_manifest_contract_version": "phase11-training-job-manifest-v1",
        "training_configuration_contract_version": "phase11-training-config-v1",
        "training_dataset_info_contract_version": "phase11-dataset-info-v1",
        "training_execution_profile_contract_version": "phase11-execution-profile-v1",
        "llamafactory_version": "0.9.5",
        "dataset_artifact_contract_version": "phase10-sft-dataset-v1",
        "dataset_example_contract_version": "phase10-sft-example-v1",
        "dataset_normalization_version": "phase10-sft-normalization-v1",
        "dataset_split_version": "phase10-sft-group-split-v1",
    }
    if any(lineage.get(key) != expected for key, expected in exact_strings.items()):
        _fail()
    if lineage.get("profile_id") not in {
        "phase11-qwen3-0.6b-lora-v1",
        "phase11-qwen3-0.6b-qlora-nf4-v1",
    }:
        _fail()


def validate_registry_manifest(value: Mapping[str, object]) -> None:
    if not isinstance(value, Mapping) or set(value) != TOP_LEVEL_KEYS:
        _fail()
    if value["artifact_contract_version"] != ADAPTER_ARTIFACT_CONTRACT_VERSION:
        _fail()
    if value["manifest_contract_version"] != ADAPTER_REGISTRY_MANIFEST_CONTRACT_VERSION:
        _fail()
    for key in ("department_id", "adapter_id", "publication_attempt_id"):
        _uuid(value[key])
    _positive(value["attempt_number"])
    _revision(value["code_revision"])
    _validate_source(value["source"])
    _validate_governance(value["governance_lineage"])
    _bool(value["declared_external_training_association"], True)
    _bool(value["verified_governance_lineage"], True)
    _bool(value["verified_artifact_compatibility"], True)
    _bool(value["training_provenance_verified"], False)
    compatibility = value["compatibility"]
    if not isinstance(compatibility, dict) or set(compatibility) != COMPATIBILITY_KEYS:
        _fail()
    if compatibility["base_model_id"] != BASE_MODEL_ID:
        _fail()
    if compatibility["base_model_revision"] != BASE_MODEL_REVISION:
        _fail()
    if compatibility["base_model_license"] != BASE_MODEL_LICENSE:
        _fail()
    if compatibility["peft_version"] != PEFT_FORMAT_REFERENCE_VERSION:
        _fail()
    if compatibility["safetensors_format"] != SAFETENSORS_FORMAT_REFERENCE_VERSION:
        _fail()
    if compatibility["tensor_dtype"] not in EXPECTED_TENSOR_BYTES:
        _fail()
    if compatibility["tensor_count"] != EXPECTED_TENSOR_COUNT:
        _fail()
    if compatibility["tensor_element_count"] != EXPECTED_TENSOR_ELEMENTS:
        _fail()
    if (
        compatibility["tensor_payload_byte_size"]
        != EXPECTED_TENSOR_BYTES[compatibility["tensor_dtype"]]
    ):
        _fail()
    if compatibility["adapter_config_contract_version"] != ADAPTER_CONFIG_CONTRACT_VERSION:
        _fail()
    if compatibility["adapter_tensor_contract_version"] != ADAPTER_TENSOR_CONTRACT_VERSION:
        _fail()
    _validate_files(value["files"])


def build_registry_manifest(
    *,
    department_id: UUID,
    adapter_id: UUID,
    publication_attempt_id: UUID,
    attempt_number: int,
    code_revision: str,
    source: Mapping[str, object],
    governance_lineage: Mapping[str, object],
    files: Mapping[str, Mapping[str, object]],
    compatibility: Mapping[str, object],
) -> bytes:
    """Build and validate the exact registry manifest bytes."""

    value: dict[str, object] = {
        "artifact_contract_version": ADAPTER_ARTIFACT_CONTRACT_VERSION,
        "manifest_contract_version": ADAPTER_REGISTRY_MANIFEST_CONTRACT_VERSION,
        "department_id": str(department_id),
        "adapter_id": str(adapter_id),
        "publication_attempt_id": str(publication_attempt_id),
        "attempt_number": attempt_number,
        "code_revision": code_revision,
        "source": dict(source),
        "governance_lineage": dict(governance_lineage),
        "declared_external_training_association": True,
        "verified_governance_lineage": True,
        "verified_artifact_compatibility": True,
        "training_provenance_verified": False,
        "compatibility": dict(compatibility),
        "files": {key: dict(value) for key, value in files.items()},
    }
    validate_registry_manifest(value)
    return canonical_json_bytes(value)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "ADAPTER_ARTIFACT_CONTRACT_VERSION",
    "ADAPTER_REGISTRY_MANIFEST_CONTRACT_VERSION",
    "AdapterRegistryDomainError",
    "TOP_LEVEL_KEYS",
    "canonical_json_bytes",
    "parse_registry_manifest",
    "validate_registry_manifest",
    "build_registry_manifest",
    "sha256_bytes",
]
