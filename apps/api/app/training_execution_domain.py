"""Closed, content-free contracts for the Phase 14.1 execution control plane.

This module deliberately contains no model, tokenizer, LlamaFactory, Qdrant, or
training-runtime dependency.  It describes authority snapshots and safe protocol
values only.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.training_job_domain import (
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    LLAMAFACTORY_VERSION,
    TRAINING_JOB_FILES,
)

EXECUTION_CONTRACT_VERSION = "phase14-training-execution-v1"
REAL_RUNTIME_CONTRACT_VERSION = "phase14-training-runtime-v1"
EXECUTION_MODEL_ID = BASE_MODEL_ID
EXECUTION_MODEL_REVISION = BASE_MODEL_REVISION
EXECUTION_MODEL_LICENSE = BASE_MODEL_LICENSE
EXECUTION_LLAMFACTORY_VERSION = LLAMAFACTORY_VERSION
EXECUTION_PROFILES = frozenset({"phase11-qwen3-0.6b-lora-v1", "phase11-qwen3-0.6b-qlora-nf4-v1"})
EXECUTION_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancel_requested",
    "cancelled",
)
EXECUTION_ATTEMPT_STATUSES = (
    "registered",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "reclaimed",
)
RUNTIME_RESULT_CLASSIFICATIONS = frozenset(
    {
        "process_ready",
        "execution_started",
        "execution_succeeded",
        "execution_failed",
        "execution_cancelled",
    }
)
EXECUTION_ERROR_CODES = frozenset(
    {
        "training_job_unavailable",
        "training_job_authority_changed",
        "training_job_artifact_missing",
        "training_job_artifact_mismatch",
        "input_snapshot_failed",
        "runtime_unavailable",
        "runtime_protocol_invalid",
        "department_unavailable",
        "requester_unauthorized",
        "claim_lost",
        "cancelled",
        "worker_shutdown",
        "worker_timeout",
        "database_unavailable",
    }
)
RUNTIME_ERROR_CODES = frozenset(
    {
        "runtime_unavailable",
        "runtime_protocol_invalid",
        "runtime_environment_invalid",
        "runtime_hardware_unsupported",
        "runtime_model_unavailable",
        "runtime_dependency_mismatch",
        "runtime_auth_failed",
        "runtime_busy",
        "training_config_invalid",
        "child_start_failed",
        "child_failed",
        "child_timeout",
        "runtime_disconnected",
        "output_limit_exceeded",
        "output_invalid",
        "runtime_cleanup_failed",
        "cancelled",
        "worker_shutdown",
        "worker_timeout",
    }
)
# The queue and persistence validators share one safe vocabulary. Keeping the
# runtime-specific codes here prevents a private data-plane error from being
# rewritten to a generic protocol failure.
EXECUTION_ERROR_CODES = EXECUTION_ERROR_CODES | RUNTIME_ERROR_CODES


class TrainingExecutionError(RuntimeError):
    """A safe fixed-code execution contract error."""

    def __init__(self, code: str = "runtime_protocol_invalid") -> None:
        if code not in EXECUTION_ERROR_CODES:
            code = "runtime_protocol_invalid"
        self.code = code
        super().__init__(code)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


# These fields are every content-free authority field copied from Phase 11.  The
# list is intentionally explicit so a new Phase 11 field cannot silently escape
# the enqueue snapshot contract.
AUTHORITY_FIELDS = (
    "id",
    "department_id",
    "dataset_build_id",
    "requested_by_user_id",
    "status",
    "review_status",
    "profile_id",
    "base_model_id",
    "base_model_revision",
    "base_model_license",
    "llamafactory_version",
    "artifact_contract_version",
    "manifest_contract_version",
    "configuration_contract_version",
    "dataset_info_contract_version",
    "execution_profile_contract_version",
    "dataset_artifact_contract_version",
    "dataset_example_contract_version",
    "dataset_normalization_version",
    "dataset_split_version",
    "dataset_build_version",
    "dataset_manifest_sha256",
    "dataset_source_bundle_id",
    "dataset_status",
    "dataset_review_status",
    "dataset_publication_attempt_id",
    "dataset_publication_attempt_number",
    "dataset_code_revision",
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
    "execution_scope_id",
    "worker_id",
    "claim_token",
    "claimed_at",
    "lease_expires_at",
    "cancellation_requested_at",
    "publication_attempt_id",
    "attempt_number",
    "code_revision",
    "train_example_count",
    "validation_example_count",
    "maximum_record_content_bytes",
    "result_manifest_sha256",
    "training_config_sha256",
    "training_config_byte_size",
    "dataset_info_sha256",
    "dataset_info_byte_size",
    "train_sha256",
    "train_byte_size",
    "validation_sha256",
    "validation_byte_size",
    "publication_manifest",
    "artifact_cleanup_confirmed_at",
    "error_code",
    "requested_at",
    "started_at",
    "finished_at",
    "reviewed_by_user_id",
    "reviewed_at",
    "archived_at",
    "purged_at",
    "version",
    "created_at",
    "updated_at",
)


def training_job_authority_snapshot(job: object) -> dict[str, Any]:
    """Return the complete immutable Phase 11 authority snapshot."""

    missing = [name for name in AUTHORITY_FIELDS if not hasattr(job, name)]
    if missing:
        raise TrainingExecutionError("training_job_authority_changed")
    return {name: _json_value(getattr(job, name)) for name in AUTHORITY_FIELDS}


def authority_fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()


def execution_authority_fingerprint(
    *, execution_id: UUID, execution_code_revision: str, snapshot: dict[str, Any]
) -> str:
    """Hash the complete Phase 11 snapshot under the Phase 14 contract."""

    return authority_fingerprint(
        {
            "execution_contract_version": EXECUTION_CONTRACT_VERSION,
            "job": snapshot,
            "execution_id": str(execution_id),
            "execution_code_revision": execution_code_revision,
        }
    )


def runtime_fingerprint(*, execution_id: UUID, attempt_id: UUID, authority: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "execution_id": str(execution_id),
                "attempt_id": str(attempt_id),
                "authority": authority,
            }
        )
    ).hexdigest()


def real_runtime_fingerprint(
    *,
    execution_id: UUID,
    attempt_id: UUID,
    authority_fingerprint_value: str,
    input_snapshot_fingerprint: str,
    runtime_contract_version: str,
    llamafactory_version: str,
    dependency_lock_sha256: str,
    environment_profile_id: str,
    environment_fingerprint: str,
    hardware_profile_id: str,
    hardware_fingerprint: str,
    base_model_id: str,
    base_model_revision: str,
    profile_id: str,
    execution_code_revision: str,
    output_stage_fingerprint: str,
) -> str:
    """Return the versioned authority fingerprint for one real runtime.

    The fake Phase 14.1 fingerprint intentionally keeps its historical meaning;
    this helper is used only by real Phase 14.2 results.
    """

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "runtime_fingerprint_version": "phase14-real-runtime-fingerprint-v1",
                "execution_id": str(execution_id),
                "attempt_id": str(attempt_id),
                "authority_fingerprint": authority_fingerprint_value,
                "input_snapshot_fingerprint": input_snapshot_fingerprint,
                "runtime_contract_version": runtime_contract_version,
                "llamafactory_version": llamafactory_version,
                "dependency_lock_sha256": dependency_lock_sha256,
                "environment_profile_id": environment_profile_id,
                "environment_fingerprint": environment_fingerprint,
                "hardware_profile_id": hardware_profile_id,
                "hardware_fingerprint": hardware_fingerprint,
                "base_model_id": base_model_id,
                "base_model_revision": base_model_revision,
                "profile_id": profile_id,
                "execution_code_revision": execution_code_revision,
                "output_stage_fingerprint": output_stage_fingerprint,
            }
        )
    ).hexdigest()


def _sha256_text(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_runtime_result(
    *,
    department_id: UUID,
    execution_id: UUID,
    attempt_id: UUID,
    training_job_id: UUID,
    authority_fingerprint_value: str,
    input_snapshot_fingerprint: str,
    result: object,
) -> tuple[str, str | None, str]:
    """Validate an injected test runtime's content-free response."""

    if not isinstance(result, dict):
        raise TrainingExecutionError()
    allowed = {
        "department_id",
        "execution_id",
        "attempt_id",
        "training_job_id",
        "authority_fingerprint",
        "input_snapshot_fingerprint",
        "runtime_fingerprint",
        "classification",
        "error_code",
    }
    if set(result) != allowed:
        raise TrainingExecutionError()
    if (
        result["department_id"] != str(department_id)
        or result["execution_id"] != str(execution_id)
        or result["attempt_id"] != str(attempt_id)
        or result["training_job_id"] != str(training_job_id)
    ):
        raise TrainingExecutionError()
    if (
        result["authority_fingerprint"] != authority_fingerprint_value
        or result["input_snapshot_fingerprint"] != input_snapshot_fingerprint
        or not isinstance(result["runtime_fingerprint"], str)
        or len(result["runtime_fingerprint"]) != 64
        or result["runtime_fingerprint"]
        != runtime_fingerprint(
            execution_id=execution_id,
            attempt_id=attempt_id,
            authority=authority_fingerprint_value,
        )
    ):
        raise TrainingExecutionError()
    classification = result["classification"]
    error_code = result["error_code"]
    if classification not in RUNTIME_RESULT_CLASSIFICATIONS:
        raise TrainingExecutionError()
    if error_code is not None and error_code not in RUNTIME_ERROR_CODES:
        raise TrainingExecutionError()
    if classification in {"execution_failed", "execution_cancelled"} and error_code is None:
        raise TrainingExecutionError()
    if (
        classification in {"process_ready", "execution_started", "execution_succeeded"}
        and error_code is not None
    ):
        raise TrainingExecutionError()
    return (
        classification,
        error_code,
        runtime_fingerprint(
            execution_id=execution_id, attempt_id=attempt_id, authority=authority_fingerprint_value
        ),
    )


def validate_real_runtime_result(
    *,
    department_id: UUID,
    execution_id: UUID,
    attempt_id: UUID,
    training_job_id: UUID,
    authority_fingerprint_value: str,
    input_snapshot_fingerprint: str,
    profile_id: str,
    base_model_id: str,
    base_model_revision: str,
    execution_code_revision: str,
    dependency_lock_sha256: str,
    environment_profile_id: str,
    environment_fingerprint: str,
    result: object,
) -> tuple[str, str | None, str, dict[str, object]]:
    """Validate and independently fingerprint a real runtime response."""

    if not isinstance(result, dict):
        raise TrainingExecutionError("runtime_protocol_invalid")
    required = {
        "department_id",
        "execution_id",
        "attempt_id",
        "training_job_id",
        "authority_fingerprint",
        "input_snapshot_fingerprint",
        "runtime_kind",
        "runtime_contract_version",
        "dependency_lock_sha256",
        "environment_profile_id",
        "environment_fingerprint",
        "hardware_profile_id",
        "hardware_fingerprint",
        "runtime_fingerprint",
        "classification",
        "error_code",
        "output_stage_fingerprint",
        "output_file_count",
        "output_total_bytes",
    }
    if set(result) != required or result.get("runtime_kind") != "real":
        raise TrainingExecutionError("runtime_protocol_invalid")
    if any(
        result.get(key) != value
        for key, value in {
            "department_id": str(department_id),
            "execution_id": str(execution_id),
            "attempt_id": str(attempt_id),
            "training_job_id": str(training_job_id),
            "authority_fingerprint": authority_fingerprint_value,
            "input_snapshot_fingerprint": input_snapshot_fingerprint,
            "runtime_contract_version": REAL_RUNTIME_CONTRACT_VERSION,
            "dependency_lock_sha256": dependency_lock_sha256,
            "environment_profile_id": environment_profile_id,
            "environment_fingerprint": environment_fingerprint,
        }.items()
    ):
        raise TrainingExecutionError("runtime_protocol_invalid")
    classification = result.get("classification")
    error_code = result.get("error_code")
    if classification in {"execution_failed", "execution_cancelled"}:
        if not isinstance(error_code, str) or error_code not in RUNTIME_ERROR_CODES:
            raise TrainingExecutionError("runtime_protocol_invalid")
        return (
            classification,
            error_code,
            None,
            {
                "runtime_kind": "real",
                "runtime_contract_version": REAL_RUNTIME_CONTRACT_VERSION,
                "dependency_lock_sha256": dependency_lock_sha256,
                "environment_profile_id": environment_profile_id,
                "environment_fingerprint": environment_fingerprint,
            },
        )
    if (
        not _sha256_text(result.get("hardware_fingerprint"))
        or not isinstance(result.get("hardware_profile_id"), str)
        or not _sha256_text(result.get("output_stage_fingerprint"))
        or type(result.get("output_file_count")) is not int
        or result["output_file_count"] < 1
        or type(result.get("output_total_bytes")) is not int
        or result["output_total_bytes"] < 1
        or error_code is not None
        or classification != "execution_succeeded"
    ):
        raise TrainingExecutionError("runtime_protocol_invalid")
    expected = real_runtime_fingerprint(
        execution_id=execution_id,
        attempt_id=attempt_id,
        authority_fingerprint_value=authority_fingerprint_value,
        input_snapshot_fingerprint=input_snapshot_fingerprint,
        runtime_contract_version=REAL_RUNTIME_CONTRACT_VERSION,
        llamafactory_version=EXECUTION_LLAMFACTORY_VERSION,
        dependency_lock_sha256=dependency_lock_sha256,
        environment_profile_id=environment_profile_id,
        environment_fingerprint=environment_fingerprint,
        hardware_profile_id=result["hardware_profile_id"],
        hardware_fingerprint=result["hardware_fingerprint"],
        base_model_id=base_model_id,
        base_model_revision=base_model_revision,
        profile_id=profile_id,
        execution_code_revision=execution_code_revision,
        output_stage_fingerprint=result["output_stage_fingerprint"],
    )
    if result.get("runtime_fingerprint") != expected:
        raise TrainingExecutionError("runtime_protocol_invalid")
    return (
        "execution_succeeded",
        None,
        expected,
        {
            "runtime_kind": "real",
            "runtime_contract_version": REAL_RUNTIME_CONTRACT_VERSION,
            "dependency_lock_sha256": dependency_lock_sha256,
            "environment_profile_id": environment_profile_id,
            "environment_fingerprint": environment_fingerprint,
            "hardware_profile_id": result["hardware_profile_id"],
            "hardware_fingerprint": result["hardware_fingerprint"],
            "output_stage_fingerprint": result["output_stage_fingerprint"],
            "output_file_count": result["output_file_count"],
            "output_total_bytes": result["output_total_bytes"],
        },
    )


__all__ = [
    "AUTHORITY_FIELDS",
    "EXECUTION_ATTEMPT_STATUSES",
    "EXECUTION_CONTRACT_VERSION",
    "REAL_RUNTIME_CONTRACT_VERSION",
    "EXECUTION_ERROR_CODES",
    "EXECUTION_LLAMFACTORY_VERSION",
    "EXECUTION_MODEL_ID",
    "EXECUTION_MODEL_LICENSE",
    "EXECUTION_MODEL_REVISION",
    "EXECUTION_PROFILES",
    "EXECUTION_STATUSES",
    "RUNTIME_RESULT_CLASSIFICATIONS",
    "TRAINING_JOB_FILES",
    "TrainingExecutionError",
    "authority_fingerprint",
    "canonical_json_bytes",
    "execution_authority_fingerprint",
    "real_runtime_fingerprint",
    "training_job_authority_snapshot",
    "validate_runtime_result",
    "validate_real_runtime_result",
]
