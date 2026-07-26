"""PostgreSQL server-time claims and finalization for Phase 9 evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.evaluation_artifacts import EvaluationArtifactStore, PublishedArtifact, StagedArtifact
from app.evaluation_domain import (
    ANSWER_NORMALIZATION_VERSION,
    GATE_POLICY_VERSION,
    METRIC_CONTRACT_VERSION,
    RUNNER_CONTRACT_VERSION,
    SAFE_EVALUATION_ERROR_CODES,
    SEED_DERIVATION_VERSION,
    AggregateMetrics,
    EvaluationCaseScore,
    EvaluationContractError,
    GateEvaluation,
    production_contract,
)
from app.evaluation_suites import (
    GroundTruthAuthoritySnapshot,
    revalidate_canonical_suite_authority_in_transaction,
)
from app.models import (
    Department,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationSuite,
    Membership,
    PersistentAuditEvent,
    UserIdentity,
)

EVALUATOR_ROLE_NAMES = ("system_admin", "department_admin", "instructor")


class EvaluationQueueError(RuntimeError):
    def __init__(self, code: str = "database_unavailable") -> None:
        self.code = code if code in SAFE_EVALUATION_ERROR_CODES else "database_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ClaimedEvaluationRun:
    id: UUID
    department_id: UUID
    suite_id: UUID
    requested_by_user_id: UUID
    worker_id: UUID
    claim_token: UUID
    stale_claim_token: UUID | None
    base_seed: int
    case_count: int
    code_revision: str
    publication_attempt_id: UUID | None = None
    stale_publication_attempt_id: UUID | None = None


def claim_next(
    factory: sessionmaker[Session],
    worker_id: UUID,
    lease_seconds: int,
    code_revision: str,
) -> ClaimedEvaluationRun | None:
    try:
        with factory() as session, session.begin():
            candidate = session.execute(
                select(EvaluationRun.id, EvaluationRun.department_id)
                .where(
                    EvaluationRun.code_revision == code_revision,
                    or_(
                        EvaluationRun.status == "queued",
                        (
                            (EvaluationRun.status == "running")
                            & (EvaluationRun.lease_expires_at <= func.clock_timestamp())
                        ),
                    ),
                )
                .order_by(EvaluationRun.created_at, EvaluationRun.id)
                .limit(1)
            ).one_or_none()
            if candidate is None:
                return None
            department = session.execute(
                select(Department).where(Department.id == candidate.department_id).with_for_update()
            ).scalar_one_or_none()
            row = session.execute(
                select(EvaluationRun)
                .where(
                    EvaluationRun.id == candidate.id,
                    EvaluationRun.department_id == candidate.department_id,
                    EvaluationRun.code_revision == code_revision,
                    or_(
                        EvaluationRun.status == "queued",
                        (
                            (EvaluationRun.status == "running")
                            & (EvaluationRun.lease_expires_at <= func.clock_timestamp())
                        ),
                    ),
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                return None
            now = session.scalar(select(func.clock_timestamp()))
            if department is None or department.status != "active":
                _terminal_failure(row, now, "department_unavailable")
                return None
            stale = row.claim_token if row.status == "running" else None
            stale_publication = row.publication_attempt_id
            if stale_publication is not None:
                row.attempt_number += 1
            claim_token = uuid4()
            publication_attempt_id = uuid4()
            row.status = "running"
            row.worker_id = worker_id
            row.claim_token = claim_token
            row.publication_attempt_id = publication_attempt_id
            row.claimed_at = now
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.started_at = row.started_at or now
            row.finished_at = None
            row.error_code = None
            row.completed_case_count = 0
            row.answered_case_count = 0
            row.insufficient_case_count = 0
            row.version += 1
            session.flush()
            return ClaimedEvaluationRun(
                id=row.id,
                department_id=row.department_id,
                suite_id=row.suite_id,
                requested_by_user_id=row.requested_by_user_id,
                worker_id=worker_id,
                claim_token=claim_token,
                stale_claim_token=stale,
                base_seed=row.base_seed,
                case_count=row.case_count,
                code_revision=row.code_revision,
                publication_attempt_id=publication_attempt_id,
                stale_publication_attempt_id=stale_publication,
            )
    except SQLAlchemyError as error:
        raise EvaluationQueueError() from error


def require_live_claim(
    factory: sessionmaker[Session],
    job: ClaimedEvaluationRun,
    *,
    allow_cancellation: bool = False,
) -> None:
    try:
        with factory() as session:
            row = session.execute(
                select(EvaluationRun.cancellation_requested_at).where(
                    *_owned_claim(job), _live_lease(), *_fixed_contract(job)
                )
            ).one_or_none()
            if row is None:
                raise EvaluationQueueError("claim_lost")
            if row.cancellation_requested_at is not None and not allow_cancellation:
                raise EvaluationQueueError("cancelled")
    except EvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise EvaluationQueueError("database_unavailable") from error


def renew_lease(
    factory: sessionmaker[Session], job: ClaimedEvaluationRun, lease_seconds: int
) -> None:
    try:
        with factory.begin() as session:
            result = session.execute(
                update(EvaluationRun)
                .where(
                    *_owned_claim(job),
                    _live_lease(),
                    *_fixed_contract(job),
                    EvaluationRun.cancellation_requested_at.is_(None),
                )
                .values(
                    lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds),
                    updated_at=func.clock_timestamp(),
                    version=EvaluationRun.version + 1,
                )
            )
            if result.rowcount != 1:
                state = session.execute(
                    select(EvaluationRun.cancellation_requested_at).where(
                        *_owned_claim(job), _live_lease()
                    )
                ).one_or_none()
                raise EvaluationQueueError(
                    "cancelled"
                    if state is not None and state.cancellation_requested_at is not None
                    else "claim_lost"
                )
    except EvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise EvaluationQueueError("database_unavailable") from error


def record_progress(
    factory: sessionmaker[Session],
    job: ClaimedEvaluationRun,
    *,
    completed: int,
    answered: int,
    insufficient: int,
) -> None:
    if (
        not 0 <= completed <= job.case_count
        or answered < 0
        or insufficient < 0
        or answered + insufficient > completed
    ):
        raise EvaluationQueueError("database_unavailable")
    try:
        with factory.begin() as session:
            result = session.execute(
                update(EvaluationRun)
                .where(
                    *_owned_claim(job),
                    _live_lease(),
                    *_fixed_contract(job),
                    EvaluationRun.cancellation_requested_at.is_(None),
                )
                .values(
                    completed_case_count=completed,
                    answered_case_count=answered,
                    insufficient_case_count=insufficient,
                    updated_at=func.clock_timestamp(),
                    version=EvaluationRun.version + 1,
                )
            )
            if result.rowcount != 1:
                raise EvaluationQueueError("claim_lost")
    except EvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise EvaluationQueueError("database_unavailable") from error


def fail_owned(factory: sessionmaker[Session], job: ClaimedEvaluationRun, code: str) -> bool:
    if code not in SAFE_EVALUATION_ERROR_CODES:
        code = "database_unavailable"
    if code == "cancelled":
        return cancel_owned(factory, job)
    try:
        with factory.begin() as session:
            result = session.execute(
                update(EvaluationRun)
                .where(
                    *_owned_claim(job),
                    _live_lease(),
                    EvaluationRun.cancellation_requested_at.is_(None),
                )
                .values(
                    status="failed",
                    gate_status="pending",
                    worker_id=None,
                    claim_token=None,
                    lease_expires_at=None,
                    finished_at=func.clock_timestamp(),
                    error_code=code,
                    updated_at=func.clock_timestamp(),
                    version=EvaluationRun.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def cancel_owned(factory: sessionmaker[Session], job: ClaimedEvaluationRun) -> bool:
    try:
        with factory.begin() as session:
            result = session.execute(
                update(EvaluationRun)
                .where(*_owned_claim(job), _live_lease())
                .values(
                    status="cancelled",
                    gate_status="pending",
                    worker_id=None,
                    claim_token=None,
                    lease_expires_at=None,
                    cancellation_requested_at=func.coalesce(
                        EvaluationRun.cancellation_requested_at, func.clock_timestamp()
                    ),
                    finished_at=func.clock_timestamp(),
                    error_code="cancelled",
                    updated_at=func.clock_timestamp(),
                    version=EvaluationRun.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def finalize_success(
    factory: sessionmaker[Session],
    store: EvaluationArtifactStore,
    job: ClaimedEvaluationRun,
    published_artifact: PublishedArtifact,
    scores: tuple[EvaluationCaseScore, ...],
    metrics: AggregateMetrics,
    gate: GateEvaluation,
    cases: tuple[dict[str, object], ...],
    authority: GroundTruthAuthoritySnapshot,
) -> None:
    scope = DepartmentScope(job.department_id)
    try:
        with factory.begin() as session:
            department = session.execute(
                select(Department).where(Department.id == job.department_id).with_for_update()
            ).scalar_one_or_none()
            suite = session.execute(
                select(EvaluationSuite)
                .where(
                    EvaluationSuite.id == job.suite_id,
                    EvaluationSuite.department_id == job.department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            run = session.execute(
                select(EvaluationRun)
                .where(*_owned_claim(job), _live_lease(), *_fixed_contract(job))
                .with_for_update()
            ).scalar_one_or_none()
            now = session.scalar(select(func.clock_timestamp()))
            if department is None or department.status != "active":
                raise EvaluationQueueError("department_unavailable")
            if suite is None or suite.status != "active":
                raise EvaluationQueueError("suite_source_stale")
            if run is None:
                raise EvaluationQueueError("claim_lost")
            if run.cancellation_requested_at is not None:
                raise EvaluationQueueError("cancelled")
            if run.publication_attempt_id != job.publication_attempt_id:
                raise EvaluationQueueError("claim_lost")
            if not _requester_authorized(
                session,
                job.department_id,
                job.requested_by_user_id,
                now,
                lock=True,
            ):
                raise EvaluationQueueError("requester_unauthorized")
            if (
                len(scores) != job.case_count
                or len(cases) != job.case_count
                or tuple(str(score.case_id) for score in scores)
                != tuple(case.get("case_id") for case in cases)
                or tuple(score.expected_status for score in scores)
                != tuple(case.get("expected_status") for case in cases)
            ):
                raise EvaluationQueueError("database_unavailable")
            revalidate_canonical_suite_authority_in_transaction(
                session,
                store.root.parent,
                scope,
                cases,
                authority,
            )
            verified_artifact = store.verify_published_run(
                scope,
                job.id,
                expected_manifest=_expected_result_manifest(job),
                expected=StagedArtifact(
                    published_artifact.path,
                    published_artifact.path,
                    published_artifact.manifest,
                    published_artifact.payload,
                    tuple(
                        (name, digest)
                        for name, digest in (
                            ("manifest.json", published_artifact.manifest),
                            ("case_results.jsonl", published_artifact.payload),
                            ("summary.json", published_artifact.summary),
                        )
                        if digest is not None
                    ),
                ),
            )
            for score in scores:
                session.add(
                    EvaluationCaseResult(
                        run_id=job.id,
                        department_id=job.department_id,
                        suite_id=job.suite_id,
                        **{
                            name: getattr(score, name)
                            for name in EvaluationCaseScore.__dataclass_fields__
                            if name != "case_id"
                        },
                        case_id=score.case_id,
                    )
                )
            values = metrics.as_dict()
            for name, value in values.items():
                setattr(run, name, value)
            run.status = "succeeded"
            run.gate_status = "passed" if gate.passed else "failed"
            run.completed_case_count = len(scores)
            run.answered_case_count = sum(item.expected_status == "answered" for item in scores)
            run.insufficient_case_count = sum(
                item.expected_status == "insufficient_information" for item in scores
            )
            run.failed_gate_count = gate.failed_count
            if verified_artifact.summary is None:
                raise EvaluationQueueError("result_publication_failed")
            run.result_manifest_sha256 = verified_artifact.manifest.sha256
            run.result_summary_sha256 = verified_artifact.summary.sha256
            run.case_results_sha256 = verified_artifact.payload.sha256
            run.case_results_byte_size = verified_artifact.payload.byte_size
            run.worker_id = None
            run.claim_token = None
            run.lease_expires_at = None
            run.finished_at = now
            run.error_code = None
            run.version += 1
            session.add(
                PersistentAuditEvent(
                    actor_subject=None,
                    actor_user_id=job.requested_by_user_id,
                    department_id=job.department_id,
                    action="evaluation.run.complete",
                    resource_type="evaluation_run",
                    resource_id=str(job.id),
                    result="allowed",
                    reason_code="mutation_applied",
                )
            )
            session.flush()
    except EvaluationQueueError:
        raise
    except EvaluationContractError:
        raise
    except SQLAlchemyError as error:
        raise EvaluationQueueError("database_unavailable") from error


def reconcile_stale_publication(
    factory: sessionmaker[Session],
    store: EvaluationArtifactStore,
    job: ClaimedEvaluationRun,
) -> None:
    """A replacement removes only a manifest-proven predecessor publication."""

    if job.stale_publication_attempt_id is None:
        return
    require_live_claim(factory, job)
    try:
        store.remove_owned_run_final(
            DepartmentScope(job.department_id),
            job.id,
            job.stale_publication_attempt_id,
        )
    except EvaluationContractError as error:
        raise EvaluationQueueError("artifact_reconciliation_failed") from error
    require_live_claim(factory, job)


def validate_claim_authority(
    factory: sessionmaker[Session], job: ClaimedEvaluationRun
) -> EvaluationSuite:
    try:
        with factory() as session:
            now = session.scalar(select(func.clock_timestamp()))
            run = session.execute(
                select(EvaluationRun).where(
                    *_owned_claim(job), _live_lease(), *_fixed_contract(job)
                )
            ).scalar_one_or_none()
            department = session.get(Department, job.department_id)
            suite = session.execute(
                select(EvaluationSuite).where(
                    EvaluationSuite.id == job.suite_id,
                    EvaluationSuite.department_id == job.department_id,
                )
            ).scalar_one_or_none()
            if run is None:
                raise EvaluationQueueError("claim_lost")
            if run.cancellation_requested_at is not None:
                raise EvaluationQueueError("cancelled")
            if department is None or department.status != "active":
                raise EvaluationQueueError("department_unavailable")
            if suite is None or suite.status != "active":
                raise EvaluationQueueError("suite_source_stale")
            if not _requester_authorized(session, job.department_id, job.requested_by_user_id, now):
                raise EvaluationQueueError("requester_unauthorized")
            return suite
    except EvaluationQueueError:
        raise
    except SQLAlchemyError as error:
        raise EvaluationQueueError("database_unavailable") from error


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
            Membership.role.in_(EVALUATOR_ROLE_NAMES),
            or_(Membership.expires_at.is_(None), Membership.expires_at > now),
            UserIdentity.status == "active",
        )
    )
    if lock:
        statement = statement.with_for_update(of=(Membership, UserIdentity))
    row = session.execute(statement).scalar_one_or_none()
    return row is not None


def _owned_claim(job: ClaimedEvaluationRun):
    return (
        EvaluationRun.id == job.id,
        EvaluationRun.department_id == job.department_id,
        EvaluationRun.suite_id == job.suite_id,
        EvaluationRun.requested_by_user_id == job.requested_by_user_id,
        EvaluationRun.status == "running",
        EvaluationRun.worker_id == job.worker_id,
        EvaluationRun.claim_token == job.claim_token,
        EvaluationRun.publication_attempt_id == job.publication_attempt_id,
    )


def _live_lease():
    return EvaluationRun.lease_expires_at > func.clock_timestamp()


def _fixed_contract(job: ClaimedEvaluationRun):
    contract = production_contract()
    return (
        EvaluationRun.runner_contract_version == RUNNER_CONTRACT_VERSION,
        EvaluationRun.code_revision == job.code_revision,
        EvaluationRun.case_count == job.case_count,
        EvaluationRun.query_embedding_pipeline_version
        == contract["query_embedding_pipeline_version"],
        EvaluationRun.query_embedding_model_id == contract["query_embedding_model_id"],
        EvaluationRun.query_embedding_model_revision == contract["query_embedding_model_revision"],
        EvaluationRun.query_embedding_dimension == contract["query_embedding_dimension"],
        EvaluationRun.query_embedding_distance == contract["query_embedding_distance"],
        EvaluationRun.generation_model_id == contract["generation_model_id"],
        EvaluationRun.generation_model_revision == contract["generation_model_revision"],
        EvaluationRun.prompt_version == contract["prompt_version"],
        EvaluationRun.answer_contract_version == contract["answer_contract_version"],
        EvaluationRun.qdrant_collection == contract["qdrant_collection"],
        EvaluationRun.vector_schema_version == contract["vector_schema_version"],
    )


def _expected_result_manifest(job: ClaimedEvaluationRun) -> dict[str, object]:
    if job.publication_attempt_id is None:
        raise EvaluationQueueError("result_publication_failed")
    return {
        "run_id": str(job.id),
        "suite_id": str(job.suite_id),
        "department_id": str(job.department_id),
        "publication_attempt_id": str(job.publication_attempt_id),
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "runner_contract_version": RUNNER_CONTRACT_VERSION,
        "answer_normalization_version": ANSWER_NORMALIZATION_VERSION,
        "gate_policy_version": GATE_POLICY_VERSION,
        "seed_derivation_version": SEED_DERIVATION_VERSION,
        "base_seed": job.base_seed,
        "code_revision": job.code_revision,
        "case_count": job.case_count,
        **dict(production_contract()),
    }


def _terminal_failure(row: EvaluationRun, now, code: str) -> None:
    row.status = "failed"
    row.gate_status = "pending"
    row.worker_id = None
    row.claim_token = None
    row.lease_expires_at = None
    row.finished_at = now
    row.error_code = code
    row.version += 1
