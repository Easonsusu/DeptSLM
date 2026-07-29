"""Closed Phase 11 LlamaFactory job-generation contracts.

This module builds configuration authority only.  It never imports a tokenizer,
model, LlamaFactory, or any training dependency, and it never executes a job.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from uuid import UUID

TRAINING_JOB_CONTRACT_VERSION = "phase11-training-job-v1"
TRAINING_JOB_MANIFEST_VERSION = "phase11-training-job-manifest-v1"
TRAINING_CONFIG_VERSION = "phase11-training-config-v1"
DATASET_INFO_VERSION = "phase11-dataset-info-v1"
EXECUTION_PROFILE_VERSION = "phase11-execution-profile-v1"
LLAMAFACTORY_VERSION = "0.9.5"
LLAMAFACTORY_RELEASE_CONTRACT = "phase11-llamafactory-0.9.5-v1"
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
BASE_MODEL_LICENSE = "Apache-2.0"
MODEL_CACHE_DIRECTORY = f"qwen3-0.6b-{BASE_MODEL_REVISION}"
MODEL_CONTAINER_PATH = f"/runtime/deptslm/model_cache/{MODEL_CACHE_DIRECTORY}"
MAX_RECORD_CONTENT_BYTES = 7_680
TRAINING_JOB_FILES = frozenset(
    {"manifest.json", "training.yaml", "dataset_info.json", "train.jsonl", "validation.jsonl"}
)


class TrainingJobContractError(RuntimeError):
    """A content-free, fixed Phase 11 contract failure."""

    def __init__(self, code: str = "training_job_contract_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TrainingProfile:
    profile_id: str
    values: tuple[tuple[str, object], ...]


_COMMON_PROFILE_VALUES: tuple[tuple[str, object], ...] = (
    ("stage", "sft"),
    ("do_train", True),
    ("finetuning_type", "lora"),
    ("lora_target", "all"),
    ("lora_rank", 16),
    ("lora_alpha", 32),
    ("lora_dropout", 0.05),
    ("template", "qwen3_nothink"),
    ("enable_thinking", False),
    ("cutoff_len", 8192),
    ("packing", False),
    ("preprocessing_num_workers", 1),
    ("dataloader_num_workers", 0),
    ("per_device_train_batch_size", 1),
    ("per_device_eval_batch_size", 1),
    ("gradient_accumulation_steps", 8),
    ("learning_rate", 0.0001),
    ("num_train_epochs", 3.0),
    ("lr_scheduler_type", "cosine"),
    ("warmup_ratio", 0.03),
    ("weight_decay", 0.0),
    ("max_grad_norm", 1.0),
    ("bf16", True),
    ("fp16", False),
    ("seed", 42),
    ("data_seed", 42),
    ("logging_steps", 10),
    ("save_strategy", "epoch"),
    ("eval_strategy", "epoch"),
    ("report_to", "none"),
    ("plot_loss", False),
    ("overwrite_cache", False),
    ("overwrite_output_dir", False),
    ("trust_remote_code", False),
    ("flash_attn", "disabled"),
    ("use_unsloth", False),
    ("use_liger_kernel", False),
)

TRAINING_PROFILES = {
    "phase11-qwen3-0.6b-lora-v1": TrainingProfile(
        "phase11-qwen3-0.6b-lora-v1", _COMMON_PROFILE_VALUES
    ),
    "phase11-qwen3-0.6b-qlora-nf4-v1": TrainingProfile(
        "phase11-qwen3-0.6b-qlora-nf4-v1",
        _COMMON_PROFILE_VALUES
        + (
            ("quantization_bit", 4),
            ("quantization_method", "bitsandbytes"),
            ("quantization_type", "nf4"),
            ("double_quantization", True),
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class ValidatedDataset:
    train_count: int
    validation_count: int
    train_sha256: str
    validation_sha256: str
    train_byte_size: int
    validation_byte_size: int


@dataclass(frozen=True, slots=True)
class TrainingJobBundle:
    manifest: bytes
    training_yaml: bytes
    dataset_info: bytes
    train: bytes
    validation: bytes
    train_count: int
    validation_count: int


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def training_profile(profile_id: str) -> TrainingProfile:
    profile = TRAINING_PROFILES.get(profile_id)
    if profile is None:
        raise TrainingJobContractError()
    return profile


def validate_phase10_records(train: bytes, validation: bytes) -> ValidatedDataset:
    train_count = _validate_records(train)
    validation_count = _validate_records(validation)
    if train_count < 1 or validation_count < 1:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    return ValidatedDataset(
        train_count=train_count,
        validation_count=validation_count,
        train_sha256=hashlib.sha256(train).hexdigest(),
        validation_sha256=hashlib.sha256(validation).hexdigest(),
        train_byte_size=len(train),
        validation_byte_size=len(validation),
    )


def build_bundle(
    *,
    department_id: UUID,
    training_job_id: UUID,
    dataset_build_id: UUID,
    publication_attempt_id: UUID,
    execution_scope_id: UUID,
    attempt_number: int,
    code_revision: str,
    dataset_build_version: int,
    dataset_manifest_sha256: str,
    dataset_artifact_contract_version: str,
    dataset_example_contract_version: str,
    dataset_normalization_version: str,
    dataset_split_version: str,
    profile_id: str,
    dataset_rights_attested: bool,
    evaluation_contamination_reviewed: bool,
    train: bytes,
    validation: bytes,
) -> TrainingJobBundle:
    """Produce byte-stable config authority while preserving dataset bytes exactly."""

    if (
        not isinstance(department_id, UUID)
        or not isinstance(training_job_id, UUID)
        or not isinstance(dataset_build_id, UUID)
        or not isinstance(publication_attempt_id, UUID)
        or not isinstance(execution_scope_id, UUID)
        or any(
            item.int == 0
            for item in (
                department_id,
                training_job_id,
                dataset_build_id,
                publication_attempt_id,
                execution_scope_id,
            )
        )
        or type(attempt_number) is not int
        or attempt_number < 1
        or type(dataset_build_version) is not int
        or dataset_build_version < 1
        or not isinstance(code_revision, str)
        or len(code_revision) != 40
        or not isinstance(dataset_rights_attested, bool)
        or not dataset_rights_attested
        or not isinstance(evaluation_contamination_reviewed, bool)
        or not evaluation_contamination_reviewed
    ):
        raise TrainingJobContractError()
    _sha256(dataset_manifest_sha256)
    profile = training_profile(profile_id)
    dataset = validate_phase10_records(train, validation)
    job_path = f"jobs/{department_id}/{training_job_id}"
    dataset_directory = f"/runtime/deptslm/training_datasets/{job_path}"
    output_directory = (
        "/runtime/deptslm/adapters/.unregistered/"
        f"{department_id}/{training_job_id}/{execution_scope_id}"
    )
    config = (
        (("model_name_or_path", MODEL_CONTAINER_PATH),)
        + profile.values
        + (
            ("dataset_dir", dataset_directory),
            ("dataset", "deptslm_train"),
            ("eval_dataset", "deptslm_validation"),
            ("output_dir", output_directory),
        )
    )
    training_yaml = _yaml(config)
    dataset_info = canonical_json_bytes(_dataset_info()) + b"\n"
    files = {
        "training.yaml": _descriptor(training_yaml),
        "dataset_info.json": _descriptor(dataset_info),
        "train.jsonl": _descriptor(train),
        "validation.jsonl": _descriptor(validation),
    }
    manifest = {
        "artifact_contract_version": TRAINING_JOB_CONTRACT_VERSION,
        "manifest_contract_version": TRAINING_JOB_MANIFEST_VERSION,
        "configuration_contract_version": TRAINING_CONFIG_VERSION,
        "dataset_info_contract_version": DATASET_INFO_VERSION,
        "execution_profile_contract_version": EXECUTION_PROFILE_VERSION,
        "department_id": str(department_id),
        "training_job_id": str(training_job_id),
        "publication_attempt_id": str(publication_attempt_id),
        "execution_scope_id": str(execution_scope_id),
        "attempt_number": attempt_number,
        "code_revision": code_revision,
        "dataset_build_id": str(dataset_build_id),
        "dataset_build_version": dataset_build_version,
        "dataset_artifact_contract_version": dataset_artifact_contract_version,
        "dataset_example_contract_version": dataset_example_contract_version,
        "dataset_normalization_version": dataset_normalization_version,
        "dataset_split_version": dataset_split_version,
        "dataset_manifest_sha256": dataset_manifest_sha256,
        "train_example_count": dataset.train_count,
        "validation_example_count": dataset.validation_count,
        "base_model_id": BASE_MODEL_ID,
        "base_model_revision": BASE_MODEL_REVISION,
        "base_model_license": BASE_MODEL_LICENSE,
        "llamafactory_version": LLAMAFACTORY_VERSION,
        "profile_id": profile.profile_id,
        "maximum_record_content_bytes": MAX_RECORD_CONTENT_BYTES,
        "tokenizer_preflight_required": True,
        "dataset_rights_attested": True,
        "evaluation_contamination_reviewed": True,
        "files": files,
    }
    return TrainingJobBundle(
        manifest=canonical_json_bytes(manifest) + b"\n",
        training_yaml=training_yaml,
        dataset_info=dataset_info,
        train=train,
        validation=validation,
        train_count=dataset.train_count,
        validation_count=dataset.validation_count,
    )


def parse_job_manifest(raw: bytes) -> dict[str, object]:
    value = _object(raw)
    expected = {
        "artifact_contract_version",
        "manifest_contract_version",
        "configuration_contract_version",
        "dataset_info_contract_version",
        "execution_profile_contract_version",
        "department_id",
        "training_job_id",
        "publication_attempt_id",
        "execution_scope_id",
        "attempt_number",
        "code_revision",
        "dataset_build_id",
        "dataset_build_version",
        "dataset_artifact_contract_version",
        "dataset_example_contract_version",
        "dataset_normalization_version",
        "dataset_split_version",
        "dataset_manifest_sha256",
        "train_example_count",
        "validation_example_count",
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "llamafactory_version",
        "profile_id",
        "maximum_record_content_bytes",
        "tokenizer_preflight_required",
        "dataset_rights_attested",
        "evaluation_contamination_reviewed",
        "files",
    }
    if set(value) != expected:
        raise TrainingJobContractError("artifact_manifest_invalid")
    if (
        value.get("artifact_contract_version") != TRAINING_JOB_CONTRACT_VERSION
        or value.get("manifest_contract_version") != TRAINING_JOB_MANIFEST_VERSION
        or value.get("configuration_contract_version") != TRAINING_CONFIG_VERSION
        or value.get("dataset_info_contract_version") != DATASET_INFO_VERSION
        or value.get("execution_profile_contract_version") != EXECUTION_PROFILE_VERSION
        or value.get("base_model_id") != BASE_MODEL_ID
        or value.get("base_model_revision") != BASE_MODEL_REVISION
        or value.get("base_model_license") != BASE_MODEL_LICENSE
        or value.get("llamafactory_version") != LLAMAFACTORY_VERSION
        or value.get("maximum_record_content_bytes") != MAX_RECORD_CONTENT_BYTES
        or value.get("tokenizer_preflight_required") is not True
        or value.get("dataset_rights_attested") is not True
        or value.get("evaluation_contamination_reviewed") is not True
    ):
        raise TrainingJobContractError("artifact_manifest_invalid")
    training_profile(value.get("profile_id"))
    for key in (
        "department_id",
        "training_job_id",
        "publication_attempt_id",
        "execution_scope_id",
        "dataset_build_id",
    ):
        _uuid(value.get(key))
    for key in ("dataset_manifest_sha256",):
        _sha256(value.get(key))
    for key in (
        "attempt_number",
        "dataset_build_version",
        "train_example_count",
        "validation_example_count",
    ):
        _positive(value.get(key))
    if not isinstance(value.get("code_revision"), str) or len(value["code_revision"]) != 40:
        raise TrainingJobContractError("artifact_manifest_invalid")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != {
        "training.yaml",
        "dataset_info.json",
        "train.jsonl",
        "validation.jsonl",
    }:
        raise TrainingJobContractError("artifact_manifest_invalid")
    for descriptor in files.values():
        if not isinstance(descriptor, dict) or set(descriptor) != {"sha256", "byte_size"}:
            raise TrainingJobContractError("artifact_manifest_invalid")
        _sha256(descriptor.get("sha256"))
        _positive(descriptor.get("byte_size"))
    return value


def _validate_records(raw: bytes) -> int:
    if not raw or not raw.endswith(b"\n") or b"\r" in raw:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TrainingJobContractError("dataset_artifact_mismatch") from error
    count = 0
    for line in text.splitlines():
        if not line:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        value = _object(line.encode("utf-8"), "dataset_artifact_mismatch")
        if set(value) != {"example_id", "messages"}:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        _uuid(value.get("example_id"), "dataset_artifact_mismatch")
        messages = value.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        roles: list[str] = []
        content_bytes = 0
        for message in messages:
            if not isinstance(message, dict) or set(message) != {"role", "content"}:
                raise TrainingJobContractError("dataset_artifact_mismatch")
            role, content = message.get("role"), message.get("content")
            if not isinstance(role, str) or not isinstance(content, str) or not content:
                raise TrainingJobContractError("dataset_artifact_mismatch")
            if any(_unsafe(character) for character in content):
                raise TrainingJobContractError("dataset_artifact_mismatch")
            roles.append(role)
            content_bytes += len(content.encode("utf-8"))
        if roles != ["user", "assistant"] or content_bytes > MAX_RECORD_CONTENT_BYTES:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        count += 1
    return count


def _dataset_info() -> dict[str, object]:
    mapping = {
        "formatting": "sharegpt",
        "columns": {"messages": "messages"},
        "tags": {
            "role_tag": "role",
            "content_tag": "content",
            "user_tag": "user",
            "assistant_tag": "assistant",
        },
    }
    return {
        "deptslm_train": {**mapping, "file_name": "train.jsonl"},
        "deptslm_validation": {**mapping, "file_name": "validation.jsonl"},
    }


def _yaml(values: tuple[tuple[str, object], ...]) -> bytes:
    lines: list[str] = []
    for key, value in values:
        if not isinstance(key, str) or not key.replace("_", "").isalnum():
            raise TrainingJobContractError()
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            rendered = str(value)
        elif (
            isinstance(value, str)
            and value
            and all(character not in value for character in '\n\r:#[]{}&*!|>@`"')
        ):
            rendered = value
        else:
            raise TrainingJobContractError()
        lines.append(f"{key}: {rendered}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _descriptor(value: bytes) -> dict[str, object]:
    if not value:
        raise TrainingJobContractError()
    return {"sha256": hashlib.sha256(value).hexdigest(), "byte_size": len(value)}


def _object(raw: bytes, code: str = "artifact_manifest_invalid") -> dict[str, object]:
    def duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise TrainingJobContractError(code) from error
    if not isinstance(value, dict):
        raise TrainingJobContractError(code)
    return value


def _uuid(value: object, code: str = "artifact_manifest_invalid") -> UUID:
    try:
        parsed = UUID(value) if isinstance(value, str) else value
    except ValueError as error:
        raise TrainingJobContractError(code) from error
    if not isinstance(parsed, UUID) or parsed.int == 0:
        raise TrainingJobContractError(code)
    return parsed


def _positive(value: object) -> int:
    if type(value) is not int or value < 1:
        raise TrainingJobContractError("artifact_manifest_invalid")
    return value


def _sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise TrainingJobContractError("artifact_manifest_invalid")
    return value


def _unsafe(character: str) -> bool:
    value = ord(character)
    category = unicodedata.category(character)
    return (
        value == 0
        or category in {"Cf", "Cs"}
        or (category == "Cc" and character not in {"\t", "\n"})
        or value == 0x034F
        or 0xFDD0 <= value <= 0xFDEF
        or value & 0xFFFF in {0xFFFE, 0xFFFF}
    )
