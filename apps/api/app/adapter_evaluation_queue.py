"""Server-time claims and non-revivable attempts for adapter evaluations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_evaluation_artifacts import AdapterEvaluationArtifactStore, ArtifactDigest
from app.adapter_evaluation_domain import compute_metric_deltas
from app.adapter_registry_domain import canonical_json_bytes
from app.authorization import DepartmentScope
from app.evaluation_domain import AggregateMetrics, EvaluationContractError, GateEvaluation
from app.evaluation_suites import (
    GroundTruthAuthoritySnapshot,
    revalidate_canonical_suite_authority_in_transaction,
)
from app.models import (
    Adapter,
    AdapterEvaluationAttempt,
    AdapterEvaluationCaseResult,
    AdapterEvaluationEvidence,
    AdapterEvaluationRun,
    AdapterPurgeOperation,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    Department,
    EvaluationSuite,
    Membership,
    PersistentAuditEvent,
    UserIdentity,
)

_EVALUATOR_ROLE_NAMES = ("system_admin", "department_admin", "instructor")


class AdapterEvaluationQueueError(RuntimeError):
    _codes = {
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
        "cancelled",
        "worker_shutdown",
        "database_unavailable",
    }

    def __init__(self, code: str = "database_unavailable") -> None:
        self.code = code if code in self._codes else "database_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ClaimedAdapterEvaluation:
    id: UUID
    department_id: UUID
    adapter_id: UUID
    suite_id: UUID
    worker_id: UUID
    claim_token: UUID
    attempt_number: int
    publication_attempt_id: UUID
    stale_claim_token: UUID | None = None
    stale_publication_attempt_id: UUID | None = None
    stale_attempt_number: int | None = None


def _owned(job: ClaimedAdapterEvaluation):
    return (
        AdapterEvaluationRun.id == job.id,
        AdapterEvaluationRun.department_id == job.department_id,
        AdapterEvaluationRun.adapter_id == job.adapter_id,
        AdapterEvaluationRun.suite_id == job.suite_id,
        AdapterEvaluationRun.status == "running",
        AdapterEvaluationRun.worker_id == job.worker_id,
        AdapterEvaluationRun.claim_token == job.claim_token,
        AdapterEvaluationRun.result_publication_attempt_id == job.publication_attempt_id,
        AdapterEvaluationRun.attempt_number == job.attempt_number,
    )


def _live():
    return AdapterEvaluationRun.lease_expires_at > func.clock_timestamp()


def claim_next(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int, code_revision: str
) -> ClaimedAdapterEvaluation | None:
    try:
        with factory() as session, session.begin():
            candidate = session.execute(
                select(AdapterEvaluationRun.id, AdapterEvaluationRun.department_id)
                .where(
                    AdapterEvaluationRun.code_revision == code_revision,
                    or_(
                        AdapterEvaluationRun.status == "queued",
                        (AdapterEvaluationRun.status == "running")
                        & (AdapterEvaluationRun.lease_expires_at <= func.clock_timestamp()),
                    ),
                )
                .order_by(AdapterEvaluationRun.created_at, AdapterEvaluationRun.id)
                .limit(1)
            ).one_or_none()
            if candidate is None:
                return None
            run = session.execute(
                select(AdapterEvaluationRun)
                .where(
                    AdapterEvaluationRun.id == candidate.id,
                    AdapterEvaluationRun.department_id == candidate.department_id,
                    AdapterEvaluationRun.code_revision == code_revision,
                    or_(
                        AdapterEvaluationRun.status == "queued",
                        (AdapterEvaluationRun.status == "running")
                        & (AdapterEvaluationRun.lease_expires_at <= func.clock_timestamp()),
                    ),
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if run is None:
                return None
            now = session.scalar(select(func.clock_timestamp()))
            department_id = run.department_id
            if run.status == "running" and run.cancellation_requested_at is not None:
                run.status = "cancelled"
                run.gate_status = "pending"
                run.error_code = "cancelled"
                run.worker_id = None
                run.claim_token = None
                run.lease_expires_at = None
                run.finished_at = now
                run.cancelled_at = now
                run.version += 1
                previous = session.execute(
                    select(AdapterEvaluationAttempt)
                    .where(
                        AdapterEvaluationAttempt.run_id == run.id,
                        AdapterEvaluationAttempt.department_id == department_id,
                        AdapterEvaluationAttempt.attempt_number == run.attempt_number,
                        AdapterEvaluationAttempt.status == "running",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if previous is not None:
                    previous.status = "cancelled"
                    previous.error_code = "cancelled"
                    previous.worker_id = None
                    previous.claim_token = None
                    previous.lease_expires_at = None
                    previous.finished_at = now
                    previous.version += 1
                return None
            adapter = session.execute(
                select(Adapter)
                .where(
                    Adapter.id == run.adapter_id,
                    Adapter.department_id == department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            suite = session.execute(
                select(EvaluationSuite)
                .where(
                    EvaluationSuite.id == run.suite_id,
                    EvaluationSuite.department_id == department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            was_running = run.status == "running"
            if (
                adapter is None
                or adapter.status != "validated"
                or suite is None
                or suite.status != "active"
                or _active_purge(session, department_id, run.adapter_id)
            ):
                run.status = "failed"
                run.error_code = (
                    "adapter_authority_changed"
                    if (
                        adapter is None
                        or adapter.status != "validated"
                        or _active_purge(session, department_id, run.adapter_id)
                    )
                    else "suite_authority_changed"
                )
                if was_running and run.result_publication_attempt_id is not None:
                    previous = session.execute(
                        select(AdapterEvaluationAttempt)
                        .where(
                            AdapterEvaluationAttempt.run_id == run.id,
                            AdapterEvaluationAttempt.department_id == department_id,
                            AdapterEvaluationAttempt.attempt_number == run.attempt_number,
                            AdapterEvaluationAttempt.publication_attempt_id
                            == run.result_publication_attempt_id,
                            AdapterEvaluationAttempt.status == "running",
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    if previous is not None:
                        previous.status = "failed"
                        previous.error_code = run.error_code
                        previous.worker_id = None
                        previous.claim_token = None
                        previous.lease_expires_at = None
                        previous.finished_at = now
                        previous.version += 1
                run.worker_id = None
                run.claim_token = None
                run.lease_expires_at = None
                run.finished_at = now
                run.version += 1
                return None
            stale_token = run.claim_token if run.status == "running" else None
            stale_pub = run.result_publication_attempt_id
            stale_attempt = run.attempt_number if stale_pub is not None else None
            if stale_pub is not None:
                previous = session.execute(
                    select(AdapterEvaluationAttempt)
                    .where(
                        AdapterEvaluationAttempt.run_id == run.id,
                        AdapterEvaluationAttempt.department_id == department_id,
                        AdapterEvaluationAttempt.attempt_number == run.attempt_number,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if previous is not None:
                    previous.status = "reclaimed"
                    previous.error_code = "claim_lost"
                    previous.finished_at = now
                    previous.worker_id = None
                    previous.claim_token = None
                    previous.lease_expires_at = None
                    previous.version += 1
                run.attempt_number += 1
            claim_token = uuid4()
            publication = uuid4()
            run.status = "running"
            run.worker_id = worker_id
            run.claim_token = claim_token
            run.result_publication_attempt_id = publication
            run.claimed_at = now
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            run.started_at = run.started_at or now
            run.finished_at = None
            run.error_code = None
            run.completed_case_count = 0
            run.version += 1
            session.add(
                AdapterEvaluationAttempt(
                    id=uuid4(),
                    department_id=department_id,
                    run_id=run.id,
                    adapter_id=run.adapter_id,
                    suite_id=run.suite_id,
                    attempt_number=run.attempt_number,
                    publication_attempt_id=publication,
                    worker_id=worker_id,
                    claim_token=claim_token,
                    lease_expires_at=run.lease_expires_at,
                    status="running",
                    code_revision=run.code_revision,
                )
            )
            session.flush()
            return ClaimedAdapterEvaluation(
                run.id,
                department_id,
                run.adapter_id,
                run.suite_id,
                worker_id,
                claim_token,
                run.attempt_number,
                publication,
                stale_token,
                stale_pub,
                stale_attempt,
            )
    except AdapterEvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise AdapterEvaluationQueueError() from error


def require_live_claim(factory: sessionmaker[Session], job: ClaimedAdapterEvaluation) -> None:
    try:
        with factory() as session:
            row = session.execute(
                select(AdapterEvaluationRun.cancellation_requested_at).where(*_owned(job), _live())
            ).one_or_none()
            if row is None:
                raise AdapterEvaluationQueueError("claim_lost")
            if row.cancellation_requested_at is not None:
                raise AdapterEvaluationQueueError("cancelled")
    except AdapterEvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise AdapterEvaluationQueueError() from error


def renew_lease(
    factory: sessionmaker[Session], job: ClaimedAdapterEvaluation, lease_seconds: int
) -> None:
    try:
        with factory.begin() as session:
            result = session.execute(
                update(AdapterEvaluationRun)
                .where(
                    *_owned(job), _live(), AdapterEvaluationRun.cancellation_requested_at.is_(None)
                )
                .values(
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                    updated_at=func.clock_timestamp(),
                    version=AdapterEvaluationRun.version + 1,
                )
            )
            if result.rowcount != 1:
                state = session.execute(
                    select(AdapterEvaluationRun.cancellation_requested_at).where(
                        *_owned(job), _live()
                    )
                ).one_or_none()
                raise AdapterEvaluationQueueError(
                    "cancelled"
                    if state is not None and state.cancellation_requested_at is not None
                    else "claim_lost"
                )
    except AdapterEvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise AdapterEvaluationQueueError() from error


def fail_owned(factory: sessionmaker[Session], job: ClaimedAdapterEvaluation, code: str) -> bool:
    safe = code if code in AdapterEvaluationQueueError._codes else "database_unavailable"
    try:
        with factory.begin() as session:
            terminal_status = "cancelled" if safe == "cancelled" else "failed"
            result = session.execute(
                update(AdapterEvaluationRun)
                .where(*_owned(job), _live())
                .values(
                    status=terminal_status,
                    gate_status="pending",
                    error_code=safe,
                    worker_id=None,
                    claim_token=None,
                    lease_expires_at=None,
                    finished_at=func.clock_timestamp(),
                    cancellation_requested_at=(
                        func.coalesce(
                            AdapterEvaluationRun.cancellation_requested_at,
                            func.clock_timestamp(),
                        )
                        if safe == "cancelled"
                        else AdapterEvaluationRun.cancellation_requested_at
                    ),
                    cancelled_at=(
                        func.clock_timestamp()
                        if safe == "cancelled"
                        else AdapterEvaluationRun.cancelled_at
                    ),
                    version=AdapterEvaluationRun.version + 1,
                )
            )
            if result.rowcount == 1:
                attempt = session.execute(
                    select(AdapterEvaluationAttempt)
                    .where(
                        AdapterEvaluationAttempt.run_id == job.id,
                        AdapterEvaluationAttempt.department_id == job.department_id,
                        AdapterEvaluationAttempt.attempt_number == job.attempt_number,
                        AdapterEvaluationAttempt.publication_attempt_id
                        == job.publication_attempt_id,
                        AdapterEvaluationAttempt.status == "running",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if attempt is not None:
                    attempt.status = terminal_status
                    attempt.error_code = safe
                    attempt.worker_id = None
                    attempt.claim_token = None
                    attempt.lease_expires_at = None
                    attempt.finished_at = func.clock_timestamp()
                    attempt.version += 1
            return result.rowcount == 1
    except SQLAlchemyError as error:
        raise AdapterEvaluationQueueError() from error


def finalize_success(
    factory: sessionmaker[Session],
    job: ClaimedAdapterEvaluation,
    *,
    baseline_metrics: AggregateMetrics,
    candidate_metrics: AggregateMetrics,
    baseline_gate: GateEvaluation,
    candidate_gate: GateEvaluation,
    result_manifest_sha256: str,
    result_summary_sha256: str,
    case_results_sha256: str,
    case_results_byte_size: int,
    case_rows: tuple[dict[str, object], ...] = (),
    data_dir: Path,
    suite_cases: tuple[dict[str, object], ...],
    suite_authority: GroundTruthAuthoritySnapshot,
    result_store: AdapterEvaluationArtifactStore,
    result_manifest: dict[str, object],
    result_files: dict[str, ArtifactDigest],
) -> None:
    """Commit exactly two immutable evidence rows after final authority checks."""

    deltas = compute_metric_deltas(baseline_metrics, candidate_metrics)
    try:
        with factory.begin() as session:
            department = session.execute(
                select(Department).where(Department.id == job.department_id).with_for_update()
            ).scalar_one_or_none()
            run = session.execute(
                select(AdapterEvaluationRun).where(*_owned(job), _live()).with_for_update()
            ).scalar_one_or_none()
            if run is None:
                raise AdapterEvaluationQueueError("claim_lost")
            now = session.scalar(select(func.clock_timestamp()))
            adapter = session.execute(
                select(Adapter)
                .where(
                    Adapter.id == job.adapter_id,
                    Adapter.department_id == job.department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            suite = session.execute(
                select(EvaluationSuite)
                .where(
                    EvaluationSuite.id == job.suite_id,
                    EvaluationSuite.department_id == job.department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            registry = session.execute(
                select(AdapterRegistryAttempt)
                .where(
                    AdapterRegistryAttempt.department_id == job.department_id,
                    AdapterRegistryAttempt.adapter_id == job.adapter_id,
                    AdapterRegistryAttempt.publication_attempt_id
                    == run.registry_publication_attempt_id,
                    AdapterRegistryAttempt.attempt_number == run.registry_attempt_number,
                    AdapterRegistryAttempt.status == "succeeded",
                )
                .with_for_update()
            ).scalar_one_or_none()
            dependency = session.execute(
                select(AdapterUpstreamDependency)
                .where(
                    AdapterUpstreamDependency.department_id == job.department_id,
                    AdapterUpstreamDependency.adapter_id == job.adapter_id,
                    AdapterUpstreamDependency.status == "active",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if (
                department is None
                or department.status != "active"
                or run.cancellation_requested_at is not None
                or not _requester_authorized(
                    session, job.department_id, run.requested_by_user_id, now, lock=True
                )
                or adapter is None
                or adapter.status != "validated"
                or adapter.version != run.adapter_version
                or adapter.verified_governance_lineage is not True
                or adapter.verified_artifact_compatibility is not True
                or adapter.training_provenance_verified is not False
                or adapter.registry_manifest_sha256 != run.registry_manifest_sha256
                or adapter.registry_adapter_config_byte_size
                != run.registry_adapter_config_byte_size
                or adapter.registry_adapter_config_sha256 != run.registry_adapter_config_sha256
                or adapter.registry_adapter_model_byte_size != run.registry_adapter_model_byte_size
                or adapter.registry_adapter_model_sha256 != run.registry_adapter_model_sha256
                or adapter.base_model_id != run.base_model_id
                or adapter.base_model_revision != run.base_model_revision
                or suite is None
                or suite.status != "active"
                or suite.version != run.suite_version
                or suite.artifact_manifest_sha256 != run.suite_artifact_manifest_sha256
                or suite.canonical_cases_sha256 != run.suite_canonical_cases_sha256
                or suite.canonical_cases_byte_size != run.suite_canonical_cases_byte_size
                or suite.suite_contract_version != "phase9-evaluation-suite-v1"
                or suite.artifact_contract_version != "phase9-evaluation-artifact-v1"
                or suite.metric_contract_version != run.metric_contract_version
                or suite.gate_policy_version != run.gate_policy_version
                or registry is None
                or registry.id != run.registry_attempt_id
                or registry.version != run.registry_attempt_version
                or registry.publication_attempt_id != run.registry_publication_attempt_id
                or registry.attempt_number != run.registry_attempt_number
                or registry.ownership_manifest is None
                or not _registry_manifest_matches(registry, run)
                or dependency is None
                or dependency.status != "active"
                or dependency.id != run.dependency_id
                or dependency.version != run.dependency_version
                or dependency.department_id != run.department_id
                or dependency.adapter_id != run.adapter_id
                or _active_purge(session, job.department_id, job.adapter_id)
                or suite.retrieval_recall_at_5_min != run.retrieval_recall_at_5_min
                or suite.retrieval_mrr_at_20_min != run.retrieval_mrr_at_20_min
                or suite.answer_status_accuracy_min != run.answer_status_accuracy_min
                or suite.citation_precision_min != run.citation_precision_min
                or suite.citation_recall_min != run.citation_recall_min
                or suite.normalized_exact_match_min != run.normalized_exact_match_min
                or suite.character_f1_min != run.character_f1_min
                or suite.invalid_contract_rate_max != run.invalid_contract_rate_max
            ):
                raise AdapterEvaluationQueueError("adapter_authority_changed")
            try:
                revalidate_canonical_suite_authority_in_transaction(
                    session,
                    data_dir,
                    DepartmentScope(job.department_id),
                    suite_cases,
                    suite_authority,
                )
            except EvaluationContractError as error:
                raise AdapterEvaluationQueueError("suite_authority_changed") from error
            try:
                verified_result = result_store.verify_published(
                    DepartmentScope(job.department_id),
                    job.id,
                    job.publication_attempt_id,
                    expected_manifest=result_manifest,
                    expected_files=result_files,
                )
            except EvaluationContractError as error:
                raise AdapterEvaluationQueueError("result_publication_failed") from error
            if not _result_manifest_matches_run(result_manifest, run, job):
                raise AdapterEvaluationQueueError("result_publication_failed")
            verified_files = dict(verified_result.files)
            manifest_file = verified_files.get("manifest.json")
            summary_file = verified_files.get("summary.json")
            cases_file = verified_files.get("case_results.jsonl")
            if (
                not isinstance(manifest_file, ArtifactDigest)
                or not isinstance(summary_file, ArtifactDigest)
                or not isinstance(cases_file, ArtifactDigest)
            ):
                raise AdapterEvaluationQueueError("result_publication_failed")
            if (
                manifest_file.sha256 != result_manifest_sha256
                or summary_file.sha256 != result_summary_sha256
                or cases_file.sha256 != case_results_sha256
                or cases_file.byte_size != case_results_byte_size
            ):
                raise AdapterEvaluationQueueError("result_publication_failed")
            live_after_external_checks = session.execute(
                select(AdapterEvaluationRun.id).where(*_owned(job), _live())
            ).scalar_one_or_none()
            if live_after_external_checks is None:
                raise AdapterEvaluationQueueError("claim_lost")
            common = {
                "run_id": run.id,
                "department_id": run.department_id,
                "adapter_id": run.adapter_id,
                "suite_id": run.suite_id,
                "adapter_version": run.adapter_version,
                "base_model_id": run.base_model_id,
                "base_model_revision": run.base_model_revision,
                "metric_contract_version": run.metric_contract_version,
                "gate_policy_version": run.gate_policy_version,
                "seed_policy_version": run.seed_policy_version,
            }
            for target, metrics, gate in (
                ("baseline", baseline_metrics, baseline_gate),
                ("candidate", candidate_metrics, candidate_gate),
            ):
                values = metrics.as_dict()
                session.add(
                    AdapterEvaluationEvidence(
                        **common,
                        target=target,
                        gate_status="passed" if gate.passed else "failed",
                        failed_gate_count=gate.failed_count,
                        **values,
                        **{
                            "delta_" + name: (deltas[name] if target == "candidate" else None)
                            for name in values
                        },
                    )
                )
            if len(case_rows) != run.case_count * 2:
                raise AdapterEvaluationQueueError("result_publication_failed")
            case_ids: dict[str, set[UUID]] = {"baseline": set(), "candidate": set()}
            for row in case_rows:
                target = row.get("target")
                case_id = row.get("case_id")
                if target not in case_ids or not isinstance(case_id, UUID) or case_id.int == 0:
                    raise AdapterEvaluationQueueError("result_publication_failed")
                if case_id in case_ids[target]:
                    raise AdapterEvaluationQueueError("result_publication_failed")
                case_ids[target].add(case_id)
            if case_ids["baseline"] != case_ids["candidate"] or any(
                len(values) != run.case_count for values in case_ids.values()
            ):
                raise AdapterEvaluationQueueError("result_publication_failed")
            if case_rows:
                for row in case_rows:
                    target = row.get("target")
                    if target not in {"baseline", "candidate"}:
                        raise AdapterEvaluationQueueError("result_publication_failed")
                    session.add(
                        AdapterEvaluationCaseResult(
                            id=uuid4(),
                            run_id=run.id,
                            department_id=run.department_id,
                            adapter_id=run.adapter_id,
                            suite_id=run.suite_id,
                            target=target,
                            case_id=row["case_id"],
                            expected_status=row["expected_status"],
                            actual_status=row["actual_status"],
                            relevant_chunk_count=row["relevant_chunk_count"],
                            retrieval_candidate_count=row["retrieval_candidate_count"],
                            retrieved_relevant_at_5=row["retrieved_relevant_at_5"],
                            retrieved_relevant_at_10=row["retrieved_relevant_at_10"],
                            retrieved_relevant_at_20=row["retrieved_relevant_at_20"],
                            reciprocal_rank_at_20=row["reciprocal_rank_at_20"],
                            answer_status_correct=row["status_correct"],
                            cited_count=row["cited_count"],
                            cited_relevant_count=row["cited_relevant_count"],
                            citation_precision=row["citation_precision"],
                            citation_recall=row["citation_recall"],
                            normalized_exact_match=row["normalized_exact_match"],
                            character_f1=row["character_f1"],
                            answer_contract_valid=row["answer_contract_valid"],
                            case_gate_passed=row["case_gate_passed"],
                            error_code=row["error_code"],
                        )
                    )
            run.status = "succeeded"
            run.gate_status = "passed" if candidate_gate.passed else "failed"
            run.completed_case_count = run.case_count
            run.result_manifest_sha256 = result_manifest_sha256
            run.result_summary_sha256 = result_summary_sha256
            run.case_results_sha256 = case_results_sha256
            run.case_results_byte_size = case_results_byte_size
            run.worker_id = None
            run.claim_token = None
            run.lease_expires_at = None
            run.finished_at = now
            run.error_code = None
            run.version += 1
            attempt = session.execute(
                select(AdapterEvaluationAttempt)
                .where(
                    AdapterEvaluationAttempt.run_id == run.id,
                    AdapterEvaluationAttempt.department_id == run.department_id,
                    AdapterEvaluationAttempt.attempt_number == job.attempt_number,
                    AdapterEvaluationAttempt.publication_attempt_id == job.publication_attempt_id,
                    AdapterEvaluationAttempt.status == "running",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                raise AdapterEvaluationQueueError("claim_lost")
            attempt.status = "succeeded"
            attempt.finished_at = now
            attempt.worker_id = None
            attempt.claim_token = None
            attempt.lease_expires_at = None
            attempt.result_manifest_sha256 = result_manifest_sha256
            attempt.result_summary_sha256 = result_summary_sha256
            attempt.case_results_sha256 = case_results_sha256
            attempt.case_results_byte_size = case_results_byte_size
            attempt.version += 1
            session.add(
                PersistentAuditEvent(
                    actor_subject=None,
                    actor_user_id=run.requested_by_user_id,
                    department_id=run.department_id,
                    action="adapter.evaluation.complete",
                    resource_type="adapter_evaluation",
                    resource_id=str(run.id),
                    result="allowed",
                    reason_code="mutation_applied",
                )
            )
    except AdapterEvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise AdapterEvaluationQueueError() from error


def _active_purge(session: Session, department_id: UUID, adapter_id: UUID) -> bool:
    return (
        session.execute(
            select(AdapterPurgeOperation.id).where(
                AdapterPurgeOperation.department_id == department_id,
                AdapterPurgeOperation.adapter_id == adapter_id,
                AdapterPurgeOperation.status.in_(("registered", "deleting")),
            )
        ).scalar_one_or_none()
        is not None
    )


def _requester_authorized(
    session: Session,
    department_id: UUID,
    user_id: UUID,
    now,
    *,
    lock: bool = False,
) -> bool:
    statement = (
        select(Membership.id)
        .join(UserIdentity, UserIdentity.id == Membership.user_id)
        .where(
            Membership.department_id == department_id,
            Membership.user_id == user_id,
            Membership.status == "active",
            Membership.role.in_(_EVALUATOR_ROLE_NAMES),
            or_(Membership.expires_at.is_(None), Membership.expires_at > now),
            UserIdentity.status == "active",
        )
    )
    if lock:
        statement = statement.with_for_update(of=(Membership, UserIdentity))
    return session.execute(statement).scalar_one_or_none() is not None


def _registry_manifest_matches(registry: AdapterRegistryAttempt, run: AdapterEvaluationRun) -> bool:
    manifest = registry.ownership_manifest
    if not isinstance(manifest, dict):
        return False
    files = manifest.get("files")
    config = files.get("adapter_config.json") if isinstance(files, dict) else None
    model = files.get("adapter_model.safetensors") if isinstance(files, dict) else None
    try:
        digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    except (TypeError, ValueError):
        return False
    return (
        digest == run.registry_manifest_sha256
        and manifest.get("department_id") == str(run.department_id)
        and manifest.get("adapter_id") == str(run.adapter_id)
        and manifest.get("publication_attempt_id") == str(run.registry_publication_attempt_id)
        and manifest.get("attempt_number") == run.registry_attempt_number
        and isinstance(config, dict)
        and isinstance(model, dict)
        and config.get("sha256") == run.registry_adapter_config_sha256
        and config.get("byte_size") == run.registry_adapter_config_byte_size
        and model.get("sha256") == run.registry_adapter_model_sha256
        and model.get("byte_size") == run.registry_adapter_model_byte_size
    )


def _result_manifest_matches_run(
    manifest: dict[str, object], run: AdapterEvaluationRun, job: ClaimedAdapterEvaluation
) -> bool:
    if not isinstance(manifest, dict):
        return False
    expected = {
        "artifact_contract_version": run.artifact_contract_version,
        "department_id": str(run.department_id),
        "evaluation_id": str(run.id),
        "adapter_id": str(run.adapter_id),
        "adapter_version": run.adapter_version,
        "suite_id": str(run.suite_id),
        "publication_attempt_id": str(job.publication_attempt_id),
        "attempt_number": job.attempt_number,
        "base_seed": run.base_seed,
        "registry_publication_attempt_id": str(run.registry_publication_attempt_id),
        "registry_attempt_number": run.registry_attempt_number,
        "registry_manifest_sha256": run.registry_manifest_sha256,
        "adapter_config_sha256": run.registry_adapter_config_sha256,
        "adapter_config_byte_size": run.registry_adapter_config_byte_size,
        "adapter_model_sha256": run.registry_adapter_model_sha256,
        "adapter_model_byte_size": run.registry_adapter_model_byte_size,
        "base_model_id": run.base_model_id,
        "base_model_revision": run.base_model_revision,
        "runner_contract_version": run.runner_contract_version,
        "metric_contract_version": run.metric_contract_version,
        "gate_policy_version": run.gate_policy_version,
        "seed_policy_version": run.seed_policy_version,
        "code_revision": run.code_revision,
    }
    return all(manifest.get(key) == value for key, value in expected.items())


__all__ = [
    "AdapterEvaluationQueueError",
    "ClaimedAdapterEvaluation",
    "claim_next",
    "fail_owned",
    "finalize_success",
    "renew_lease",
    "require_live_claim",
]
