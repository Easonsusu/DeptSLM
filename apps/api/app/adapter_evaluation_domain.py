"""Closed Phase 12.2 adapter-paired evaluation contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from app.evaluation_domain import (
    MAX_BASE_SEED,
    AggregateMetrics,
    GateEvaluation,
    QualityGates,
    evaluate_gates,
)

ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION = "phase12-adapter-evaluation-v1"
ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION = "phase12-adapter-evaluation-artifact-v1"
ADAPTER_EVALUATION_SEED_POLICY_VERSION = "phase12-adapter-evaluation-seed-v1"
ADAPTER_EVALUATION_GATE_POLICY_VERSION = "phase9-quality-gates-v1"
ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION = "phase9-deterministic-metrics-v1"
ADAPTER_EVALUATION_MAX_CASES = 500

ADAPTER_EVALUATION_ERROR_CODES = frozenset(
    {
        "adapter_unavailable",
        "adapter_authority_changed",
        "adapter_artifact_missing",
        "adapter_artifact_mismatch",
        "suite_unavailable",
        "suite_authority_changed",
        "department_unavailable",
        "requester_unauthorized",
        "qdrant_unavailable",
        "retrieval_authority_failed",
        "source_artifact_missing",
        "source_artifact_mismatch",
        "base_runtime_unavailable",
        "base_runtime_timeout",
        "candidate_runtime_unavailable",
        "candidate_runtime_timeout",
        "candidate_adapter_load_failed",
        "invalid_generation_response",
        "invalid_citation",
        "result_publication_failed",
        "claim_lost",
        "worker_shutdown",
        "cancelled",
        "database_unavailable",
    }
)


class AdapterEvaluationTarget(StrEnum):
    BASELINE = "baseline"
    CANDIDATE = "candidate"


@dataclass(frozen=True, slots=True)
class MetricDeltas:
    values: dict[str, Decimal]

    def as_dict(self) -> dict[str, Decimal]:
        return dict(self.values)


def derive_adapter_evaluation_base_seed(
    department_id: UUID, adapter_id: UUID, adapter_version: int, suite_id: UUID
) -> int:
    if (
        not all(
            isinstance(value, UUID) and value.int != 0
            for value in (department_id, adapter_id, suite_id)
        )
        or type(adapter_version) is not int
        or adapter_version <= 0
    ):
        raise ValueError("invalid adapter evaluation seed inputs")
    payload = (
        f"{ADAPTER_EVALUATION_SEED_POLICY_VERSION}:{department_id}:{adapter_id}:"
        f"{adapter_version}:{suite_id}"
    ).encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & MAX_BASE_SEED


def derive_adapter_evaluation_case_seed(
    department_id: UUID,
    adapter_id: UUID,
    adapter_version: int,
    suite_id: UUID,
    case_id: UUID,
) -> int:
    base = derive_adapter_evaluation_base_seed(department_id, adapter_id, adapter_version, suite_id)
    payload = f"{ADAPTER_EVALUATION_SEED_POLICY_VERSION}:{base}:{case_id}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & MAX_BASE_SEED


def compute_metric_deltas(
    baseline: AggregateMetrics, candidate: AggregateMetrics
) -> dict[str, Decimal]:
    """Return candidate minus baseline using Decimal arithmetic only."""

    with localcontext() as context:
        context.prec = 40
        return {
            name: candidate.as_dict()[name] - baseline.as_dict()[name]
            for name in baseline.as_dict()
        }


def evaluate_adapter_gates(metrics: AggregateMetrics, gates: QualityGates) -> GateEvaluation:
    return evaluate_gates(metrics, gates)


def safe_error_code(value: object) -> str:
    return (
        value
        if isinstance(value, str) and value in ADAPTER_EVALUATION_ERROR_CODES
        else "database_unavailable"
    )


def validate_metric_delta_mapping(value: object) -> dict[str, Decimal]:
    if not isinstance(value, dict) or set(value) != set(AggregateMetrics.__dataclass_fields__):
        raise ValueError("invalid metric deltas")
    result: dict[str, Decimal] = {}
    for name in AggregateMetrics.__dataclass_fields__:
        if name not in value or not isinstance(value[name], Decimal) or not value[name].is_finite():
            raise ValueError("invalid metric deltas")
        result[name] = value[name]
    return result
