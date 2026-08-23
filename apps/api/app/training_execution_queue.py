"""Dedicated Phase 14.1 leased worker queue.

The queue supervises an injected closed runtime.  The production default never
executes training and fails closed; tests may inject a deterministic fake.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    Department,
    Membership,
    TrainingExecution,
    TrainingExecutionAttempt,
    TrainingJob,
    UserIdentity,
)
from app.services import ServiceError, append_mutation_audit
from app.training_execution_domain import (
    EXECUTION_ERROR_CODES,
    EXECUTION_PROFILES,
    TrainingExecutionError,
    execution_authority_fingerprint,
    training_job_authority_snapshot,
    validate_runtime_result,
)
from app.training_execution_runtime import (
    TrainingExecutionRuntime,
    TrainingRuntimeHandles,
    TrainingRuntimeRequest,
    validate_runtime_request,
)
from app.training_execution_storage import (
    TrainingExecutionArtifactStore,
    TrainingExecutionStorageError,
)
from app.training_job_services import _require_no_active_purge_reservation


class TrainingExecutionQueueError(RuntimeError):
    def __init__(self, code: str = "database_unavailable") -> None:
        self.code = code if code in EXECUTION_ERROR_CODES else "database_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ClaimedTrainingExecution:
    execution_id: UUID
    department_id: UUID
    training_job_id: UUID
    requested_by_user_id: UUID
    attempt_id: UUID
    attempt_number: int
    worker_id: UUID
    claim_token: UUID
    authority_fingerprint: str
    profile_id: str
    base_model_id: str
    base_model_revision: str
    llamafactory_version: str


def _server_now(session: Session) -> datetime:
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:
        raise TrainingExecutionQueueError("database_unavailable")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _valid_claim(
    session: Session, claim: ClaimedTrainingExecution, *, lock: bool = False
) -> tuple[TrainingExecution, TrainingExecutionAttempt, TrainingJob] | None:
    # All Phase 11/14.1 mutation paths use the same job-first order.  The
    # immutable claim already carries training_job_id, so never lock the
    # execution merely to discover its parent job.
    job_query = select(TrainingJob).where(
        TrainingJob.id == claim.training_job_id,
        TrainingJob.department_id == claim.department_id,
    )
    if lock:
        job_query = job_query.with_for_update()
    job = session.execute(job_query).scalar_one_or_none()
    if job is None:
        return None
    execution_query = select(TrainingExecution).where(
        TrainingExecution.id == claim.execution_id,
        TrainingExecution.department_id == claim.department_id,
        TrainingExecution.training_job_id == claim.training_job_id,
        TrainingExecution.current_attempt_id == claim.attempt_id,
        TrainingExecution.worker_id == claim.worker_id,
        TrainingExecution.claim_token == claim.claim_token,
        TrainingExecution.status.in_(("running", "cancel_requested")),
    )
    if lock:
        execution_query = execution_query.with_for_update()
    execution = session.execute(execution_query).scalar_one_or_none()
    if execution is None:
        return None
    attempt_query = select(TrainingExecutionAttempt).where(
        TrainingExecutionAttempt.id == claim.attempt_id,
        TrainingExecutionAttempt.execution_id == claim.execution_id,
        TrainingExecutionAttempt.department_id == claim.department_id,
        TrainingExecutionAttempt.attempt_number == claim.attempt_number,
        TrainingExecutionAttempt.worker_id == claim.worker_id,
        TrainingExecutionAttempt.claim_token == claim.claim_token,
        TrainingExecutionAttempt.status == "running",
    )
    if lock:
        attempt_query = attempt_query.with_for_update()
    attempt = session.execute(attempt_query).scalar_one_or_none()
    if attempt is None or job is None:
        return None
    now = _server_now(session)
    if execution.lease_expires_at is None or execution.lease_expires_at <= now:
        return None
    if (
        job.status != "succeeded"
        or job.review_status != "approved"
        or job.purged_at is not None
        or execution.authority_fingerprint
        != execution_authority_fingerprint(
            execution_id=execution.id,
            execution_code_revision=execution.execution_code_revision,
            snapshot=training_job_authority_snapshot(job),
        )
    ):
        return None
    return execution, attempt, job


def check_execution_claim(factory: sessionmaker[Session], claim: ClaimedTrainingExecution) -> bool:
    try:
        with factory() as session:
            return _valid_claim(session, claim) is not None
    except SQLAlchemyError:
        return False


def execution_should_stop(factory: sessionmaker[Session], claim: ClaimedTrainingExecution) -> bool:
    """Return true for cancellation, claim loss, or database failure."""

    try:
        with factory() as session:
            owned = _valid_claim(session, claim)
            return owned is None or owned[0].status == "cancel_requested"
    except SQLAlchemyError:
        return True


def renew_execution_lease(
    factory: sessionmaker[Session], claim: ClaimedTrainingExecution, lease_seconds: int
) -> bool:
    try:
        with factory.begin() as session:
            owned = _valid_claim(session, claim, lock=True)
            if owned is None:
                return False
            execution, attempt, _job = owned
            now = _server_now(session)
            expires = now + timedelta(seconds=max(1, lease_seconds))
            execution.lease_expires_at = expires
            execution.version += 1
            attempt.lease_expires_at = expires
            attempt.version += 1
            return True
    except SQLAlchemyError:
        return False


def claim_next_training_execution(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int, code_revision: str
) -> ClaimedTrainingExecution | None:
    if re.fullmatch(r"[0-9a-f]{40}", code_revision) is None:
        raise TrainingExecutionQueueError("training_job_authority_changed")
    try:
        with factory.begin() as session:
            candidate = session.scalar(
                select(TrainingExecution.id)
                .where(
                    TrainingExecution.execution_code_revision == code_revision,
                    (TrainingExecution.status == "queued")
                    | (
                        TrainingExecution.status.in_(("running", "cancel_requested"))
                        & (TrainingExecution.lease_expires_at <= func.clock_timestamp())
                    ),
                )
                .order_by(TrainingExecution.created_at, TrainingExecution.id)
                .limit(1)
            )
            if candidate is None:
                return None
            execution_probe = session.scalar(
                select(TrainingExecution).where(TrainingExecution.id == candidate)
            )
            if execution_probe is None:
                return None
            # Job is deliberately locked before the execution row.
            job = session.execute(
                select(TrainingJob)
                .where(
                    TrainingJob.id == execution_probe.training_job_id,
                    TrainingJob.department_id == execution_probe.department_id,
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if job is None:
                return None
            execution = session.execute(
                select(TrainingExecution)
                .where(
                    TrainingExecution.id == candidate,
                    TrainingExecution.training_job_id == job.id,
                    TrainingExecution.department_id == job.department_id,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ).scalar_one_or_none()
            if execution is None:
                return None
            department = session.scalar(
                select(Department).where(
                    Department.id == job.department_id, Department.status == "active"
                )
            )
            requester = session.scalar(
                select(Membership).where(
                    Membership.user_id == execution.requested_by_user_id,
                    Membership.department_id == job.department_id,
                    Membership.status == "active",
                    Membership.role.in_(("system_admin", "department_admin")),
                )
            )
            if department is None or requester is None:
                execution.status = "failed"
                execution.error_code = (
                    "department_unavailable" if department is None else "requester_unauthorized"
                )
                execution.finished_at = _server_now(session)
                execution.version += 1
                return None
            try:
                _require_no_active_purge_reservation(session, job)
            except ServiceError:
                # A committed Phase 11 purge fence wins this claim race; leave
                # the queued execution untouched for an explicit operator
                # decision rather than creating a runtime attempt.
                return None
            snapshot = training_job_authority_snapshot(job)
            expected_fp = execution_authority_fingerprint(
                execution_id=execution.id,
                execution_code_revision=execution.execution_code_revision,
                snapshot=snapshot,
            )
            if (
                execution.authority_fingerprint != expected_fp
                or job.status != "succeeded"
                or job.review_status != "approved"
            ):
                execution.status = "failed"
                execution.error_code = "training_job_authority_changed"
                execution.finished_at = _server_now(session)
                execution.version += 1
                return None
            if (
                job.profile_id not in EXECUTION_PROFILES
                or job.code_revision != execution.training_job_code_revision
                or code_revision != execution.execution_code_revision
            ):
                execution.status = "failed"
                execution.error_code = "training_job_authority_changed"
                execution.finished_at = _server_now(session)
                execution.version += 1
                return None
            now = _server_now(session)
            if execution.status in ("running", "cancel_requested") and execution.current_attempt_id:
                previous = session.execute(
                    select(TrainingExecutionAttempt)
                    .where(
                        TrainingExecutionAttempt.id == execution.current_attempt_id,
                        TrainingExecutionAttempt.execution_id == execution.id,
                        TrainingExecutionAttempt.department_id == execution.department_id,
                        TrainingExecutionAttempt.status == "running",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if previous is not None:
                    previous.status = "reclaimed"
                    previous.finished_at = now
                    previous.result_classification = "execution_failed"
                    previous.error_code = "claim_lost"
                    previous.version += 1
            if execution.status == "cancel_requested":
                execution.status = "cancelled"
                execution.error_code = "cancelled"
                execution.finished_at = now
                execution.worker_id = execution.claim_token = execution.lease_expires_at = None
                execution.current_attempt_id = None
                execution.version += 1
                return None
            attempt_id, claim_token = uuid4(), uuid4()
            attempt_number = execution.current_attempt_number + (
                1 if execution.current_attempt_id else 0
            )
            # Register the attempt while the parent remains queued.  The
            # second flush then updates both rows with an existing FK target.
            attempt = TrainingExecutionAttempt(
                id=attempt_id,
                execution_id=execution.id,
                department_id=execution.department_id,
                attempt_number=attempt_number,
                status="registered",
                version=1,
            )
            session.add(attempt)
            session.flush()
            execution.current_attempt_number = attempt_number
            expires = now + timedelta(seconds=max(1, lease_seconds))
            execution.status = "running"
            execution.current_attempt_id = attempt_id
            execution.worker_id = worker_id
            execution.claim_token = claim_token
            execution.lease_expires_at = expires
            execution.started_at = execution.started_at or now
            execution.finished_at = None
            execution.error_code = None
            execution.version += 1
            attempt.worker_id = worker_id
            attempt.claim_token = claim_token
            attempt.claimed_at = now
            attempt.lease_expires_at = expires
            attempt.started_at = now
            attempt.status = "running"
            return ClaimedTrainingExecution(
                execution_id=execution.id,
                department_id=execution.department_id,
                training_job_id=job.id,
                requested_by_user_id=execution.requested_by_user_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                worker_id=worker_id,
                claim_token=claim_token,
                authority_fingerprint=execution.authority_fingerprint,
                profile_id=execution.profile_id,
                base_model_id=execution.base_model_id,
                base_model_revision=execution.base_model_revision,
                llamafactory_version=execution.llamafactory_version,
            )
    except TrainingExecutionQueueError:
        raise
    except SQLAlchemyError as error:
        raise TrainingExecutionQueueError("database_unavailable") from error


def _finalize(
    factory: sessionmaker[Session],
    claim: ClaimedTrainingExecution,
    *,
    status: str,
    error_code: str | None,
    classification: str,
    runtime_fp: str | None,
    input_fp: str | None,
) -> bool:
    try:
        with factory.begin() as session:
            owned = _valid_claim(session, claim, lock=True)
            if owned is None:
                return False
            execution, attempt, job = owned
            now = _server_now(session)
            if execution.status == "cancel_requested" and status == "succeeded":
                status = "cancelled"
                error_code = "cancelled"
                classification = "execution_cancelled"
            attempt.status = (
                "succeeded"
                if status == "succeeded"
                else "cancelled"
                if status == "cancelled"
                else "failed"
            )
            attempt.finished_at = now
            attempt.runtime_fingerprint = runtime_fp
            attempt.input_snapshot_fingerprint = input_fp
            attempt.result_classification = classification
            attempt.error_code = error_code
            attempt.version += 1
            execution.status = status
            execution.error_code = error_code
            execution.finished_at = now
            execution.current_attempt_id = None
            execution.worker_id = None
            execution.claim_token = None
            execution.lease_expires_at = None
            execution.version += 1
            if status == "succeeded":
                actor = session.scalar(
                    select(UserIdentity).where(UserIdentity.id == job.requested_by_user_id)
                )
                if actor is not None:
                    append_mutation_audit(
                        session,
                        actor=actor,
                        actor_subject=actor.subject,
                        request_scope=DepartmentRequestScope(DepartmentScope(job.department_id)),
                        action="training.execution.complete",
                        resource_type="training_execution",
                        resource_id=execution.id,
                    )
            return True
    except SQLAlchemyError:
        return False


def process_training_execution(
    factory: sessionmaker[Session],
    data_dir: Path,
    claim: ClaimedTrainingExecution,
    *,
    runtime: TrainingExecutionRuntime | None = None,
    lease_seconds: int = 300,
    operation_seconds: int = 3600,
    should_stop: Callable[[], bool] = lambda: False,
) -> bool:
    deadline = time.monotonic() + max(1, operation_seconds)
    if runtime is None:
        return _finalize(
            factory,
            claim,
            status="failed",
            error_code="runtime_unavailable",
            classification="execution_failed",
            runtime_fp=None,
            input_fp=None,
        )

    def checkpoint() -> None:
        if should_stop():
            raise TrainingExecutionQueueError("worker_shutdown")
        if time.monotonic() >= deadline:
            raise TrainingExecutionQueueError("worker_timeout")
        if not check_execution_claim(factory, claim):
            raise TrainingExecutionQueueError("claim_lost")

    stage = None
    snapshot = None
    try:
        with factory() as session:
            job = session.scalar(
                select(TrainingJob).where(
                    TrainingJob.id == claim.training_job_id,
                    TrainingJob.department_id == claim.department_id,
                )
            )
            if job is None:
                raise TrainingExecutionQueueError("training_job_unavailable")
        checkpoint()
        with TrainingExecutionArtifactStore(data_dir) as store:
            stage = store.create_attempt(claim.department_id, claim.execution_id, claim.attempt_id)
            snapshot = store.snapshot_phase11_final(
                stage,
                scope=DepartmentScope(claim.department_id),
                job=job,
            )
            checkpoint()
            with factory.begin() as session:
                owned = _valid_claim(session, claim, lock=True)
                if owned is None:
                    raise TrainingExecutionQueueError("claim_lost")
                attempt = owned[1]
                attempt.input_snapshot_fingerprint = snapshot.fingerprint
                attempt.version += 1
            request = TrainingRuntimeRequest(
                contract_version="phase14-training-execution-v1",
                department_id=claim.department_id,
                execution_id=claim.execution_id,
                attempt_id=claim.attempt_id,
                training_job_id=claim.training_job_id,
                publication_attempt_id=job.publication_attempt_id,
                authority_fingerprint=claim.authority_fingerprint,
                input_snapshot_fingerprint=snapshot.fingerprint,
                profile_id=claim.profile_id,
                base_model_id=claim.base_model_id,
                base_model_revision=claim.base_model_revision,
                attempt_namespace=claim.attempt_id,
            )
            validate_runtime_request(request)
            result = runtime.run(
                request,
                handles=TrainingRuntimeHandles(
                    input_fd=stage.input_fd,
                    scratch_fd=stage.scratch_fd,
                    logs_fd=stage.logs_fd,
                    output_stage_fd=stage.output_stage_fd,
                ),
                should_stop=lambda: should_stop() or execution_should_stop(factory, claim),
                heartbeat=lambda: renew_execution_lease(factory, claim, lease_seconds),
            )
            classification, error_code, runtime_fp = validate_runtime_result(
                department_id=claim.department_id,
                execution_id=claim.execution_id,
                attempt_id=claim.attempt_id,
                training_job_id=claim.training_job_id,
                authority_fingerprint_value=claim.authority_fingerprint,
                input_snapshot_fingerprint=snapshot.fingerprint,
                result=result.as_closed_mapping()
                if hasattr(result, "as_closed_mapping")
                else result,
            )
            checkpoint()
            status = (
                "succeeded"
                if classification == "execution_succeeded"
                else "cancelled"
                if classification == "execution_cancelled"
                else "failed"
            )
            code = error_code or ("runtime_protocol_invalid" if status == "failed" else None)
            return _finalize(
                factory,
                claim,
                status=status,
                error_code=code,
                classification=classification,
                runtime_fp=runtime_fp,
                input_fp=snapshot.fingerprint,
            )
    except TrainingExecutionQueueError as error:
        _finalize(
            factory,
            claim,
            status="cancelled" if error.code in {"worker_shutdown", "cancelled"} else "failed",
            error_code=error.code,
            classification="execution_cancelled"
            if error.code in {"worker_shutdown", "cancelled"}
            else "execution_failed",
            runtime_fp=None,
            input_fp=None,
        )
        return False
    except (TrainingExecutionError, TrainingExecutionStorageError) as error:
        code = getattr(error, "code", "input_snapshot_failed")
        _finalize(
            factory,
            claim,
            status="failed",
            error_code=code,
            classification="execution_failed",
            runtime_fp=None,
            input_fp=None,
        )
        return False
    finally:
        if stage is not None:
            stage.close()


__all__ = [
    "ClaimedTrainingExecution",
    "TrainingExecutionQueueError",
    "check_execution_claim",
    "claim_next_training_execution",
    "execution_should_stop",
    "process_training_execution",
    "renew_execution_lease",
]
