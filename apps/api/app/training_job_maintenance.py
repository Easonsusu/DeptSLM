"""Bounded, explicit Phase 11 archive, purge, and reconciliation operations."""

from __future__ import annotations

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
    TrainingJob,
    TrainingJobArtifactOperation,
    TrainingJobArtifactOperationItem,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.sft_artifacts import SftArtifactError, SftArtifactStore
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
            scope, authorization = _authorize(session, department_id, actor_issuer, actor_subject)
            job = session.execute(
                select(TrainingJob)
                .where(
                    TrainingJob.id == training_job_id, TrainingJob.department_id == department_id
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise ServiceError(404, "Training job not found")
            active_reservation = session.execute(
                select(TrainingJobPurgeReservation)
                .where(
                    TrainingJobPurgeReservation.department_id == department_id,
                    TrainingJobPurgeReservation.training_job_id == training_job_id,
                    TrainingJobPurgeReservation.status.in_(("registered", "deletion_authorized")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if active_reservation is not None:
                raise ServiceError(409, "Training job purge is in progress")
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
            _scope, authorization = _authorize(session, department_id, actor_issuer, actor_subject)
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
                return existing.id, tuple(
                    _Candidate(
                        item.training_job_id,
                        item.publication_attempt_id,
                        item.resource_surface,
                        dict(item.ownership_manifest)
                        if isinstance(item.ownership_manifest, dict)
                        else None,
                    )
                    for item in items
                )
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
                    )
                    .order_by(retained_at, TrainingJob.id)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                ).all()
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
            candidates = tuple(_candidates(attempts))
            if not apply:
                return None, candidates
            operation = TrainingJobArtifactOperation(
                id=uuid4(),
                department_id=department_id,
                requested_by_user_id=authorization.identity.id,
                limit_value=limit,
                retention_days=retention_days,
                operation_type=operation_type,
                status="registered",
            )
            session.add(operation)
            session.flush()
            if operation_type == "purge":
                assert retention_days is not None
                for job in jobs:
                    anchor = _retention_anchor(job)
                    if anchor is None:
                        raise ServiceError(409, "Training job purge is unavailable")
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


def _candidates(attempts: list[TrainingJobAttempt]) -> list[_Candidate]:
    result: list[_Candidate] = []
    for attempt in attempts:
        result.append(
            _Candidate(attempt.training_job_id, attempt.publication_attempt_id, "stage", None)
        )
        if isinstance(attempt.ownership_manifest, dict):
            result.append(
                _Candidate(
                    attempt.training_job_id,
                    attempt.publication_attempt_id,
                    "final",
                    dict(attempt.ownership_manifest),
                )
            )
    return result


def _attempt_surfaces(attempts: list[TrainingJobAttempt]) -> set[tuple[UUID, str]]:
    """All and only the surfaces registered for an exact terminal attempt set."""

    surfaces: set[tuple[UUID, str]] = set()
    for attempt in attempts:
        surfaces.add((attempt.publication_attempt_id, "stage"))
        if isinstance(attempt.ownership_manifest, dict):
            surfaces.add((attempt.publication_attempt_id, "final"))
    return surfaces


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
        raise ServiceError(409, "Training-job maintenance operation is unavailable")
    _authorize_purge_deletion(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        operation_id=operation_id,
    )
    applied = 0
    purged_count = 0
    outcomes: dict[tuple[UUID, UUID, str], tuple[bool, str | None]] = {}
    scope = DepartmentScope(department_id)
    for candidate in candidates:
        try:
            with SftArtifactStore(data_dir) as store:
                if candidate.surface == "stage":
                    store.remove_owned_training_job_stage(
                        scope, candidate.training_job_id, candidate.attempt_id
                    )
                elif candidate.manifest is not None:
                    store.remove_owned_training_job_final(
                        scope,
                        candidate.training_job_id,
                        candidate.attempt_id,
                        expected=candidate.manifest,
                    )
                else:
                    raise SftArtifactError("artifact_ownership_mismatch")
            applied += 1
            outcomes[(candidate.training_job_id, candidate.attempt_id, candidate.surface)] = (
                True,
                None,
            )
        except SftArtifactError as error:
            outcomes[(candidate.training_job_id, candidate.attempt_id, candidate.surface)] = (
                False,
                _blocked_reason(error),
            )
    try:
        with factory.begin() as session:
            scope_request, authorization = _authorize(
                session, department_id, actor_issuer, actor_subject
            )
            now = session.scalar(select(func.clock_timestamp()))
            operation = session.execute(
                select(TrainingJobArtifactOperation)
                .where(
                    TrainingJobArtifactOperation.id == operation_id,
                    TrainingJobArtifactOperation.department_id == department_id,
                    TrainingJobArtifactOperation.status == "registered",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if operation is None:
                raise ServiceError(409, "Training-job maintenance operation is unavailable")
            for candidate in candidates:
                completed, reason = outcomes[
                    (candidate.training_job_id, candidate.attempt_id, candidate.surface)
                ]
                item = session.execute(
                    select(TrainingJobArtifactOperationItem)
                    .where(
                        TrainingJobArtifactOperationItem.operation_id == operation_id,
                        TrainingJobArtifactOperationItem.department_id == department_id,
                        TrainingJobArtifactOperationItem.training_job_id
                        == candidate.training_job_id,
                        TrainingJobArtifactOperationItem.publication_attempt_id
                        == candidate.attempt_id,
                        TrainingJobArtifactOperationItem.resource_surface == candidate.surface,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if item is None:
                    raise ServiceError(409, "Training-job maintenance operation is unavailable")
                if completed:
                    item.status = "completed"
                    item.completed_at = now
                else:
                    item.status = "blocked"
                    item.blocked_at = now
                    item.blocked_reason_code = reason
            for training_job_id, attempt_id in {
                (candidate.training_job_id, candidate.attempt_id) for candidate in candidates
            }:
                group = [
                    candidate
                    for candidate in candidates
                    if candidate.training_job_id == training_job_id
                    and candidate.attempt_id == attempt_id
                ]
                if not all(
                    outcomes[(item.training_job_id, item.attempt_id, item.surface)][0]
                    for item in group
                ):
                    continue
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
                required = {item.surface for item in group}
                if {item.resource_surface for item in items} != required or any(
                    item.status != "completed" for item in items
                ):
                    continue
                attempt = session.execute(
                    select(TrainingJobAttempt)
                    .where(
                        TrainingJobAttempt.department_id == department_id,
                        TrainingJobAttempt.training_job_id == training_job_id,
                        TrainingJobAttempt.publication_attempt_id == attempt_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if attempt is not None and attempt.cleanup_confirmed_at is None:
                    attempt.cleanup_confirmed_at = now
                    attempt.version += 1
            if operation.operation_type == "purge":
                for training_job_id in {candidate.training_job_id for candidate in candidates}:
                    attempts = session.scalars(
                        select(TrainingJobAttempt)
                        .where(
                            TrainingJobAttempt.department_id == department_id,
                            TrainingJobAttempt.training_job_id == training_job_id,
                        )
                        .with_for_update()
                    ).all()
                    if not attempts or any(
                        attempt.status not in {"succeeded", "failed", "cancelled", "reclaimed"}
                        or attempt.cleanup_confirmed_at is None
                        for attempt in attempts
                    ):
                        continue
                    items = session.scalars(
                        select(TrainingJobArtifactOperationItem)
                        .where(
                            TrainingJobArtifactOperationItem.operation_id == operation_id,
                            TrainingJobArtifactOperationItem.department_id == department_id,
                            TrainingJobArtifactOperationItem.training_job_id == training_job_id,
                        )
                        .with_for_update()
                    ).all()
                    if (
                        not items
                        or {(item.publication_attempt_id, item.resource_surface) for item in items}
                        != _attempt_surfaces(attempts)
                        or any(item.status != "completed" for item in items)
                    ):
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
                    job = session.execute(
                        select(TrainingJob)
                        .where(
                            TrainingJob.id == training_job_id,
                            TrainingJob.department_id == department_id,
                            TrainingJob.status == "succeeded",
                            TrainingJob.review_status.in_(("rejected", "archived")),
                            TrainingJob.purged_at.is_(None),
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    if (
                        job is not None
                        and reservation is not None
                        and _purge_reservation_matches(reservation, job, now)
                    ):
                        job.review_status = "purged"
                        job.purged_at = now
                        job.version += 1
                        purged_count += 1
            blocked = len(candidates) - applied
            operation.status = "completed_with_blocks" if blocked else "completed"
            operation.completed_at = now
            if operation.operation_type == "purge":
                reservations = session.scalars(
                    select(TrainingJobPurgeReservation)
                    .where(
                        TrainingJobPurgeReservation.operation_id == operation_id,
                        TrainingJobPurgeReservation.department_id == department_id,
                        TrainingJobPurgeReservation.status == "deletion_authorized",
                    )
                    .with_for_update()
                ).all()
                for reservation in reservations:
                    reservation.status = "terminalized"
                    reservation.terminalized_at = now
                    reservation.version += 1
            if applied and operation.operation_type == "reconcile":
                append_mutation_audit(
                    session,
                    actor=authorization.identity,
                    actor_subject=actor_subject,
                    request_scope=scope_request,
                    action="training.job.reconcile",
                    resource_type="training_job_artifact_operation",
                    resource_id=operation.id,
                )
            if purged_count:
                append_mutation_audit(
                    session,
                    actor=authorization.identity,
                    actor_subject=actor_subject,
                    request_scope=scope_request,
                    action="training.job.purge",
                    resource_type="training_job_artifact_operation",
                    resource_id=operation.id,
                )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error
    return TrainingJobMaintenanceResult(len(candidates), applied, len(candidates) - applied)


def _authorize(
    session: Session, department_id: UUID, issuer: str, subject: str
) -> tuple[DepartmentRequestScope, object]:
    scope = DepartmentRequestScope(DepartmentScope(department_id))
    authorization = authorize_transaction(
        session,
        AuthenticatedPrincipal(subject, issuer),
        scope,
        TRAINING_MUTATION_ROLES,
        lock=True,
        audit_action="training.job.maintenance.authorization",
    )
    return scope, authorization


def _limit(value: int) -> None:
    if type(value) is not int or not 1 <= value <= 1000:
        raise ServiceError(422, "Invalid maintenance limit")


def _blocked_reason(error: SftArtifactError) -> str:
    return error.code if error.code in _BLOCKED_REASONS else "artifact_ownership_mismatch"


def _retention_anchor(job: TrainingJob):
    if job.review_status == "rejected":
        return job.reviewed_at
    if job.review_status == "archived":
        return job.archived_at
    return None


def _purge_reservation_matches(
    reservation: TrainingJobPurgeReservation, job: TrainingJob, now
) -> bool:
    return (
        reservation.status == "deletion_authorized"
        and job.status == "succeeded"
        and job.purged_at is None
        and job.review_status == reservation.expected_review_status
        and job.version == reservation.expected_job_version
        and _retention_anchor(job) == reservation.retention_anchor_at
        and reservation.retention_anchor_at <= now - timedelta(days=reservation.retention_days)
    )


def _authorize_purge_deletion(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    operation_id: UUID,
) -> None:
    """Durably fence purge authority before any external descriptor deletion."""

    try:
        with factory.begin() as session:
            _authorize(session, department_id, actor_issuer, actor_subject)
            operation = session.execute(
                select(TrainingJobArtifactOperation)
                .where(
                    TrainingJobArtifactOperation.id == operation_id,
                    TrainingJobArtifactOperation.department_id == department_id,
                    TrainingJobArtifactOperation.status == "registered",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if operation is None:
                raise ServiceError(409, "Training-job maintenance operation is unavailable")
            if operation.operation_type != "purge":
                return
            now = session.scalar(select(func.clock_timestamp()))
            reservations = session.scalars(
                select(TrainingJobPurgeReservation)
                .where(
                    TrainingJobPurgeReservation.operation_id == operation_id,
                    TrainingJobPurgeReservation.department_id == department_id,
                    TrainingJobPurgeReservation.status.in_(("registered", "deletion_authorized")),
                )
                .order_by(TrainingJobPurgeReservation.training_job_id)
                .with_for_update()
            ).all()
            if not reservations:
                raise ServiceError(409, "Training-job purge reservation is unavailable")
            for reservation in reservations:
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
                    .with_for_update()
                ).all()
                items = session.scalars(
                    select(TrainingJobArtifactOperationItem)
                    .where(
                        TrainingJobArtifactOperationItem.operation_id == operation_id,
                        TrainingJobArtifactOperationItem.department_id == department_id,
                        TrainingJobArtifactOperationItem.training_job_id
                        == reservation.training_job_id,
                    )
                    .with_for_update()
                ).all()
                if (
                    job is None
                    or not attempts
                    or any(
                        attempt.status not in {"succeeded", "failed", "cancelled", "reclaimed"}
                        for attempt in attempts
                    )
                    or {(item.publication_attempt_id, item.resource_surface) for item in items}
                    != _attempt_surfaces(attempts)
                    or any(item.status != "registered" for item in items)
                    or not _purge_reservation_predelete_matches(reservation, job, now)
                ):
                    raise ServiceError(409, "Training-job purge authority changed")
            for reservation in reservations:
                if reservation.status == "registered":
                    reservation.status = "deletion_authorized"
                    reservation.deletion_authorized_at = now
                    reservation.version += 1
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _purge_reservation_predelete_matches(
    reservation: TrainingJobPurgeReservation, job: TrainingJob, now
) -> bool:
    return (
        reservation.status in {"registered", "deletion_authorized"}
        and job.status == "succeeded"
        and job.purged_at is None
        and job.review_status == reservation.expected_review_status
        and job.version == reservation.expected_job_version
        and _retention_anchor(job) == reservation.retention_anchor_at
        and reservation.retention_anchor_at <= now - timedelta(days=reservation.retention_days)
    )
