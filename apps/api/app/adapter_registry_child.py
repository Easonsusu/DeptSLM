"""Secret-free fixed child that builds one opaque adapter registry stage."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import sys

from app.adapter_contract import (
    MAX_CONFIG_BYTES,
    MAX_SAFETENSORS_HEADER_BYTES,
    canonical_adapter_config_bytes,
    parse_external_adapter_config,
    validate_safetensors_metadata,
)
from app.adapter_registry_domain import (
    ADAPTER_ARTIFACT_CONTRACT_VERSION,
    ADAPTER_REGISTRY_MANIFEST_CONTRACT_VERSION,
    build_registry_manifest,
    canonical_json_bytes,
)
from app.training_job_domain import TrainingJobContractError, parse_job_manifest

MAX_REQUEST_FRAME_BYTES = 128 * 1024
MAX_RESPONSE_FRAME_BYTES = 384 * 1024
MAX_MODEL_BYTES = 44_040_192
_MARKER = ".deptslm-adapter-registry-stage-owner"
FINAL_FILES = frozenset({"adapter_config.json", "adapter_model.safetensors", "manifest.json"})
SOURCE_SNAPSHOT_KEYS = frozenset(
    {
        "source_bundle_id",
        "authoritative_attempt_id",
        "publication_attempt_id",
        "attempt_number",
        "version",
        "code_revision",
        "source_contract_version",
        "intake_contract_version",
        "config_contract_version",
        "tensor_contract_version",
        "intake_manifest_sha256",
        "adapter_config_sha256",
        "adapter_config_byte_size",
        "adapter_model_sha256",
        "adapter_model_byte_size",
        "peft_version",
        "safetensors_format",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
        "imported_by_user_id",
    }
)
GOVERNANCE_SNAPSHOT_KEYS = frozenset(
    {
        "training_job_id",
        "training_job_version",
        "training_job_publication_attempt_id",
        "training_job_attempt_number",
        "training_job_code_revision",
        "training_job_manifest_sha256",
        "training_job_profile_id",
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
SOURCE_MANIFEST_KEYS = frozenset(
    {
        "source_contract_version",
        "intake_contract_version",
        "config_contract_version",
        "tensor_contract_version",
        "department_id",
        "source_bundle_id",
        "import_attempt_id",
        "publication_attempt_id",
        "attempt_number",
        "imported_by_user_id",
        "code_revision",
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "peft_version",
        "safetensors_format",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
        "files",
    }
)
CHILD_ERROR_CODES = frozenset(
    {
        "adapter_source_artifact_mismatch",
        "adapter_source_authority_changed",
        "training_job_artifact_mismatch",
        "training_job_authority_changed",
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
        "adapter_registry_manifest_invalid",
        "adapter_registry_publication_failed",
        "adapter_registry_authority_changed",
        "adapter_input_unsafe",
    }
)


class AdapterRegistryChildError(RuntimeError):
    def __init__(self, code: str = "adapter_registry_publication_failed") -> None:
        self.code = code if code in CHILD_ERROR_CODES else "adapter_registry_publication_failed"
        super().__init__(self.code)


def _exact_request(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    expected = {
        "source_config_fd",
        "source_model_fd",
        "source_manifest_fd",
        "training_manifest_fd",
        "stage_fd",
        "source_config_size",
        "source_model_size",
        "source_manifest_size",
        "training_manifest_size",
        "department_id",
        "adapter_id",
        "publication_attempt_id",
        "attempt_number",
        "code_revision",
        "source",
        "governance_lineage",
    }
    if set(value) != expected:
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    for key in (
        "source_config_fd",
        "source_model_fd",
        "source_manifest_fd",
        "training_manifest_fd",
        "stage_fd",
    ):
        if type(value[key]) is not int or value[key] < 0:
            raise AdapterRegistryChildError("adapter_input_unsafe")
    for key in (
        "source_config_size",
        "source_model_size",
        "source_manifest_size",
        "training_manifest_size",
        "attempt_number",
    ):
        if type(value[key]) is not int or value[key] <= 0:
            raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    for key in ("department_id", "adapter_id", "publication_attempt_id"):
        if not isinstance(value[key], str) or len(value[key]) != 36:
            raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    if not isinstance(value["code_revision"], str) or len(value["code_revision"]) != 40:
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    if not isinstance(value["source"], dict) or not isinstance(value["governance_lineage"], dict):
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    if (
        set(value["source"]) != SOURCE_SNAPSHOT_KEYS
        or set(value["governance_lineage"]) != GOVERNANCE_SNAPSHOT_KEYS
    ):
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    if any(isinstance(item, (dict, list, tuple)) for item in value["source"].values()):
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    if any(isinstance(item, (dict, list, tuple)) for item in value["governance_lineage"].values()):
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    return value


def _private_fd(descriptor: int, *, directory: bool, writable: bool = False) -> os.stat_result:
    metadata = os.fstat(descriptor)
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        (directory and not stat.S_ISDIR(metadata.st_mode))
        or (not directory and not stat.S_ISREG(metadata.st_mode))
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or mode not in ({0o700} if directory else {0o600})
        or (writable and not metadata.st_mode & stat.S_IWUSR)
    ):
        raise AdapterRegistryChildError("adapter_input_unsafe")
    return metadata


def _read_bounded(descriptor: int, size: int, maximum: int) -> bytes:
    if size <= 0 or size > maximum:
        raise AdapterRegistryChildError("adapter_file_too_large")
    metadata = _private_fd(descriptor, directory=False)
    if metadata.st_size != size:
        raise AdapterRegistryChildError("adapter_source_authority_changed")
    output = bytearray()
    offset = 0
    while offset < size:
        block = os.pread(descriptor, min(64 * 1024, size - offset), offset)
        if not block:
            raise AdapterRegistryChildError("adapter_source_authority_changed")
        output.extend(block)
        offset += len(block)
    if _private_fd(descriptor, directory=False).st_size != size:
        raise AdapterRegistryChildError("adapter_source_authority_changed")
    return bytes(output)


def _closed_object(raw: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = item
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError("noncanonical")
    return value


def _validate_source_manifest(
    raw: bytes,
    request: dict[str, object],
    source: dict[str, object],
    config_size: int,
    model_size: int,
    config_digest: str,
) -> None:
    value = _closed_object(raw)
    if set(value) != SOURCE_MANIFEST_KEYS:
        raise ValueError("source manifest keys")
    expected = {
        "source_contract_version": source["source_contract_version"],
        "intake_contract_version": source["intake_contract_version"],
        "config_contract_version": source["config_contract_version"],
        "tensor_contract_version": source["tensor_contract_version"],
        "department_id": request["department_id"],
        "source_bundle_id": source["source_bundle_id"],
        "import_attempt_id": source["authoritative_attempt_id"],
        "publication_attempt_id": source["publication_attempt_id"],
        "attempt_number": source["attempt_number"],
        "code_revision": source["code_revision"],
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": source["peft_version"],
        "safetensors_format": source["safetensors_format"],
        "tensor_dtype": source["tensor_dtype"],
        "tensor_count": source["tensor_count"],
        "tensor_element_count": source["tensor_element_count"],
        "tensor_payload_byte_size": source["tensor_payload_byte_size"],
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError("source manifest authority")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != {
        "adapter_config.json",
        "adapter_model.safetensors",
    }:
        raise ValueError("source manifest files")
    config_file = files.get("adapter_config.json")
    model_file = files.get("adapter_model.safetensors")
    if (
        not isinstance(config_file, dict)
        or not isinstance(model_file, dict)
        or set(config_file) != {"sha256", "byte_size"}
        or set(model_file) != {"sha256", "byte_size"}
        or config_file.get("sha256") != source["adapter_config_sha256"]
        or config_file.get("byte_size") != config_size
        or config_file.get("sha256") != config_digest
        or model_file.get("sha256") != source["adapter_model_sha256"]
        or model_file.get("byte_size") != model_size
    ):
        raise ValueError("source manifest digest")


def _validate_training_manifest(
    manifest: dict[str, object], request: dict[str, object], governance: dict[str, object]
) -> None:
    expected = {
        "department_id": request["department_id"],
        "training_job_id": governance["training_job_id"],
        "publication_attempt_id": governance["training_job_publication_attempt_id"],
        "attempt_number": governance["training_job_attempt_number"],
        "code_revision": governance["training_job_code_revision"],
        "dataset_build_id": governance["dataset_build_id"],
        "dataset_build_version": governance["dataset_build_version"],
        "dataset_artifact_contract_version": governance["dataset_artifact_contract_version"],
        "dataset_example_contract_version": governance["dataset_example_contract_version"],
        "dataset_normalization_version": governance["dataset_normalization_version"],
        "dataset_split_version": governance["dataset_split_version"],
        "dataset_manifest_sha256": governance["dataset_manifest_sha256"],
        "train_example_count": governance["dataset_train_example_count"],
        "validation_example_count": governance["dataset_validation_example_count"],
        "profile_id": governance["training_job_profile_id"],
        "artifact_contract_version": governance["training_job_artifact_contract_version"],
        "manifest_contract_version": governance["training_job_manifest_contract_version"],
        "configuration_contract_version": governance["training_configuration_contract_version"],
        "dataset_info_contract_version": governance["training_dataset_info_contract_version"],
        "execution_profile_contract_version": governance[
            "training_execution_profile_contract_version"
        ],
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "llamafactory_version": governance["llamafactory_version"],
        "dataset_rights_attested": governance["dataset_rights_attested"],
        "evaluation_contamination_reviewed": governance["evaluation_contamination_reviewed"],
        "maximum_record_content_bytes": 7680,
        "tokenizer_preflight_required": True,
    }
    if any(manifest.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError("training manifest authority")
    if (
        hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
        != governance["training_job_manifest_sha256"]
    ):
        raise ValueError("training manifest digest")


def _copy_model(
    source_fd: int, stage_fd: int, expected_sha: str, expected_size: int
) -> tuple[str, int]:
    if expected_size <= 0 or expected_size > MAX_MODEL_BYTES:
        raise AdapterRegistryChildError("adapter_file_too_large")
    metadata = _private_fd(source_fd, directory=False)
    if metadata.st_size != expected_size:
        raise AdapterRegistryChildError("adapter_source_authority_changed")
    try:
        destination = os.open(
            "adapter_model.safetensors",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=stage_fd,
        )
    except OSError as error:
        raise AdapterRegistryChildError("adapter_registry_publication_failed") from error
    digest = hashlib.sha256()
    total = 0
    try:
        while total < expected_size:
            block = os.pread(source_fd, min(1024 * 1024, expected_size - total), total)
            if not block:
                raise AdapterRegistryChildError("adapter_source_authority_changed")
            digest.update(block)
            written = 0
            while written < len(block):
                written += os.write(destination, block[written:])
            total += len(block)
        os.fsync(destination)
    except OSError as error:
        raise AdapterRegistryChildError("adapter_registry_publication_failed") from error
    finally:
        os.close(destination)
    if total != expected_size or digest.hexdigest() != expected_sha:
        raise AdapterRegistryChildError("adapter_source_artifact_mismatch")
    return digest.hexdigest(), total


def build_registry_stage(request: dict[str, object]) -> dict[str, object]:
    value = _exact_request(request)
    source_config_fd = int(value["source_config_fd"])
    source_model_fd = int(value["source_model_fd"])
    source_manifest_fd = int(value["source_manifest_fd"])
    training_manifest_fd = int(value["training_manifest_fd"])
    stage_fd = int(value["stage_fd"])
    _private_fd(stage_fd, directory=True, writable=True)
    config_raw = _read_bounded(source_config_fd, int(value["source_config_size"]), MAX_CONFIG_BYTES)
    model_size = int(value["source_model_size"])
    source_manifest_raw = _read_bounded(
        source_manifest_fd, int(value["source_manifest_size"]), 256 * 1024
    )
    training_manifest_raw = _read_bounded(
        training_manifest_fd, int(value["training_manifest_size"]), 256 * 1024
    )
    source = dict(value["source"])
    governance = dict(value["governance_lineage"])
    config_digest = hashlib.sha256(config_raw).hexdigest()
    if config_digest != source.get("adapter_config_sha256") or len(config_raw) != source.get(
        "adapter_config_byte_size"
    ):
        raise AdapterRegistryChildError("adapter_source_artifact_mismatch")
    try:
        parse_external_adapter_config(config_raw)
    except Exception as error:
        code = getattr(error, "code", "adapter_config_invalid")
        raise AdapterRegistryChildError(code) from error
    try:
        _validate_source_manifest(
            source_manifest_raw,
            value,
            source,
            int(value["source_config_size"]),
            model_size,
            config_digest,
        )
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AdapterRegistryChildError("adapter_source_authority_changed") from error
    try:
        training_manifest = parse_job_manifest(training_manifest_raw)
        if canonical_json_bytes(training_manifest).rstrip(b"\n") + b"\n" != training_manifest_raw:
            raise ValueError("training manifest canonical")
        _validate_training_manifest(training_manifest, value, governance)
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        TrainingJobContractError,
    ) as error:
        raise AdapterRegistryChildError("training_job_artifact_mismatch") from error
    expected_sha = source.get("adapter_model_sha256")
    if not isinstance(expected_sha, str) or len(expected_sha) != 64:
        raise AdapterRegistryChildError("adapter_source_artifact_mismatch")
    # Read only the fixed safetensors header; values are never deserialized.
    try:
        validate_safetensors_metadata(
            _PreadReader(source_model_fd, min(model_size, 8 + MAX_SAFETENSORS_HEADER_BYTES)),
            model_size,
        )
    except Exception as error:
        code = getattr(error, "code", "adapter_header_invalid")
        raise AdapterRegistryChildError(code) from error
    canonical_config = canonical_adapter_config_bytes()
    _write_file(stage_fd, "adapter_config.json", canonical_config)
    model_sha, model_bytes = _copy_model(source_model_fd, stage_fd, expected_sha, model_size)
    files = {
        "adapter_config.json": {
            "sha256": hashlib.sha256(canonical_config).hexdigest(),
            "byte_size": len(canonical_config),
        },
        "adapter_model.safetensors": {"sha256": model_sha, "byte_size": model_bytes},
    }
    manifest_source = {
        "source_bundle_id": source["source_bundle_id"],
        "authoritative_import_attempt_id": source["authoritative_attempt_id"],
        "import_publication_attempt_id": source["publication_attempt_id"],
        "import_attempt_number": source["attempt_number"],
        "source_code_revision": source["code_revision"],
        "source_contract_version": source["source_contract_version"],
        "intake_contract_version": source["intake_contract_version"],
        "intake_manifest_sha256": source["intake_manifest_sha256"],
        "external_adapter_config_sha256": source["adapter_config_sha256"],
        "external_adapter_config_byte_size": source["adapter_config_byte_size"],
        "external_adapter_model_sha256": source["adapter_model_sha256"],
        "external_adapter_model_byte_size": source["adapter_model_byte_size"],
    }
    compatibility = {
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "base_model_license": "Apache-2.0",
        "peft_version": source["peft_version"],
        "safetensors_format": source["safetensors_format"],
        "tensor_dtype": source["tensor_dtype"],
        "tensor_count": source["tensor_count"],
        "tensor_element_count": source["tensor_element_count"],
        "tensor_payload_byte_size": source["tensor_payload_byte_size"],
        "adapter_config_contract_version": source["config_contract_version"],
        "adapter_tensor_contract_version": source["tensor_contract_version"],
    }
    manifest_governance = dict(value["governance_lineage"])
    manifest_governance["profile_id"] = manifest_governance.pop("training_job_profile_id")
    manifest = build_registry_manifest(
        department_id=_uuid_text(value["department_id"]),
        adapter_id=_uuid_text(value["adapter_id"]),
        publication_attempt_id=_uuid_text(value["publication_attempt_id"]),
        attempt_number=int(value["attempt_number"]),
        code_revision=str(value["code_revision"]),
        source=manifest_source,
        governance_lineage=manifest_governance,
        files=files,
        compatibility=compatibility,
    )
    _write_file(stage_fd, "manifest.json", manifest)
    os.fsync(stage_fd)
    result = {
        "publication_manifest": json.loads(manifest[:-1].decode("utf-8")),
        "artifact_contract_version": ADAPTER_ARTIFACT_CONTRACT_VERSION,
        "manifest_contract_version": ADAPTER_REGISTRY_MANIFEST_CONTRACT_VERSION,
        "registry_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "registry_manifest_byte_size": len(manifest),
        "registry_adapter_config_sha256": files["adapter_config.json"]["sha256"],
        "registry_adapter_config_byte_size": files["adapter_config.json"]["byte_size"],
        "registry_adapter_model_sha256": model_sha,
        "registry_adapter_model_byte_size": model_bytes,
        "tensor_dtype": source["tensor_dtype"],
        "tensor_count": source["tensor_count"],
        "tensor_element_count": source["tensor_element_count"],
        "tensor_payload_byte_size": source["tensor_payload_byte_size"],
    }
    return result


def _uuid_text(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    return value  # domain validation performs canonical UUID validation


def _write_file(stage_fd: int, name: str, raw: bytes) -> None:
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=stage_fd
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _PreadReader:
    def __init__(self, descriptor: int, limit: int) -> None:
        self.descriptor = descriptor
        self.limit = limit
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if type(size) is not int or size < 0 or self.offset + size > self.limit:
            raise ValueError("bounded read")
        raw = os.pread(self.descriptor, size, self.offset)
        if len(raw) != size:
            raise ValueError("short read")
        self.offset += size
        return raw


def _read_frame() -> dict[str, object]:
    prefix = _read_stdin(4)
    size = struct.unpack("!I", prefix)[0]
    if not 1 <= size <= MAX_REQUEST_FRAME_BYTES:
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    value = json.loads(_read_stdin(size).decode("utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"operation", "request"}
        or value["operation"] != "build_registry"
    ):
        raise AdapterRegistryChildError("adapter_registry_manifest_invalid")
    return _exact_request(value["request"])


def _read_stdin(size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        block = sys.stdin.buffer.read(size - len(output))
        if not block:
            raise AdapterRegistryChildError("adapter_registry_publication_failed")
        output.extend(block)
    return bytes(output)


def _write_frame(value: dict[str, object]) -> None:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if not 1 <= len(raw) <= MAX_RESPONSE_FRAME_BYTES:
        raw = b'{"status":"error","code":"adapter_registry_manifest_invalid"}'
    sys.stdout.buffer.write(struct.pack("!I", len(raw)) + raw)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        result = build_registry_stage(_read_frame())
    except AdapterRegistryChildError as error:
        _write_frame({"status": "error", "code": error.code})
        return 1
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        _write_frame({"status": "error", "code": "adapter_registry_publication_failed"})
        return 1
    _write_frame({"status": "ok", "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_REQUEST_FRAME_BYTES",
    "MAX_RESPONSE_FRAME_BYTES",
    "CHILD_ERROR_CODES",
    "AdapterRegistryChildError",
    "build_registry_stage",
    "main",
]
