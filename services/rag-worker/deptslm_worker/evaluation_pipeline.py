"""One claimed Phase 9 run through production-policy execution and publication."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.evaluation_artifacts import EvaluationArtifactStore
from app.evaluation_domain import (
    ANSWER_NORMALIZATION_VERSION,
    ARTIFACT_CONTRACT_VERSION,
    GATE_POLICY_VERSION,
    METRIC_CONTRACT_VERSION,
    RUNNER_CONTRACT_VERSION,
    SEED_DERIVATION_VERSION,
    SUITE_CONTRACT_VERSION,
    EvaluationContractError,
    QualityGates,
    aggregate_metrics,
    derive_case_seed,
    evaluate_gates,
    production_contract,
    score_case,
)
from app.evaluation_suites import (
    verify_canonical_suite_authority_without_open_transaction,
)
from app.rag_answer_services import (
    execute_rag_policy,
    revalidate_ephemeral_sources,
)
from app.rag_domain import RagContractError
from app.rag_runtime_client import RagRuntimeClient
from deptslm_worker.evaluation_queue import (
    ClaimedEvaluationRun,
    EvaluationQueueError,
    fail_owned,
    finalize_success,
    record_progress,
    renew_lease,
    require_live_claim,
    validate_claim_authority,
)
from deptslm_worker.evaluation_settings import EvaluationSettings
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError
from deptslm_worker.vector_retrieval import RetrievalBoundaryError

LOGGER = logging.getLogger("deptslm.evaluator")


class _WorkerStopped(RuntimeError):
    pass


def process_evaluation_run(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    store: EvaluationArtifactStore,
    runtime: RagRuntimeClient,
    qdrant: DepartmentQdrant,
    job: ClaimedEvaluationRun,
    should_stop: Callable[[], bool],
) -> bool:
    scope = DepartmentScope(job.department_id)
    staged = None
    try:
        require_live_claim(factory, job)
        if job.stale_claim_token is not None:
            store.cleanup_stage(scope, job.id, job.stale_claim_token, suite=False)
            require_live_claim(factory, job)
        suite = validate_claim_authority(factory, job)
        cases = tuple(
            store.iter_suite_cases(
                scope,
                suite.id,
                manifest_sha256=suite.artifact_manifest_sha256,
                cases_sha256=suite.canonical_cases_sha256,
                cases_byte_size=suite.canonical_cases_byte_size,
            )
        )
        if len(cases) != job.case_count:
            raise EvaluationQueueError("suite_artifact_mismatch")
        _verify_suite_authority(factory, settings, scope, cases)
        gates = _gates(suite)
        scores = []
        answered = 0
        insufficient = 0
        for case in cases:
            if should_stop():
                raise _WorkerStopped()
            renew_lease(factory, job, settings.lease_seconds)
            expected_status, relevant_ids, accepted_answers = _case_contract(case)
            started = time.monotonic()
            try:
                outcome = execute_rag_policy(
                    factory,
                    settings.rag,
                    settings.data_dir,
                    scope,
                    case["question"],
                    runtime,
                    qdrant,
                    seed=derive_case_seed(job.base_seed, UUID(case["case_id"])),
                )
                if time.monotonic() - started > settings.operation_timeout_seconds:
                    raise RagContractError("runtime_timeout")
                renew_lease(factory, job, settings.lease_seconds)
                revalidate_ephemeral_sources(factory, scope, outcome.supplied)
                score = score_case(
                    case_id=UUID(case["case_id"]),
                    expected_status=expected_status,
                    relevant_chunk_ids=relevant_ids,
                    accepted_answers=accepted_answers,
                    actual_status=outcome.status,
                    generated_answer=outcome.answer,
                    authorized_candidate_ids=outcome.authorized_candidate_ids,
                    cited_chunk_ids=outcome.cited_chunk_ids,
                    answer_contract_valid=True,
                )
            except RagContractError as error:
                if error.code not in {
                    "invalid_generation_response",
                    "invalid_citation",
                }:
                    raise
                score = score_case(
                    case_id=UUID(case["case_id"]),
                    expected_status=expected_status,
                    relevant_chunk_ids=relevant_ids,
                    accepted_answers=accepted_answers,
                    actual_status="failed",
                    generated_answer="",
                    authorized_candidate_ids=(),
                    cited_chunk_ids=(),
                    answer_contract_valid=False,
                    error_code=error.code,
                )
            scores.append(score)
            answered += expected_status == "answered"
            insufficient += expected_status == "insufficient_information"
            record_progress(
                factory,
                job,
                completed=len(scores),
                answered=answered,
                insufficient=insufficient,
            )
        metrics = aggregate_metrics(scores)
        gate = evaluate_gates(metrics, gates)
        _verify_suite_authority(factory, settings, scope, cases)
        require_live_claim(factory, job)
        manifest, summary = _result_values(job, metrics, gates, gate)
        staged, summary_digest = store.stage_run(
            scope,
            job.suite_id,
            job.id,
            job.claim_token,
            manifest_value=manifest,
            summary_value=summary,
            scores=tuple(scores),
        )
        require_live_claim(factory, job)
        finalize_success(
            factory,
            store,
            job,
            staged,
            summary_digest,
            tuple(scores),
            metrics,
            gate,
        )
        _event(job, "complete", "allowed", "evaluation_succeeded")
        return True
    except _WorkerStopped:
        if staged is not None:
            try:
                require_live_claim(factory, job)
                store.cleanup_stage(scope, job.id, job.claim_token, suite=False)
            except (EvaluationQueueError, EvaluationContractError):
                pass
        _event(job, "processing", "denied", "worker_shutdown")
        return False
    except EvaluationQueueError as error:
        code = error.code
    except EvaluationContractError as error:
        code = error.code
    except RagContractError as error:
        code = _rag_error_code(error.code)
    except QdrantBoundaryError:
        code = "qdrant_unavailable"
    except RetrievalBoundaryError:
        code = "retrieval_authority_failed"
    except SQLAlchemyError:
        code = "database_unavailable"
    except Exception:
        code = "generation_failed"

    if staged is not None:
        try:
            require_live_claim(factory, job, allow_cancellation=True)
            store.cleanup_stage(scope, job.id, job.claim_token, suite=False)
        except (EvaluationQueueError, EvaluationContractError):
            pass
    fail_owned(factory, job, code)
    _event(job, "complete", "denied", code)
    return False


def _verify_suite_authority(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    scope: DepartmentScope,
    cases: tuple[dict[str, object], ...],
) -> None:
    try:
        verify_canonical_suite_authority_without_open_transaction(
            factory, settings.data_dir, scope, cases
        )
    except EvaluationContractError:
        raise
    except SQLAlchemyError as error:
        raise EvaluationQueueError("database_unavailable") from error


def _case_contract(
    value: dict[str, object],
) -> tuple[str, tuple[UUID, ...], tuple[str, ...]]:
    if set(value) != {
        "case_id",
        "expected_status",
        "question",
        "relevant_sources",
        "accepted_answers",
    }:
        raise EvaluationContractError("suite_artifact_mismatch")
    expected = value.get("expected_status")
    sources = value.get("relevant_sources")
    answers = value.get("accepted_answers")
    if (
        expected not in {"answered", "insufficient_information"}
        or not isinstance(value.get("question"), str)
        or not isinstance(sources, list)
        or not isinstance(answers, list)
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    try:
        relevant = tuple(UUID(item["chunk_id"]) for item in sources)
    except (KeyError, TypeError, ValueError) as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error
    if any(not isinstance(item, str) for item in answers):
        raise EvaluationContractError("suite_artifact_mismatch")
    return expected, relevant, tuple(answers)


def _gates(suite) -> QualityGates:
    if (
        suite.suite_contract_version != SUITE_CONTRACT_VERSION
        or suite.artifact_contract_version != ARTIFACT_CONTRACT_VERSION
        or suite.metric_contract_version != METRIC_CONTRACT_VERSION
        or suite.answer_normalization_version != ANSWER_NORMALIZATION_VERSION
        or suite.gate_policy_version != GATE_POLICY_VERSION
    ):
        raise EvaluationQueueError("suite_contract_invalid")
    return QualityGates(
        **{name: getattr(suite, name) for name in QualityGates.__dataclass_fields__}
    )


def _result_values(job, metrics, gates, gate):
    contract = dict(production_contract())
    manifest = {
        "run_id": str(job.id),
        "suite_id": str(job.suite_id),
        "department_id": str(job.department_id),
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "runner_contract_version": RUNNER_CONTRACT_VERSION,
        "answer_normalization_version": ANSWER_NORMALIZATION_VERSION,
        "gate_policy_version": GATE_POLICY_VERSION,
        "seed_derivation_version": SEED_DERIVATION_VERSION,
        "base_seed": job.base_seed,
        "code_revision": job.code_revision,
        "case_count": job.case_count,
        **contract,
    }
    summary = {
        "case_count": job.case_count,
        "metrics": metrics.as_dict(),
        "gates": gates.as_dict(),
        "gate_results": gate.results,
        "gate_status": "passed" if gate.passed else "failed",
        "failed_gate_count": gate.failed_count,
    }
    return manifest, summary


def _rag_error_code(code: str) -> str:
    mapping = {
        "query_embedding_failed": "invalid_query_embedding",
        "generation_timeout": "runtime_timeout",
        "source_changed": "source_artifact_mismatch",
        "department_unavailable": "department_unavailable",
        "database_unavailable": "database_unavailable",
    }
    return mapping.get(code, code)


def _event(job: ClaimedEvaluationRun, action: str, result: str, reason: str) -> None:
    LOGGER.info(
        "evaluation_event action=%s result=%s reason=%s department_id=%s "
        "suite_id=%s run_id=%s case_count=%s",
        action,
        result,
        reason,
        job.department_id,
        job.suite_id,
        job.id,
        job.case_count,
    )
