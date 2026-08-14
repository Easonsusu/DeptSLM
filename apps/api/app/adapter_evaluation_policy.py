"""Paired adapter evaluation orchestration over the production RAG policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from deptslm_worker.qdrant_adapter import DepartmentQdrant
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_evaluation_domain import derive_adapter_evaluation_case_seed
from app.authorization import DepartmentScope
from app.rag_answer_services import (
    EphemeralRagOutcome,
    PreparedRagPolicyContext,
    execute_rag_policy_lane,
    prepare_rag_policy_context,
)
from app.rag_domain import RagContractError
from app.rag_runtime_client import RagRuntimeClient
from app.rag_settings import RagSettings


class AdapterEvaluationLaneError(RuntimeError):
    """Safe lane-specific runtime failure for queue finalization."""

    _codes = frozenset(
        {
            "base_runtime_unavailable",
            "base_runtime_timeout",
            "candidate_adapter_load_failed",
            "candidate_runtime_unavailable",
            "candidate_runtime_timeout",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if code in self._codes else "candidate_runtime_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class PairedRagCaseOutcome:
    """Transient baseline/candidate outcomes sharing one retrieval context."""

    context: PreparedRagPolicyContext
    case_seed: int
    baseline: EphemeralRagOutcome
    candidate: EphemeralRagOutcome


def execute_paired_rag_case(
    factory: sessionmaker[Session],
    settings: RagSettings,
    data_dir: Path,
    scope: DepartmentScope,
    question: str,
    *,
    department_id: UUID,
    adapter_id: UUID,
    adapter_version: int,
    suite_id: UUID,
    case_id: UUID,
    baseline_runtime: RagRuntimeClient,
    candidate_runtime: RagRuntimeClient,
    qdrant: DepartmentQdrant,
    stage_callback: Callable[[str], None] | None = None,
) -> PairedRagCaseOutcome:
    """Retrieve once, then run both generation lanes with the same seed."""

    try:
        context = prepare_rag_policy_context(
            factory,
            settings,
            data_dir,
            scope,
            question,
            baseline_runtime,
            qdrant,
            stage_callback=stage_callback,
        )
    except RagContractError as error:
        mapped = _base_runtime_code(error.code)
        if mapped is not None:
            raise AdapterEvaluationLaneError(mapped) from error
        raise
    case_seed = derive_adapter_evaluation_case_seed(
        department_id, adapter_id, adapter_version, suite_id, case_id
    )
    try:
        baseline = execute_rag_policy_lane(
            context,
            data_dir,
            scope,
            baseline_runtime,
            settings,
            seed=case_seed,
            retain_contract_failure=True,
            stage_callback=stage_callback,
        )
    except RagContractError as error:
        mapped = _base_runtime_code(error.code)
        if mapped is not None:
            raise AdapterEvaluationLaneError(mapped) from error
        raise
    try:
        candidate = execute_rag_policy_lane(
            context,
            data_dir,
            scope,
            candidate_runtime,
            settings,
            seed=case_seed,
            retain_contract_failure=True,
            stage_callback=stage_callback,
        )
    except RagContractError as error:
        mapped = _candidate_runtime_code(error.code)
        if mapped is not None:
            raise AdapterEvaluationLaneError(mapped) from error
        raise
    return PairedRagCaseOutcome(context, case_seed, baseline, candidate)


def _base_runtime_code(code: str) -> str | None:
    if code in {"runtime_timeout", "generation_timeout"}:
        return "base_runtime_timeout"
    if code in {
        "runtime_unavailable",
        "generation_failed",
        "query_embedding_failed",
        "invalid_query_embedding",
    }:
        return "base_runtime_unavailable"
    return None


def _candidate_runtime_code(code: str) -> str | None:
    if code in {"runtime_timeout", "generation_timeout"}:
        return "candidate_runtime_timeout"
    if code in {
        "runtime_unavailable",
        "generation_failed",
        "candidate_runtime_unavailable",
    }:
        return "candidate_runtime_unavailable"
    if code == "candidate_adapter_load_failed":
        return code
    return None


__all__ = ["AdapterEvaluationLaneError", "PairedRagCaseOutcome", "execute_paired_rag_case"]
