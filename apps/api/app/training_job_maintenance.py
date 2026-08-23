"""Bounded, explicit Phase 11 archive, purge, and reconciliation operations."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    AdapterUpstreamDependency,
    TrainingExecution,
    TrainingJob,
    TrainingJobArtifactOperation,
    TrainingJobArtifactOperationItem,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.sft_artifacts import SftArtifactError, SftArtifactStore
from app.training_execution_fences import has_active_training_execution
from app.training_job_domain import (
    TrainingJobContractError,
    canonical_json_bytes,
    parse_job_manifest,
)
from app.training_job_services import TRAINING_MUTATION_ROLES


class TrainingJobMaintenanceConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrainingJobMaintenanceSettings:
    database_url: str
    data_dir: Path
    retention_days: int

    @classmethod
    def from_environment(cls) -> TrainingJobMaintenanceSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        raw_retention = os.getenv("DEPTSLM_TRAINING_JOB_RETENTION_DAYS", "180").strip()
        if not database_url.startswith("postgresql+psycopg://") or not raw_data_dir:
            raise TrainingJobMaintenanceConfigurationError(
                "Training-job maintenance configuration is invalid."
            )
        if (
            not raw_retention.isascii()
            or not raw_retention.isdecimal()
            or not 30 <= int(raw_retention) <= 730
        ):
            raise TrainingJobMaintenanceConfigurationError(
                "DEPTSLM_TRAINING_JOB_RETENTION_DAYS must be between 30 and 730."
            )
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise TrainingJobMaintenanceConfigurationError("Training-job storage is unavailable.")
        return cls(database_url, data_dir, int(raw_retention))


@dataclass(frozen=True, slots=True)
class TrainingJobMaintenanceResult:
    eligible_count: int
    applied_count: int
    blocked_count: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    training_job_id: UUID
    attempt_id: UUID
    surface: str
    manifest: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class _AuthoritativeFinal:
    attempt: TrainingJobAttempt
    manifest: dict[str, object]


@dataclass(frozen=True, slots=True)
class _BoundTombstoneStep:
    candidate: _Candidate
    identity: dict[str, object]
    name: str | None


_BLOCKED_REASONS = frozenset(
    {
        "staging_path_unsafe",
        "artifact_ownership_mismatch",
        "artifact_manifest_invalid",
        "artifact_permissions_invalid",
    }
)


def archive_training_job(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    training_job_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    apply: bool,
) -> bool:
    try:
        with factory.begin() as session:
            job = session.execute(
                select(TrainingJob)
                .where(
                    TrainingJob.id == training_job_id, TrainingJob.department_id == department_id
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise ServiceError(404, "Training job not found")
            scope, authorization = _authorize(session, department_id, actor_issuer, actor_subject)
            active_reservation = session.execute(
                select(TrainingJobPurgeReservation)
                .where(
                    TrainingJobPurgeReservation.department_id == department_id,
                    TrainingJobPurgeReservation.training_job_id == training_job_id,
                    TrainingJobPurgeReservation.status.in_(
                        ("registered", "deletion_authorized", "tombstone_bound")
                    ),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if active_reservation is not None:
                raise ServiceError(409, "Training job purge is in progress")
            if has_active_training_execution(session, department_id, training_job_id, lock=True):
                raise ServiceError(409, "Training job has an active execution")
            if job.status != "succeeded" or job.review_status not in {"approved", "rejected"}:
                raise ServiceError(409, "Training job cannot be archived")
            if not apply:
                return False
            job.review_status = "archived"
            job.archived_at = session.scalar(select(func.clock_timestamp()))
            job.version += 1
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=actor_subject,
                request_scope=scope,
                action="training.job.review.archive",
                resource_type="training_job",
                resource_id=job.id,
            )
            return True
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def reconcile_training_job_artifacts(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int,
    apply: bool,
) -> TrainingJobMaintenanceResult:
    _limit(limit)
    operation_id, candidates = _register_candidates(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_type="reconcile",
        retention_days=None,
        limit=limit,
        apply=apply,
    )
    return _execute(
        factory,
        data_dir,
        department_id,
        actor_issuer,
        actor_subject,
        operation_id,
        candidates,
        apply,
    )


def purge_training_job_artifacts(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    retention_days: int,
    limit: int,
    apply: bool,
) -> TrainingJobMaintenanceResult:
    _limit(limit)
    if not 30 <= retention_days <= 730:
        raise ServiceError(422, "Invalid training-job retention")
    operation_id, candidates = _register_candidates(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_type="purge",
        retention_days=retention_days,
        limit=limit,
        apply=apply,
    )
    return _execute(
        factory,
        data_dir,
        department_id,
        actor_issuer,
        actor_subject,
        operation_id,
        candidates,
        apply,
    )


def _register_candidates(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_type: str,
    retention_days: int | None,
    limit: int,
    apply: bool,
) -> tuple[UUID | None, tuple[_Candidate, ...]]:
    try:
        with factory.begin() as session:
            _scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=False
            )
            jobs: list[TrainingJob] = []
            existing = session.execute(
                select(TrainingJobArtifactOperation)
                .where(
                    TrainingJobArtifactOperation.department_id == department_id,
                    TrainingJobArtifactOperation.operation_type == operation_type,
                    TrainingJobArtifactOperation.status == "registered",
                )
                .order_by(TrainingJobArtifactOperation.created_at, TrainingJobArtifactOperation.id)
                .with_for_update()
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                items = session.scalars(
                    select(TrainingJobArtifactOperationItem)
                    .where(
                        TrainingJobArtifactOperationItem.operation_id == existing.id,
                        TrainingJobArtifactOperationItem.department_id == department_id,
                        TrainingJobArtifactOperationItem.status == "registered",
                    )
                    .order_by(
                        TrainingJobArtifactOperationItem.created_at,
                        TrainingJobArtifactOperationItem.id,
                    )
                ).all()
                if items:
                    return existing.id, tuple(_candidate_from_item(item) for item in items)
                _close_empty_registered_operation(session, existing, department_id)
                # Flush terminalized legacy reservations before a fresh
                # reservation can be inserted under the active-job index.
                session.flush()
            if operation_type == "reconcile":
                query = select(TrainingJobAttempt).where(
                    TrainingJobAttempt.department_id == department_id,
                    TrainingJobAttempt.status.in_(("reclaimed", "failed", "cancelled")),
                    TrainingJobAttempt.cleanup_confirmed_at.is_(None),
                )
                attempts = session.scalars(
                    query.order_by(TrainingJobAttempt.created_at, TrainingJobAttempt.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                ).all()
                jobs_by_id = {
                    attempt.training_job_id: session.execute(
                        select(TrainingJob)
                        .where(
                            TrainingJob.id == attempt.training_job_id,
                            TrainingJob.department_id == department_id,
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    for attempt in attempts
                }
                candidates = tuple(_reconcile_candidates(attempts, jobs_by_id))
            else:
                assert retention_days is not None
                cutoff = session.scalar(select(func.clock_timestamp())) - timedelta(
                    days=retention_days
                )
                retained_at = case(
                    (TrainingJob.review_status == "rejected", TrainingJob.reviewed_at),
                    (TrainingJob.review_status == "archived", TrainingJob.archived_at),
                    else_=None,
                )
                jobs = session.scalars(
                    select(TrainingJob)
                    .where(
                        TrainingJob.department_id == department_id,
                        TrainingJob.status == "succeeded",
                        TrainingJob.review_status.in_(("rejected", "archived")),
                        TrainingJob.purged_at.is_(None),
                        retained_at <= cutoff,
                        ~select(AdapterUpstreamDependency.id)
                        .where(
                            AdapterUpstreamDependency.department_id == department_id,
                            AdapterUpstreamDependency.training_job_id == TrainingJob.id,
                            AdapterUpstreamDependency.status == "active",
                        )
                        .exists(),
                        ~select(TrainingExecution.id)
                        .where(
                            TrainingExecution.department_id == department_id,
                            TrainingExecution.training_job_id == TrainingJob.id,
                            TrainingExecution.status.in_(("queued", "running", "cancel_requested")),
                        )
                        .exists(),
                    )
                    # Lock target jobs in the same deterministic order used by
                    # execution enqueue/archive and purge continuation paths.
                    .order_by(TrainingJob.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                ).all()
                if operation_type == "purge":
                    jobs = [
                        job
                        for job in jobs
                        if not has_active_training_execution(
                            session, department_id, job.id, lock=True
                        )
                    ]
                attempts = []
                for job in jobs:
                    attempts.extend(
                        session.scalars(
                            select(TrainingJobAttempt)
                            .where(
                                TrainingJobAttempt.department_id == department_id,
                                TrainingJobAttempt.training_job_id == job.id,
                            )
                            .order_by(TrainingJobAttempt.attempt_number, TrainingJobAttempt.id)
                            .with_for_update()
                        ).all()
                    )
                candidates = tuple(
                    candidate
                    for job in jobs
                    for candidate in _purge_candidates(
                        job,
                        [attempt for attempt in attempts if attempt.training_job_id == job.id],
                    )
                )
            if not apply:
                return None, candidates
            if not candidates:
                # An empty apply is a successful no-op. It must not create a
                # resumable operation or an active reservation with no work.
                return None, ()
            # All selected jobs are locked before the department lock is
            # reacquired, preserving job -> department order with execution
            # enqueue/cancel/retry.
            _scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=True
            )
            operation = TrainingJobArtifactOperation(
                id=uuid4(),
                department_id=department_id,
                requested_by_user_id=authorization.identity.id,
                limit_value=limit,
                retention_days=retention_days,
                operation_type=operation_type,
                status="registered",
                purged_job_count=0,
                version=1,
            )
            session.add(operation)
            session.flush()
            if operation_type == "purge":
                assert retention_days is not None
                for job in jobs:
                    anchor = _retention_anchor(job)
                    if anchor is None:
                        raise ServiceError(409, "Training job purge is unavailable")
                    final_candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.training_job_id == job.id and candidate.surface == "final"
                    ]
                    if len(final_candidates) != 1 or final_candidates[0].manifest is None:
                        raise ServiceError(409, "Training job purge authority changed")
                    final_candidate = final_candidates[0]
                    session.add(
                        TrainingJobPurgeReservation(
                            id=uuid4(),
                            operation_id=operation.id,
                            department_id=department_id,
                            training_job_id=job.id,
                            expected_job_version=job.version,
                            expected_review_status=job.review_status,
                            retention_anchor_at=anchor,
                            retention_days=retention_days,
                            authoritative_publication_attempt_id=final_candidate.attempt_id,
                            authoritative_manifest=dict(final_candidate.manifest),
                            tombstone_operation_id=operation.id,
                            status="registered",
                            version=1,
                        )
                    )
            for candidate in candidates:
                session.add(
                    TrainingJobArtifactOperationItem(
                        id=uuid4(),
                        operation_id=operation.id,
                        department_id=department_id,
                        training_job_id=candidate.training_job_id,
                        publication_attempt_id=candidate.attempt_id,
                        resource_surface=candidate.surface,
                        ownership_manifest=candidate.manifest,
                        status="registered",
                    )
                )
            return operation.id, candidates
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _candidate_from_item(item: TrainingJobArtifactOperationItem) -> _Candidate:
    return _Candidate(
        item.training_job_id,
        item.publication_attempt_id,
        item.resource_surface,
        dict(item.ownership_manifest) if isinstance(item.ownership_manifest, dict) else None,
    )


def _reconcile_candidates(
    attempts: list[TrainingJobAttempt], jobs_by_id: dict[UUID, TrainingJob | None]
) -> list[_Candidate]:
    """Reconcile stale attempts without treating historical manifests as final owners."""

    result: list[_Candidate] = []
    for attempt in attempts:
        result.append(
            _Candidate(attempt.training_job_id, attempt.publication_attempt_id, "stage", None)
        )
        job = jobs_by_id.get(attempt.training_job_id)
        if (
            job is not None
            and job.status != "succeeded"
            and isinstance(attempt.ownership_manifest, dict)
        ):
            result.append(
                _Candidate(
                    attempt.training_job_id,
                    attempt.publication_attempt_id,
                    "final",
                    dict(attempt.ownership_manifest),
                )
            )
    return result


def _purge_candidates(job: TrainingJob, attempts: list[TrainingJobAttempt]) -> list[_Candidate]:
    """Register attempt stages and exactly one authoritative job-level final."""

    owner = _authoritative_final(job, attempts)
    if not attempts or any(not _terminal_attempt(attempt) for attempt in attempts):
        raise ServiceError(409, "Training-job purge authority changed")
    result = [
        _Candidate(attempt.training_job_id, attempt.publication_attempt_id, "stage", None)
        for attempt in attempts
        if attempt.cleanup_confirmed_at is None
    ]
    result.append(
        _Candidate(
            job.id,
            owner.attempt.publication_attempt_id,
            "final",
            dict(owner.manifest),
        )
    )
    return result


def _terminal_attempt(attempt: TrainingJobAttempt) -> bool:
    return attempt.status in {"succeeded", "failed", "cancelled", "reclaimed"}


def _authoritative_final(
    job: TrainingJob, attempts: list[TrainingJobAttempt]
) -> _AuthoritativeFinal:
    """Validate the one and only owner of the physical job-level final path."""

    if (
        job.status != "succeeded"
        or job.publication_attempt_id is None
        or not isinstance(job.publication_manifest, dict)
    ):
        raise ServiceError(409, "Training-job purge authority changed")
    matching = [
        attempt
        for attempt in attempts
        if attempt.status == "succeeded"
        and attempt.publication_attempt_id == job.publication_attempt_id
    ]
    if len(matching) != 1:
        raise ServiceError(409, "Training-job purge authority changed")
    attempt = matching[0]
    manifest = dict(job.publication_manifest)
    if attempt.ownership_manifest != manifest or not _manifest_matches_job(job, attempt, manifest):
        raise ServiceError(409, "Training-job purge authority changed")
    return _AuthoritativeFinal(attempt, manifest)


def _manifest_matches_job(
    job: TrainingJob, attempt: TrainingJobAttempt, manifest: dict[str, object]
) -> bool:
    """Compare every content-free owner field before final cleanup or purging."""

    try:
        parse_job_manifest(canonical_json_bytes(manifest) + b"\n")
    except TrainingJobContractError:
        return False
    if (
        job.publication_attempt_id != attempt.publication_attempt_id
        or job.execution_scope_id is None
        or job.result_manifest_sha256 is None
        or any(
            value is None or value <= 0
            for value in (
                job.training_config_byte_size,
                job.dataset_info_byte_size,
                job.train_byte_size,
                job.validation_byte_size,
                job.train_example_count,
                job.validation_example_count,
            )
        )
        or hashlib.sha256(canonical_json_bytes(manifest) + b"\n").hexdigest()
        != job.result_manifest_sha256
    ):
        return False
    if (
        manifest.get("department_id") != str(job.department_id)
        or manifest.get("training_job_id") != str(job.id)
        or manifest.get("publication_attempt_id") != str(attempt.publication_attempt_id)
        or manifest.get("execution_scope_id") != str(job.execution_scope_id)
        or manifest.get("attempt_number") != attempt.attempt_number
        or manifest.get("code_revision") != job.code_revision
        or attempt.code_revision != job.code_revision
        or manifest.get("dataset_build_id") != str(job.dataset_build_id)
        or manifest.get("dataset_build_version") != job.dataset_build_version
        or manifest.get("dataset_manifest_sha256") != job.dataset_manifest_sha256
        or manifest.get("profile_id") != job.profile_id
        or manifest.get("train_example_count") != job.train_example_count
        or manifest.get("validation_example_count") != job.validation_example_count
    ):
        return False
    return manifest.get("files") == {
        "training.yaml": {
            "sha256": job.training_config_sha256,
            "byte_size": job.training_config_byte_size,
        },
        "dataset_info.json": {
            "sha256": job.dataset_info_sha256,
            "byte_size": job.dataset_info_byte_size,
        },
        "train.jsonl": {"sha256": job.train_sha256, "byte_size": job.train_byte_size},
        "validation.jsonl": {
            "sha256": job.validation_sha256,
            "byte_size": job.validation_byte_size,
        },
    }


def _close_empty_registered_operation(
    session: Session, operation: TrainingJobArtifactOperation, department_id: UUID
) -> None:
    """Prevent a legacy empty operation from permanently blocking maintenance."""

    reservations = session.scalars(
        select(TrainingJobPurgeReservation)
        .where(
            TrainingJobPurgeReservation.operation_id == operation.id,
            TrainingJobPurgeReservation.department_id == department_id,
            TrainingJobPurgeReservation.status.in_(
                ("registered", "deletion_authorized", "tombstone_bound")
            ),
        )
        .with_for_update()
    ).all()
    if any(
        reservation.status in {"deletion_authorized", "tombstone_bound"}
        for reservation in reservations
    ):
        raise ServiceError(409, "Training-job purge authority changed")
    now = session.scalar(select(func.clock_timestamp()))
    for reservation in reservations:
        reservation.status = "terminalized"
        reservation.terminalized_at = now
        reservation.version += 1
    operation.status = "completed"
    operation.completed_at = now


def _execute(
    factory: sessionmaker[Session],
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID | None,
    candidates: tuple[_Candidate, ...],
    apply: bool,
) -> TrainingJobMaintenanceResult:
    if not apply:
        return TrainingJobMaintenanceResult(len(candidates), 0, 0)
    if operation_id is None:
        return TrainingJobMaintenanceResult(0, 0, 0)
    operation_type = _operation_type(factory, operation_id, department_id)
    if operation_type == "reconcile":
        outcomes = _remove_candidates(data_dir, department_id, candidates)
        _persist_reconcile_outcomes(
            factory,
            department_id=department_id,
            actor_issuer=actor_issuer,
            actor_subject=actor_subject,
            operation_id=operation_id,
            outcomes=outcomes,
        )
        return TrainingJobMaintenanceResult(
            len(candidates),
            sum(completed for completed, _reason in outcomes.values()),
            sum(not completed for completed, _reason in outcomes.values()),
        )
    return _execute_purge(
        factory,
        data_dir=data_dir,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
        candidates=candidates,
    )


def _operation_type(factory: sessionmaker[Session], operation_id: UUID, department_id: UUID) -> str:
    try:
        with factory() as session:
            operation = session.execute(
                select(TrainingJobArtifactOperation.operation_type).where(
                    TrainingJobArtifactOperation.id == operation_id,
                    TrainingJobArtifactOperation.department_id == department_id,
                    TrainingJobArtifactOperation.status == "registered",
                )
            ).scalar_one_or_none()
            if operation not in {"reconcile", "purge"}:
                raise ServiceError(409, "Training-job maintenance operation is unavailable")
            return operation
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _execute_purge(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
    candidates: tuple[_Candidate, ...],
) -> TrainingJobMaintenanceResult:
    _authorize_purge_prerequisites(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
    )
    stages = tuple(candidate for candidate in candidates if candidate.surface == "stage")
    stage_outcomes = _remove_candidates(data_dir, department_id, stages)
    _persist_purge_stage_outcomes(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
        outcomes=stage_outcomes,
    )
    if any(not completed for completed, _reason in stage_outcomes.values()):
        return TrainingJobMaintenanceResult(
            len(candidates),
            sum(completed for completed, _reason in stage_outcomes.values()),
            sum(not completed for completed, _reason in stage_outcomes.values()),
        )
    final_candidates = _authorize_final_deletion(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
    )
    binding_outcomes = _bind_final_tombstones(
        factory,
        data_dir,
        department_id,
        actor_issuer,
        actor_subject,
        final_candidates,
        purge_operation_id=operation_id,
    )
    _persist_tombstone_bindings(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
        outcomes=binding_outcomes,
    )
    final_outcomes = _remove_bound_tombstones(
        factory,
        data_dir=data_dir,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
    )
    _persist_purge_final_outcomes(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
        outcomes=final_outcomes,
    )
    outcomes = {**stage_outcomes, **binding_outcomes, **final_outcomes}
    return TrainingJobMaintenanceResult(
        len(candidates),
        sum(completed for completed, _reason in outcomes.values()),
        sum(not completed for completed, _reason in outcomes.values()),
    )


def _remove_candidates(
    data_dir: Path,
    department_id: UUID,
    candidates: tuple[_Candidate, ...],
) -> dict[tuple[UUID, UUID, str], tuple[bool, str | None]]:
    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, str | None]] = {}
    scope = DepartmentScope(department_id)
    for candidate in candidates:
        try:
            with SftArtifactStore(data_dir) as store:
                if candidate.surface == "stage":
                    store.remove_owned_training_job_stage(
                        scope, candidate.training_job_id, candidate.attempt_id
                    )
                elif candidate.surface == "final" and candidate.manifest is not None:
                    store.remove_owned_training_job_final(
                        scope,
                        candidate.training_job_id,
                        candidate.attempt_id,
                        expected=candidate.manifest,
                    )
                else:
                    raise SftArtifactError("artifact_ownership_mismatch")
            outcomes[_candidate_key(candidate)] = (True, None)
        except SftArtifactError as error:
            outcomes[_candidate_key(candidate)] = (False, _blocked_reason(error))
    return outcomes


def _assert_no_active_execution_before_bytes(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    training_job_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
) -> None:
    """Revalidate the exact purge/job fence immediately before storage I/O."""

    try:
        with factory.begin() as session:
            # Resolve authorization without taking a department lock first;
            # then lock the job, preserving the job -> department ordering
            # used by execution enqueue and archive mutations.
            _authorize(session, department_id, actor_issuer, actor_subject, lock=False)
            job = session.execute(
                select(TrainingJob)
                .where(
                    TrainingJob.id == training_job_id,
                    TrainingJob.department_id == department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise ServiceError(409, "Training-job purge authority changed")
            _authorize(session, department_id, actor_issuer, actor_subject, lock=True)
            reservation = session.execute(
                select(TrainingJobPurgeReservation)
                .where(
                    TrainingJobPurgeReservation.operation_id == operation_id,
                    TrainingJobPurgeReservation.department_id == department_id,
                    TrainingJobPurgeReservation.training_job_id == training_job_id,
                    TrainingJobPurgeReservation.status.in_(
                        ("deletion_authorized", "tombstone_bound")
                    ),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if reservation is None:
                raise ServiceError(409, "Training-job purge authority changed")
            _assert_purge_authority(
                session,
                department_id,
                reservation,
                session.scalar(select(func.clock_timestamp())),
            )
            if has_active_training_execution(session, department_id, training_job_id, lock=True):
                raise ServiceError(409, "Training job has an active execution")
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _bind_final_tombstones(
    factory: sessionmaker[Session],
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    candidates: tuple[_Candidate, ...],
    *,
    purge_operation_id: UUID,
) -> dict[tuple[UUID, UUID, str], tuple[bool, dict[str, object] | str | None]]:
    """Move exact finals, then capture a complete tombstone identity only."""

    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, dict[str, object] | str | None]] = {}
    scope = DepartmentScope(department_id)
    for candidate in candidates:
        if candidate.surface != "final" or candidate.manifest is None:
            outcomes[_candidate_key(candidate)] = (False, "artifact_ownership_mismatch")
            continue
        try:
            # Recheck the job fence immediately before the filesystem move.
            # The active purge reservation blocks a new execution, while the
            # job lock makes the enqueue/archive/purge ordering explicit.
            _assert_no_active_execution_before_bytes(
                factory,
                department_id=department_id,
                training_job_id=candidate.training_job_id,
                actor_issuer=actor_issuer,
                actor_subject=actor_subject,
                operation_id=purge_operation_id,
            )
            with SftArtifactStore(data_dir) as store:
                identity = store.prepare_authorized_training_job_tombstone(
                    scope,
                    candidate.training_job_id,
                    candidate.attempt_id,
                    purge_operation_id,
                    expected=candidate.manifest,
                )
            if identity is None:
                raise SftArtifactError("final_deletion_recovery_required")
            outcomes[_candidate_key(candidate)] = (True, identity)
        except SftArtifactError as error:
            outcomes[_candidate_key(candidate)] = (False, _blocked_reason(error))
    return outcomes


def _persist_tombstone_bindings(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, dict[str, object] | str | None]],
) -> None:
    """Commit an exact tombstone identity before any member can be unlinked."""

    if not outcomes:
        return
    try:
        with factory.begin() as session:
            _authorize(session, department_id, actor_issuer, actor_subject, lock=False)
            _locked_operation(session, operation_id, department_id, "purge")
            now = session.scalar(select(func.clock_timestamp()))
            _lock_purge_job_ids(
                session,
                department_id,
                {
                    training_job_id
                    for (training_job_id, _attempt_id, surface), (bound, value) in outcomes.items()
                    if surface == "final" and bound and isinstance(value, dict)
                },
            )
            _authorize(session, department_id, actor_issuer, actor_subject, lock=True)
            for key, (bound, value) in outcomes.items():
                training_job_id, attempt_id, surface = key
                if surface != "final" or not bound or not isinstance(value, dict):
                    # A failed move/bind remains actively fenced.  It must
                    # never become a blocked terminal item merely because a
                    # crash left a partial or absent tombstone.
                    continue
                reservation = session.execute(
                    select(TrainingJobPurgeReservation)
                    .where(
                        TrainingJobPurgeReservation.operation_id == operation_id,
                        TrainingJobPurgeReservation.department_id == department_id,
                        TrainingJobPurgeReservation.training_job_id == training_job_id,
                        TrainingJobPurgeReservation.status == "deletion_authorized",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if reservation is None:
                    raise ServiceError(409, "Training-job purge authority changed")
                reservation = _lock_purge_reservation(session, reservation)
                _job, _attempts, items, owner = _assert_purge_authority(
                    session, department_id, reservation, now
                )
                if owner.attempt.publication_attempt_id != attempt_id:
                    raise ServiceError(409, "Training-job purge authority changed")
                final_item = _final_item(items, owner)
                if final_item.status != "registered":
                    raise ServiceError(409, "Training-job purge authority changed")
                reservation.tombstone_identity = dict(value)
                reservation.tombstone_bound_at = now
                reservation.status = "tombstone_bound"
                reservation.version += 1
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _remove_bound_tombstones(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
) -> dict[tuple[UUID, UUID, str], tuple[bool, str | None]]:
    """Resume only persisted, identity-bound tombstones one durable unlink at a time."""

    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, str | None]] = {}
    while True:
        step = _begin_bound_tombstone_step(
            factory,
            department_id=department_id,
            actor_issuer=actor_issuer,
            actor_subject=actor_subject,
            operation_id=operation_id,
            completed=outcomes,
        )
        if step is None:
            return outcomes
        key = _candidate_key(step.candidate)
        try:
            # A durable tombstone is still protected by the same exact job
            # fence immediately before each byte unlink.  Never assume the
            # earlier authorization transaction remains current.
            _assert_no_active_execution_before_bytes(
                factory,
                department_id=department_id,
                training_job_id=step.candidate.training_job_id,
                actor_issuer=actor_issuer,
                actor_subject=actor_subject,
                operation_id=operation_id,
            )
            with SftArtifactStore(data_dir) as store:
                if step.name is None:
                    store.remove_bound_training_job_tombstone_directory(
                        DepartmentScope(department_id),
                        step.candidate.training_job_id,
                        operation_id,
                        identity=step.identity,
                    )
                else:
                    present = store.unlink_bound_training_job_tombstone_file(
                        DepartmentScope(department_id),
                        step.candidate.training_job_id,
                        operation_id,
                        identity=step.identity,
                        name=step.name,
                    )
                    if not present:
                        raise SftArtifactError("final_deletion_recovery_required")
            if step.name is None:
                outcomes[key] = (True, None)
            else:
                _finish_bound_tombstone_unlink(
                    factory,
                    department_id=department_id,
                    actor_issuer=actor_issuer,
                    actor_subject=actor_subject,
                    operation_id=operation_id,
                    step=step,
                )
        except SftArtifactError:
            # After the identity has been committed, no cleanup error may
            # terminalize the reservation. It may represent a post-unlink
            # crash, descriptor churn, or a transient filesystem failure; the
            # exact bound surface must remain fenced for recovery.
            outcomes[key] = (False, "final_deletion_recovery_required")
        if key in outcomes and not outcomes[key][0]:
            # Do not spin on a substituted, partial, or otherwise unsafe
            # bound surface.  The reservation intentionally remains active.
            continue


def _begin_bound_tombstone_step(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
    completed: dict[tuple[UUID, UUID, str], tuple[bool, str | None]],
) -> _BoundTombstoneStep | None:
    try:
        with factory.begin() as session:
            _authorize(session, department_id, actor_issuer, actor_subject, lock=False)
            _locked_operation(session, operation_id, department_id, "purge")
            now = session.scalar(select(func.clock_timestamp()))
            reservations = _active_reservations(session, operation_id, department_id, lock=False)
            _lock_purge_jobs(session, department_id, reservations)
            _authorize(session, department_id, actor_issuer, actor_subject, lock=True)
            for reservation in reservations:
                reservation = _lock_purge_reservation(session, reservation)
                if reservation.status != "tombstone_bound":
                    continue
                job, _attempts, items, owner = _assert_purge_authority(
                    session, department_id, reservation, now
                )
                candidate = _Candidate(
                    job.id,
                    owner.attempt.publication_attempt_id,
                    "final",
                    dict(owner.manifest),
                )
                key = _candidate_key(candidate)
                if key in completed:
                    continue
                final_item = _final_item(items, owner)
                if final_item.status != "registered" or not isinstance(
                    reservation.tombstone_identity, dict
                ):
                    raise ServiceError(409, "Training-job purge authority changed")
                identity = dict(reservation.tombstone_identity)
                remaining = identity.get("remaining_files")
                inflight = identity.get("unlinking_file")
                if not isinstance(remaining, list) or any(
                    not isinstance(name, str) for name in remaining
                ):
                    raise ServiceError(409, "Training-job purge authority changed")
                name = (
                    inflight if isinstance(inflight, str) else (remaining[0] if remaining else None)
                )
                if name is not None and inflight is None:
                    identity["unlinking_file"] = name
                    reservation.tombstone_identity = identity
                    reservation.version += 1
                return _BoundTombstoneStep(candidate, identity, name)
            return None
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _finish_bound_tombstone_unlink(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
    step: _BoundTombstoneStep,
) -> None:
    if step.name is None:
        raise ServiceError(409, "Training-job purge authority changed")
    try:
        with factory.begin() as session:
            _authorize(session, department_id, actor_issuer, actor_subject, lock=False)
            _locked_operation(session, operation_id, department_id, "purge")
            now = session.scalar(select(func.clock_timestamp()))
            reservation = session.execute(
                select(TrainingJobPurgeReservation).where(
                    TrainingJobPurgeReservation.operation_id == operation_id,
                    TrainingJobPurgeReservation.department_id == department_id,
                    TrainingJobPurgeReservation.training_job_id == step.candidate.training_job_id,
                    TrainingJobPurgeReservation.status == "tombstone_bound",
                )
            ).scalar_one_or_none()
            if reservation is None:
                raise ServiceError(409, "Training-job purge authority changed")
            _lock_purge_job_ids(session, department_id, {step.candidate.training_job_id})
            _authorize(session, department_id, actor_issuer, actor_subject, lock=True)
            reservation = _lock_purge_reservation(session, reservation)
            _job, _attempts, _items, owner = _assert_purge_authority(
                session, department_id, reservation, now
            )
            if owner.attempt.publication_attempt_id != step.candidate.attempt_id:
                raise ServiceError(409, "Training-job purge authority changed")
            identity = reservation.tombstone_identity
            if not isinstance(identity, dict) or identity != step.identity:
                raise ServiceError(409, "Training-job purge authority changed")
            remaining = identity.get("remaining_files")
            if identity.get("unlinking_file") != step.name or not isinstance(remaining, list):
                raise ServiceError(409, "Training-job purge authority changed")
            identity = dict(identity)
            identity["remaining_files"] = [name for name in remaining if name != step.name]
            identity["unlinking_file"] = None
            reservation.tombstone_identity = identity
            reservation.version += 1
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _candidate_key(candidate: _Candidate) -> tuple[UUID, UUID, str]:
    return candidate.training_job_id, candidate.attempt_id, candidate.surface


def _authorize(
    session: Session, department_id: UUID, issuer: str, subject: str, *, lock: bool = True
) -> tuple[DepartmentRequestScope, object]:
    scope = DepartmentRequestScope(DepartmentScope(department_id))
    authorization = authorize_transaction(
        session,
        AuthenticatedPrincipal(subject, issuer),
        scope,
        TRAINING_MUTATION_ROLES,
        lock=lock,
        audit_action="training.job.maintenance.authorization",
    )
    return scope, authorization


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ServiceError(422, "Invalid maintenance limit")


def _blocked_reason(error: SftArtifactError) -> str:
    if error.code == "final_deletion_recovery_required":
        return error.code
    return error.code if error.code in _BLOCKED_REASONS else "artifact_ownership_mismatch"


def _retention_anchor(job: TrainingJob):
    if job.review_status == "rejected":
        return job.reviewed_at
    if job.review_status == "archived":
        return job.archived_at
    return None


def _persist_reconcile_outcomes(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, str | None]],
) -> None:
    try:
        with factory.begin() as session:
            scope, authorization = _authorize(session, department_id, actor_issuer, actor_subject)
            operation = _locked_operation(session, operation_id, department_id, "reconcile")
            now = session.scalar(select(func.clock_timestamp()))
            for key, (completed, reason) in outcomes.items():
                item = _locked_item(session, operation_id, department_id, *key)
                _record_item_outcome(item, completed, reason, now)
            for training_job_id, attempt_id, _surface in outcomes:
                _confirm_attempt_if_complete(
                    session,
                    operation_id,
                    department_id,
                    training_job_id,
                    attempt_id,
                    now,
                )
            operation.status = (
                "completed_with_blocks"
                if any(not completed for completed, _ in outcomes.values())
                else "completed"
            )
            operation.completed_at = now
            if any(completed for completed, _reason in outcomes.values()):
                append_mutation_audit(
                    session,
                    actor=authorization.identity,
                    actor_subject=actor_subject,
                    request_scope=scope,
                    action="training.job.reconcile",
                    resource_type="training_job_artifact_operation",
                    resource_id=operation.id,
                )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _authorize_purge_prerequisites(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
) -> None:
    """Reauthorize exact ownership before non-authoritative stage cleanup."""

    try:
        with factory.begin() as session:
            _authorize(session, department_id, actor_issuer, actor_subject, lock=False)
            _locked_operation(session, operation_id, department_id, "purge")
            now = session.scalar(select(func.clock_timestamp()))
            reservations = _active_reservations(session, operation_id, department_id, lock=False)
            if not reservations:
                raise ServiceError(409, "Training-job purge reservation is unavailable")
            _lock_purge_jobs(session, department_id, reservations)
            _authorize(session, department_id, actor_issuer, actor_subject, lock=True)
            for reservation in reservations:
                reservation = _lock_purge_reservation(session, reservation)
                _reject_active_adapter_dependency(
                    session, department_id, reservation.training_job_id
                )
                _assert_purge_authority(session, department_id, reservation, now)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _persist_purge_stage_outcomes(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, str | None]],
) -> None:
    try:
        with factory.begin() as session:
            scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=False
            )
            operation = _locked_operation(session, operation_id, department_id, "purge")
            now = session.scalar(select(func.clock_timestamp()))
            # Lock every target job before any item or reservation row.  This
            # keeps purge outcome persistence in the same job-first order as
            # enqueue, archive, and the other retention fences.
            _lock_purge_job_ids(
                session,
                department_id,
                {training_job_id for training_job_id, _attempt_id, _surface in outcomes},
            )
            scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=True
            )
            for key, (completed, reason) in outcomes.items():
                item = _locked_item(session, operation_id, department_id, *key)
                _record_item_outcome(item, completed, reason, now)
            reservations = _active_reservations(session, operation_id, department_id, lock=False)
            _lock_purge_jobs(session, department_id, reservations)
            for reservation in reservations:
                reservation = _lock_purge_reservation(session, reservation)
                job, attempts, items, _owner = _assert_purge_authority(
                    session, department_id, reservation, now
                )
                if any(item.status == "blocked" for item in items):
                    _terminalize_reservation(reservation, now)
                    continue
                for attempt in attempts:
                    _confirm_attempt_if_complete(
                        session,
                        operation_id,
                        department_id,
                        job.id,
                        attempt.publication_attempt_id,
                        now,
                    )
            _close_purge_operation_if_terminal(
                session,
                operation,
                department_id,
                now,
                audit_actor=authorization.identity,
                audit_subject=actor_subject,
                audit_scope=scope,
            )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _authorize_final_deletion(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
) -> tuple[_Candidate, ...]:
    """Durably authorize the sole final path only after all stages complete."""

    try:
        with factory.begin() as session:
            scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=False
            )
            operation = _locked_operation(session, operation_id, department_id, "purge")
            now = session.scalar(select(func.clock_timestamp()))
            result: list[_Candidate] = []
            reservations = _active_reservations(session, operation_id, department_id, lock=False)
            _lock_purge_jobs(session, department_id, reservations)
            scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=True
            )
            for reservation in reservations:
                reservation = _lock_purge_reservation(session, reservation)
                _reject_active_adapter_dependency(
                    session, department_id, reservation.training_job_id
                )
                if has_active_training_execution(
                    session, department_id, reservation.training_job_id, lock=True
                ):
                    raise ServiceError(409, "Training job has an active execution")
                job, _attempts, items, owner = _assert_purge_authority(
                    session, department_id, reservation, now
                )
                if reservation.status == "tombstone_bound":
                    continue
                if any(item.status == "blocked" for item in items):
                    _terminalize_reservation(reservation, now)
                    continue
                final_item = _final_item(items, owner)
                stages_complete = all(
                    item.status == "completed" for item in items if item.resource_surface == "stage"
                )
                if not stages_complete:
                    continue
                if final_item.status == "completed":
                    continue
                if final_item.status != "registered":
                    raise ServiceError(409, "Training-job purge authority changed")
                if reservation.status == "registered":
                    reservation.status = "deletion_authorized"
                    reservation.deletion_authorized_at = now
                    reservation.version += 1
                if reservation.status != "deletion_authorized":
                    raise ServiceError(409, "Training-job purge authority changed")
                result.append(
                    _Candidate(
                        job.id,
                        owner.attempt.publication_attempt_id,
                        "final",
                        dict(owner.manifest),
                    )
                )
            _close_purge_operation_if_terminal(
                session,
                operation,
                department_id,
                now,
                audit_actor=authorization.identity,
                audit_subject=actor_subject,
                audit_scope=scope,
            )
            return tuple(result)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _persist_purge_final_outcomes(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, str | None]],
) -> None:
    if not outcomes:
        return
    try:
        with factory.begin() as session:
            scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=False
            )
            operation = _locked_operation(session, operation_id, department_id, "purge")
            now = session.scalar(select(func.clock_timestamp()))
            _lock_purge_job_ids(
                session,
                department_id,
                {training_job_id for training_job_id, _attempt_id, _surface in outcomes},
            )
            scope, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=True
            )
            for key, (completed, reason) in sorted(outcomes.items(), key=lambda item: item[0]):
                training_job_id, attempt_id, surface = key
                if surface != "final":
                    raise ServiceError(409, "Training-job purge authority changed")
                reservation = session.execute(
                    select(TrainingJobPurgeReservation)
                    .where(
                        TrainingJobPurgeReservation.operation_id == operation_id,
                        TrainingJobPurgeReservation.department_id == department_id,
                        TrainingJobPurgeReservation.training_job_id == training_job_id,
                        TrainingJobPurgeReservation.status == "tombstone_bound",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if reservation is None:
                    raise ServiceError(409, "Training-job purge authority changed")
                reservation = _lock_purge_reservation(session, reservation)
                job, attempts, items, owner = _assert_purge_authority(
                    session, department_id, reservation, now
                )
                _reject_active_adapter_dependency(session, department_id, job.id)
                if has_active_training_execution(session, department_id, job.id, lock=True):
                    raise ServiceError(409, "Training job has an active execution")
                if owner.attempt.publication_attempt_id != attempt_id:
                    raise ServiceError(409, "Training-job purge authority changed")
                item = _locked_item(session, operation_id, department_id, *key)
                if not completed:
                    if reason == "final_deletion_recovery_required":
                        # The final path may already have moved into its exact
                        # tombstone.  Keep the durable reservation and item
                        # active so the same operation can resume safely.
                        continue
                    _record_item_outcome(item, False, reason, now)
                    _terminalize_reservation(reservation, now)
                    continue
                _record_item_outcome(item, True, None, now)
                for attempt in attempts:
                    _confirm_attempt_if_complete(
                        session,
                        operation_id,
                        department_id,
                        job.id,
                        attempt.publication_attempt_id,
                        now,
                    )
                current_items = _locked_job_items(session, operation_id, department_id, job.id)
                if (
                    all(
                        _terminal_attempt(attempt) and attempt.cleanup_confirmed_at is not None
                        for attempt in attempts
                    )
                    and current_items
                    and all(item.status == "completed" for item in current_items)
                ):
                    job.review_status = "purged"
                    job.purged_at = now
                    job.version += 1
                    _terminalize_reservation(reservation, now)
                    operation.purged_job_count += 1
                    operation.version += 1
            _close_purge_operation_if_terminal(
                session,
                operation,
                department_id,
                now,
                audit_actor=authorization.identity,
                audit_subject=actor_subject,
                audit_scope=scope,
            )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _locked_operation(
    session: Session, operation_id: UUID, department_id: UUID, operation_type: str
) -> TrainingJobArtifactOperation:
    operation = session.execute(
        select(TrainingJobArtifactOperation)
        .where(
            TrainingJobArtifactOperation.id == operation_id,
            TrainingJobArtifactOperation.department_id == department_id,
            TrainingJobArtifactOperation.operation_type == operation_type,
            TrainingJobArtifactOperation.status == "registered",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if operation is None:
        raise ServiceError(409, "Training-job maintenance operation is unavailable")
    return operation


def _locked_item(
    session: Session,
    operation_id: UUID,
    department_id: UUID,
    training_job_id: UUID,
    attempt_id: UUID,
    surface: str,
) -> TrainingJobArtifactOperationItem:
    item = session.execute(
        select(TrainingJobArtifactOperationItem)
        .where(
            TrainingJobArtifactOperationItem.operation_id == operation_id,
            TrainingJobArtifactOperationItem.department_id == department_id,
            TrainingJobArtifactOperationItem.training_job_id == training_job_id,
            TrainingJobArtifactOperationItem.publication_attempt_id == attempt_id,
            TrainingJobArtifactOperationItem.resource_surface == surface,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if item is None or item.status != "registered":
        raise ServiceError(409, "Training-job maintenance operation is unavailable")
    return item


def _record_item_outcome(
    item: TrainingJobArtifactOperationItem,
    completed: bool,
    reason: str | None,
    now,
) -> None:
    if completed:
        item.status = "completed"
        item.completed_at = now
    else:
        item.status = "blocked"
        item.blocked_at = now
        item.blocked_reason_code = reason or "artifact_ownership_mismatch"


def _active_reservations(
    session: Session, operation_id: UUID, department_id: UUID, *, lock: bool = True
) -> list[TrainingJobPurgeReservation]:
    query = (
        select(TrainingJobPurgeReservation)
        .where(
            TrainingJobPurgeReservation.operation_id == operation_id,
            TrainingJobPurgeReservation.department_id == department_id,
            TrainingJobPurgeReservation.status.in_(
                ("registered", "deletion_authorized", "tombstone_bound")
            ),
        )
        .order_by(TrainingJobPurgeReservation.training_job_id)
    )
    if lock:
        query = query.with_for_update()
    return session.scalars(query).all()


def _lock_purge_jobs(
    session: Session,
    department_id: UUID,
    reservations: list[TrainingJobPurgeReservation],
) -> None:
    """Acquire every target job before dependent purge rows."""

    _lock_purge_job_ids(
        session,
        department_id,
        {reservation.training_job_id for reservation in reservations},
    )


def _lock_purge_job_ids(session: Session, department_id: UUID, job_ids: set[UUID]) -> None:
    job_ids = sorted(job_ids, key=str)
    if not job_ids:
        return
    jobs = session.scalars(
        select(TrainingJob)
        .where(
            TrainingJob.department_id == department_id,
            TrainingJob.id.in_(job_ids),
        )
        .order_by(TrainingJob.id)
        .with_for_update()
    ).all()
    if {job.id for job in jobs} != set(job_ids):
        raise ServiceError(409, "Training-job purge authority changed")


def _lock_purge_reservation(
    session: Session, reservation: TrainingJobPurgeReservation
) -> TrainingJobPurgeReservation:
    locked = session.execute(
        select(TrainingJobPurgeReservation)
        .where(TrainingJobPurgeReservation.id == reservation.id)
        .with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise ServiceError(409, "Training-job purge authority changed")
    return locked


def _assert_purge_authority(
    session: Session,
    department_id: UUID,
    reservation: TrainingJobPurgeReservation,
    now,
) -> tuple[
    TrainingJob,
    list[TrainingJobAttempt],
    list[TrainingJobArtifactOperationItem],
    _AuthoritativeFinal,
]:
    job = session.execute(
        select(TrainingJob)
        .where(
            TrainingJob.id == reservation.training_job_id,
            TrainingJob.department_id == department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    attempts = session.scalars(
        select(TrainingJobAttempt)
        .where(
            TrainingJobAttempt.department_id == department_id,
            TrainingJobAttempt.training_job_id == reservation.training_job_id,
        )
        .order_by(TrainingJobAttempt.attempt_number, TrainingJobAttempt.id)
        .with_for_update()
    ).all()
    if job is None or not attempts or not _purge_reservation_matches(reservation, job, now):
        raise ServiceError(409, "Training-job purge authority changed")
    owner = _authoritative_final(job, attempts)
    if not _reservation_binds_owner(reservation, owner):
        raise ServiceError(409, "Training-job purge authority changed")
    if any(not _terminal_attempt(attempt) for attempt in attempts):
        raise ServiceError(409, "Training-job purge authority changed")
    items = _locked_job_items(session, reservation.operation_id, department_id, job.id)
    if not _purge_items_match(items, attempts, owner):
        raise ServiceError(409, "Training-job purge authority changed")
    return job, attempts, items, owner


def _locked_job_items(
    session: Session, operation_id: UUID, department_id: UUID, training_job_id: UUID
) -> list[TrainingJobArtifactOperationItem]:
    return session.scalars(
        select(TrainingJobArtifactOperationItem)
        .where(
            TrainingJobArtifactOperationItem.operation_id == operation_id,
            TrainingJobArtifactOperationItem.department_id == department_id,
            TrainingJobArtifactOperationItem.training_job_id == training_job_id,
        )
        .order_by(
            TrainingJobArtifactOperationItem.resource_surface,
            TrainingJobArtifactOperationItem.publication_attempt_id,
        )
        .with_for_update()
    ).all()


def _purge_items_match(
    items: list[TrainingJobArtifactOperationItem],
    attempts: list[TrainingJobAttempt],
    owner: _AuthoritativeFinal,
) -> bool:
    attempt_ids = {attempt.publication_attempt_id for attempt in attempts}
    finals = [item for item in items if item.resource_surface == "final"]
    if (
        len(finals) != 1
        or finals[0].publication_attempt_id != owner.attempt.publication_attempt_id
        or finals[0].ownership_manifest != owner.manifest
    ):
        return False
    stages = [item for item in items if item.resource_surface == "stage"]
    if any(item.publication_attempt_id not in attempt_ids for item in stages):
        return False
    stage_ids = {item.publication_attempt_id for item in stages}
    return all(
        attempt.cleanup_confirmed_at is not None or attempt.publication_attempt_id in stage_ids
        for attempt in attempts
    )


def _final_item(
    items: list[TrainingJobArtifactOperationItem], owner: _AuthoritativeFinal
) -> TrainingJobArtifactOperationItem:
    matches = [
        item
        for item in items
        if item.resource_surface == "final"
        and item.publication_attempt_id == owner.attempt.publication_attempt_id
        and item.ownership_manifest == owner.manifest
    ]
    if len(matches) != 1:
        raise ServiceError(409, "Training-job purge authority changed")
    return matches[0]


def _confirm_attempt_if_complete(
    session: Session,
    operation_id: UUID,
    department_id: UUID,
    training_job_id: UUID,
    attempt_id: UUID,
    now,
) -> None:
    attempt = session.execute(
        select(TrainingJobAttempt)
        .where(
            TrainingJobAttempt.department_id == department_id,
            TrainingJobAttempt.training_job_id == training_job_id,
            TrainingJobAttempt.publication_attempt_id == attempt_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if attempt is None:
        raise ServiceError(409, "Training-job maintenance operation is unavailable")
    items = session.scalars(
        select(TrainingJobArtifactOperationItem)
        .where(
            TrainingJobArtifactOperationItem.operation_id == operation_id,
            TrainingJobArtifactOperationItem.department_id == department_id,
            TrainingJobArtifactOperationItem.training_job_id == training_job_id,
            TrainingJobArtifactOperationItem.publication_attempt_id == attempt_id,
        )
        .with_for_update()
    ).all()
    if (
        items
        and all(item.status == "completed" for item in items)
        and attempt.cleanup_confirmed_at is None
    ):
        attempt.cleanup_confirmed_at = now
        attempt.version += 1


def _terminalize_reservation(reservation: TrainingJobPurgeReservation, now) -> None:
    reservation.status = "terminalized"
    reservation.terminalized_at = now
    reservation.version += 1


def _close_purge_operation_if_terminal(
    session: Session,
    operation: TrainingJobArtifactOperation,
    department_id: UUID,
    now,
    *,
    audit_actor=None,
    audit_subject: str | None = None,
    audit_scope: DepartmentRequestScope | None = None,
) -> None:
    active = session.scalar(
        select(func.count())
        .select_from(TrainingJobPurgeReservation)
        .where(
            TrainingJobPurgeReservation.operation_id == operation.id,
            TrainingJobPurgeReservation.department_id == department_id,
            TrainingJobPurgeReservation.status.in_(
                ("registered", "deletion_authorized", "tombstone_bound")
            ),
        )
    )
    if active:
        return
    blocked = session.scalar(
        select(func.count())
        .select_from(TrainingJobArtifactOperationItem)
        .where(
            TrainingJobArtifactOperationItem.operation_id == operation.id,
            TrainingJobArtifactOperationItem.department_id == department_id,
            TrainingJobArtifactOperationItem.status == "blocked",
        )
    )
    operation.status = "completed_with_blocks" if blocked else "completed"
    operation.completed_at = now
    operation.version += 1
    if (
        operation.operation_type == "purge"
        and operation.purged_job_count > 0
        and operation.success_audited_at is None
    ):
        if audit_actor is None or audit_subject is None or audit_scope is None:
            raise ServiceError(409, "Training-job purge audit is unavailable")
        append_mutation_audit(
            session,
            actor=audit_actor,
            actor_subject=audit_subject,
            request_scope=audit_scope,
            action="training.job.purge",
            resource_type="training_job_artifact_operation",
            resource_id=operation.id,
        )
        operation.success_audited_at = now
        operation.version += 1


def _has_active_adapter_dependency(
    session: Session, department_id: UUID, training_job_id: UUID
) -> bool:
    return bool(
        session.scalar(
            select(func.count(AdapterUpstreamDependency.id)).where(
                AdapterUpstreamDependency.department_id == department_id,
                AdapterUpstreamDependency.training_job_id == training_job_id,
                AdapterUpstreamDependency.status == "active",
            )
        )
    )


def _reject_active_adapter_dependency(
    session: Session, department_id: UUID, training_job_id: UUID
) -> None:
    dependency = session.execute(
        select(AdapterUpstreamDependency)
        .where(
            AdapterUpstreamDependency.department_id == department_id,
            AdapterUpstreamDependency.training_job_id == training_job_id,
            AdapterUpstreamDependency.status == "active",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if dependency is not None:
        raise ServiceError(409, "Training job is retained by an adapter registry dependency")


def _purge_reservation_matches(
    reservation: TrainingJobPurgeReservation, job: TrainingJob, now
) -> bool:
    return (
        reservation.status in {"registered", "deletion_authorized", "tombstone_bound"}
        and job.status == "succeeded"
        and job.purged_at is None
        and job.review_status == reservation.expected_review_status
        and job.version == reservation.expected_job_version
        and _retention_anchor(job) == reservation.retention_anchor_at
        and reservation.retention_anchor_at <= now - timedelta(days=reservation.retention_days)
    )


def _reservation_binds_owner(
    reservation: TrainingJobPurgeReservation, owner: _AuthoritativeFinal
) -> bool:
    """Keep the durable deletion reservation tied to one exact final owner."""

    return (
        reservation.authoritative_publication_attempt_id == owner.attempt.publication_attempt_id
        and reservation.authoritative_manifest == owner.manifest
        and reservation.tombstone_operation_id == reservation.operation_id
    )
