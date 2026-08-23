"""Process boundary for Phase 14.1 execution.

The API and normal worker contain only this protocol.  A fake implementation is
provided by tests through dependency injection; there is no production fake
environment switch and no LlamaFactory/model dependency here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.training_execution_domain import (
    EXECUTION_ERROR_CODES,
    TrainingExecutionError,
    runtime_fingerprint,
)


@dataclass(frozen=True, slots=True)
class TrainingRuntimeRequest:
    contract_version: str
    department_id: UUID
    execution_id: UUID
    attempt_id: UUID
    training_job_id: UUID
    publication_attempt_id: UUID
    authority_fingerprint: str
    input_snapshot_fingerprint: str
    profile_id: str
    base_model_id: str
    base_model_revision: str
    attempt_namespace: UUID
    input_descriptor: int
    scratch_descriptor: int
    logs_descriptor: int
    output_stage_descriptor: int


@dataclass(frozen=True, slots=True)
class TrainingRuntimeResult:
    department_id: UUID
    execution_id: UUID
    attempt_id: UUID
    training_job_id: UUID
    authority_fingerprint: str
    input_snapshot_fingerprint: str
    runtime_fingerprint: str
    classification: str
    error_code: str | None = None

    def as_closed_mapping(self) -> dict[str, object]:
        return {
            "department_id": str(self.department_id),
            "execution_id": str(self.execution_id),
            "attempt_id": str(self.attempt_id),
            "training_job_id": str(self.training_job_id),
            "authority_fingerprint": self.authority_fingerprint,
            "input_snapshot_fingerprint": self.input_snapshot_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "classification": self.classification,
            "error_code": self.error_code,
        }


class TrainingExecutionRuntime(Protocol):
    def run(
        self,
        request: TrainingRuntimeRequest,
        *,
        should_stop: Callable[[], bool],
        heartbeat: Callable[[], None],
    ) -> TrainingRuntimeResult | dict[str, object]: ...


class UnavailableTrainingRuntime:
    """Production default: fail closed until a reviewed runtime is supplied."""

    def run(
        self,
        request: TrainingRuntimeRequest,
        *,
        should_stop: Callable[[], bool],
        heartbeat: Callable[[], None],
    ) -> TrainingRuntimeResult:
        del should_stop, heartbeat
        return TrainingRuntimeResult(
            request.department_id,
            request.execution_id,
            request.attempt_id,
            request.training_job_id,
            request.authority_fingerprint,
            request.input_snapshot_fingerprint,
            runtime_fingerprint(
                execution_id=request.execution_id,
                attempt_id=request.attempt_id,
                authority=request.authority_fingerprint,
            ),
            "execution_failed",
            "runtime_unavailable",
        )


def validate_runtime_request(request: TrainingRuntimeRequest) -> None:
    if (
        request.contract_version != "phase14-training-execution-v1"
        or not isinstance(request.department_id, UUID)
        or not isinstance(request.execution_id, UUID)
        or not isinstance(request.attempt_id, UUID)
        or not isinstance(request.training_job_id, UUID)
        or not isinstance(request.publication_attempt_id, UUID)
        or not isinstance(request.attempt_namespace, UUID)
        or len(request.authority_fingerprint) != 64
        or len(request.input_snapshot_fingerprint) != 64
        or any(
            not isinstance(value, int) or value < 0
            for value in (
                request.input_descriptor,
                request.scratch_descriptor,
                request.logs_descriptor,
                request.output_stage_descriptor,
            )
        )
    ):
        raise TrainingExecutionError("runtime_protocol_invalid")


def validate_runtime_result_shape(
    result: TrainingRuntimeResult | dict[str, object],
) -> dict[str, object]:
    if isinstance(result, TrainingRuntimeResult):
        result = result.as_closed_mapping()
    if not isinstance(result, dict):
        raise TrainingExecutionError("runtime_protocol_invalid")
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
        raise TrainingExecutionError("runtime_protocol_invalid")
    if result.get("error_code") is not None and result["error_code"] not in EXECUTION_ERROR_CODES:
        raise TrainingExecutionError("runtime_protocol_invalid")
    return result


__all__ = [
    "TrainingExecutionRuntime",
    "TrainingRuntimeRequest",
    "TrainingRuntimeResult",
    "UnavailableTrainingRuntime",
    "validate_runtime_request",
    "validate_runtime_result_shape",
]
