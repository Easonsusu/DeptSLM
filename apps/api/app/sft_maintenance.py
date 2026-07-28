"""Explicit, bounded Phase 10 source/archive/purge/reconciliation operations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    SftArtifactReconciliationOperation,
    SftArtifactReconciliationOperationItem,
    SftDatasetBuild,
    SftDatasetBuildAttempt,
    SftSourceBundle,
    SftSourceImportAttempt,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.sft_artifacts import SftArtifactError, SftArtifactStore
from app.sft_services import SFT_ADMIN_ROLES


class SftMaintenanceConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SftMaintenanceSettings:
    database_url: str
    data_dir: Path
    retention_days: int

    @classmethod
    def from_environment(cls) -> SftMaintenanceSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        raw_retention = os.getenv("DEPTSLM_SFT_RETENTION_DAYS", "180").strip()
        if not database_url.startswith("postgresql+psycopg://") or not raw_data_dir:
            raise SftMaintenanceConfigurationError("SFT maintenance configuration is invalid.")
        if (
            not raw_retention.isascii()
            or not raw_retention.isdecimal()
            or not 30 <= int(raw_retention) <= 730
        ):
            raise SftMaintenanceConfigurationError(
                "DEPTSLM_SFT_RETENTION_DAYS must be between 30 and 730."
            )
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise SftMaintenanceConfigurationError("SFT dataset storage is unavailable.")
        return cls(database_url, data_dir, int(raw_retention))


@dataclass(frozen=True, slots=True)
class SftMaintenanceResult:
    eligible_count: int
    applied_count: int
    blocked_count: int


def archive_sft_source(
    factory: sessionmaker,
    *,
    department_id: UUID,
    source_bundle_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    apply: bool,
) -> bool:
    try:
        with factory.begin() as session:
            principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
            scope = DepartmentRequestScope(DepartmentScope(department_id))
            authorization = authorize_transaction(
                session,
                principal,
                scope,
                SFT_ADMIN_ROLES,
                lock=True,
                audit_action="sft.source.archive.authorization",
            )
            source = session.execute(
                select(SftSourceBundle)
                .where(
                    SftSourceBundle.id == source_bundle_id,
                    SftSourceBundle.department_id == department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if source is None:
                raise ServiceError(404, "SFT source not found")
            if source.status != "active":
                raise ServiceError(409, "SFT source is unavailable")
            if not apply:
                return False
            source.status = "archived"
            source.archived_at = session.scalar(select(func.clock_timestamp()))
            source.version += 1
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=actor_subject,
                request_scope=scope,
                action="sft.source.archive",
                resource_type="sft_source_bundle",
                resource_id=source.id,
            )
            return True
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def reconcile_sft_artifacts(
    factory: sessionmaker,
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int,
    apply: bool,
) -> SftMaintenanceResult:
    _limit(limit)
    operation_id, items = _register_or_resume_reconciliation(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        limit=limit,
        apply=apply,
    )
    if operation_id is None:
        return SftMaintenanceResult(len(items), 0, 0)
    return _execute_artifact_operation(
        factory,
        data_dir=data_dir,
        operation_id=operation_id,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        items=items,
    )


def purge_sft_artifacts(
    factory: sessionmaker,
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    retention_days: int,
    limit: int,
    apply: bool,
) -> SftMaintenanceResult:
    _limit(limit)
    if not 30 <= retention_days <= 730:
        raise ServiceError(422, "Invalid SFT retention")
    operation_id, items = _register_or_resume_purge(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        retention_days=retention_days,
        limit=limit,
        apply=apply,
    )
    if operation_id is None:
        return SftMaintenanceResult(len(items), 0, 0)
    return _execute_artifact_operation(
        factory,
        data_dir=data_dir,
        operation_id=operation_id,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        items=items,
    )


@dataclass(frozen=True, slots=True)
class _ArtifactOperationItem:
    id: UUID
    resource_type: str
    resource_id: UUID
    attempt_id: UUID
    ownership_manifest: dict[str, object]


def _register_or_resume_reconciliation(
    factory: sessionmaker,
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int,
    apply: bool,
) -> tuple[UUID | None, tuple[_ArtifactOperationItem, ...]]:
    try:
        with factory.begin() as session:
            principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
            scope = DepartmentRequestScope(DepartmentScope(department_id))
            authorization = authorize_transaction(
                session,
                principal,
                scope,
                SFT_ADMIN_ROLES,
                lock=True,
                audit_action="sft.artifact.reconcile.authorization",
            )
            existing = session.execute(
                select(SftArtifactReconciliationOperation)
                .where(
                    SftArtifactReconciliationOperation.department_id == department_id,
                    SftArtifactReconciliationOperation.operation_type == "reconcile",
                    SftArtifactReconciliationOperation.status == "registered",
                )
                .order_by(
                    SftArtifactReconciliationOperation.created_at,
                    SftArtifactReconciliationOperation.id,
                )
                .with_for_update()
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id, _registered_items(session, existing.id, department_id)
            attempts = session.scalars(
                select(SftSourceImportAttempt)
                .where(
                    SftSourceImportAttempt.department_id == department_id,
                    SftSourceImportAttempt.status.in_(("registered", "staged", "published")),
                )
                .order_by(SftSourceImportAttempt.created_at, SftSourceImportAttempt.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            build_attempts = session.scalars(
                select(SftDatasetBuildAttempt)
                .where(
                    SftDatasetBuildAttempt.department_id == department_id,
                    SftDatasetBuildAttempt.status.in_(("reclaimed", "failed", "cancelled")),
                    SftDatasetBuildAttempt.cleanup_confirmed_at.is_(None),
                )
                .order_by(
                    SftDatasetBuildAttempt.created_at,
                    SftDatasetBuildAttempt.id,
                )
                .with_for_update(skip_locked=True)
                .limit(max(0, limit - len(attempts)))
            ).all()
            if not apply:
                return None, _reconciliation_candidates(attempts, build_attempts)
            operation = SftArtifactReconciliationOperation(
                department_id=department_id,
                requested_by_user_id=authorization.identity.id,
                limit_value=limit,
                operation_type="reconcile",
                status="registered",
            )
            session.add(operation)
            session.flush()
            for candidate in _reconciliation_candidates(attempts, build_attempts):
                session.add(
                    SftArtifactReconciliationOperationItem(
                        operation_id=operation.id,
                        department_id=department_id,
                        resource_type=candidate.resource_type,
                        resource_id=candidate.resource_id,
                        attempt_id=candidate.attempt_id,
                        ownership_manifest=candidate.ownership_manifest,
                        status="registered",
                    )
                )
            session.flush()
            return operation.id, _registered_items(session, operation.id, department_id)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _register_or_resume_purge(
    factory: sessionmaker,
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    retention_days: int,
    limit: int,
    apply: bool,
) -> tuple[UUID | None, tuple[_ArtifactOperationItem, ...]]:
    try:
        with factory.begin() as session:
            principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
            scope = DepartmentRequestScope(DepartmentScope(department_id))
            authorization = authorize_transaction(
                session,
                principal,
                scope,
                SFT_ADMIN_ROLES,
                lock=True,
                audit_action="sft.artifact.purge.authorization",
            )
            existing = session.execute(
                select(SftArtifactReconciliationOperation)
                .where(
                    SftArtifactReconciliationOperation.department_id == department_id,
                    SftArtifactReconciliationOperation.operation_type == "purge",
                    SftArtifactReconciliationOperation.status == "registered",
                )
                .order_by(
                    SftArtifactReconciliationOperation.created_at,
                    SftArtifactReconciliationOperation.id,
                )
                .with_for_update()
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id, _registered_items(session, existing.id, department_id)
            cutoff = session.scalar(select(func.clock_timestamp())) - timedelta(days=retention_days)
            builds = session.scalars(
                select(SftDatasetBuild)
                .where(
                    SftDatasetBuild.department_id == department_id,
                    SftDatasetBuild.status == "succeeded",
                    SftDatasetBuild.review_status.in_(("rejected", "archived")),
                    SftDatasetBuild.reviewed_at <= cutoff,
                )
                .order_by(SftDatasetBuild.reviewed_at, SftDatasetBuild.id)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            sources = session.scalars(
                select(SftSourceBundle)
                .where(
                    SftSourceBundle.department_id == department_id,
                    SftSourceBundle.status == "archived",
                    SftSourceBundle.archived_at <= cutoff,
                    ~select(SftDatasetBuild.id)
                    .where(
                        SftDatasetBuild.department_id == department_id,
                        SftDatasetBuild.source_bundle_id == SftSourceBundle.id,
                        SftDatasetBuild.review_status != "purged",
                    )
                    .exists(),
                )
                .order_by(SftSourceBundle.archived_at, SftSourceBundle.id)
                .with_for_update(skip_locked=True)
                .limit(max(0, limit - len(builds)))
            ).all()
            candidates = _purge_candidates(session, builds, sources)
            if not apply:
                return None, tuple(candidates)
            operation = SftArtifactReconciliationOperation(
                department_id=department_id,
                requested_by_user_id=authorization.identity.id,
                limit_value=limit,
                operation_type="purge",
                status="registered",
            )
            session.add(operation)
            session.flush()
            for candidate in candidates:
                session.add(
                    SftArtifactReconciliationOperationItem(
                        operation_id=operation.id,
                        department_id=department_id,
                        resource_type=candidate.resource_type,
                        resource_id=candidate.resource_id,
                        attempt_id=candidate.attempt_id,
                        ownership_manifest=candidate.ownership_manifest,
                        status="registered",
                    )
                )
            session.flush()
            return operation.id, _registered_items(session, operation.id, department_id)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _reconciliation_candidates(
    attempts: list[SftSourceImportAttempt],
    build_attempts: list[SftDatasetBuildAttempt],
) -> tuple[_ArtifactOperationItem, ...]:
    candidates: list[_ArtifactOperationItem] = []
    for attempt in attempts:
        candidates.append(
            _ArtifactOperationItem(
                id=uuid4(),
                resource_type="source_stage",
                resource_id=attempt.source_bundle_id,
                attempt_id=attempt.import_attempt_id,
                ownership_manifest={},
            )
        )
        if isinstance(attempt.artifact_manifest, dict):
            candidates.append(
                _ArtifactOperationItem(
                    id=uuid4(),
                    resource_type="source_final",
                    resource_id=attempt.source_bundle_id,
                    attempt_id=attempt.import_attempt_id,
                    ownership_manifest=dict(attempt.artifact_manifest),
                )
            )
    for attempt in build_attempts:
        candidates.append(
            _ArtifactOperationItem(
                id=uuid4(),
                resource_type="dataset_stage",
                resource_id=attempt.build_id,
                attempt_id=attempt.publication_attempt_id,
                ownership_manifest={},
            )
        )
        if isinstance(attempt.ownership_manifest, dict):
            candidates.append(
                _ArtifactOperationItem(
                    id=uuid4(),
                    resource_type="dataset_final",
                    resource_id=attempt.build_id,
                    attempt_id=attempt.publication_attempt_id,
                    ownership_manifest=dict(attempt.ownership_manifest),
                )
            )
    return tuple(candidates)


def _purge_candidates(
    session: Session,
    builds: list[SftDatasetBuild],
    sources: list[SftSourceBundle],
) -> tuple[_ArtifactOperationItem, ...]:
    candidates: list[_ArtifactOperationItem] = []
    for build in builds:
        if build.publication_attempt_id is None or not isinstance(build.publication_manifest, dict):
            continue
        candidates.append(
            _ArtifactOperationItem(
                id=uuid4(),
                resource_type="dataset_final",
                resource_id=build.id,
                attempt_id=build.publication_attempt_id,
                ownership_manifest=dict(build.publication_manifest),
            )
        )
    for source in sources:
        attempt = session.execute(
            select(SftSourceImportAttempt)
            .where(
                SftSourceImportAttempt.department_id == source.department_id,
                SftSourceImportAttempt.source_bundle_id == source.id,
                SftSourceImportAttempt.status == "committed",
            )
            .order_by(SftSourceImportAttempt.committed_at.desc(), SftSourceImportAttempt.id)
            .limit(1)
        ).scalar_one_or_none()
        if attempt is None or not isinstance(attempt.artifact_manifest, dict):
            continue
        candidates.append(
            _ArtifactOperationItem(
                id=uuid4(),
                resource_type="source_final",
                resource_id=source.id,
                attempt_id=attempt.import_attempt_id,
                ownership_manifest=dict(attempt.artifact_manifest),
            )
        )
    return tuple(candidates)


def _registered_items(
    session: Session, operation_id: UUID, department_id: UUID
) -> tuple[_ArtifactOperationItem, ...]:
    items = session.scalars(
        select(SftArtifactReconciliationOperationItem)
        .where(
            SftArtifactReconciliationOperationItem.operation_id == operation_id,
            SftArtifactReconciliationOperationItem.department_id == department_id,
            SftArtifactReconciliationOperationItem.status == "registered",
        )
        .order_by(
            SftArtifactReconciliationOperationItem.created_at,
            SftArtifactReconciliationOperationItem.id,
        )
    ).all()
    return tuple(
        _ArtifactOperationItem(
            id=item.id,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            attempt_id=item.attempt_id,
            ownership_manifest=dict(item.ownership_manifest),
        )
        for item in items
    )


def _limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ServiceError(422, "Invalid SFT maintenance limit")


def _execute_artifact_operation(
    factory: sessionmaker,
    *,
    data_dir: Path,
    operation_id: UUID,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    items: tuple[_ArtifactOperationItem, ...],
) -> SftMaintenanceResult:
    applied = blocked = 0
    with SftArtifactStore(data_dir) as store:
        for item in items:
            try:
                _remove_item_artifact(store, DepartmentScope(department_id), item)
                _terminalize_artifact_item(
                    factory,
                    operation_id=operation_id,
                    department_id=department_id,
                    item_id=item.id,
                    actor_issuer=actor_issuer,
                    actor_subject=actor_subject,
                    completed=True,
                )
                applied += 1
            except SftArtifactError as error:
                _terminalize_artifact_item(
                    factory,
                    operation_id=operation_id,
                    department_id=department_id,
                    item_id=item.id,
                    actor_issuer=actor_issuer,
                    actor_subject=actor_subject,
                    completed=False,
                    reason=_blocked_reason(error.code),
                )
                blocked += 1
    _complete_artifact_operation(factory, operation_id, department_id, actor_issuer, actor_subject)
    return SftMaintenanceResult(len(items), applied, blocked)


def _remove_item_artifact(
    store: SftArtifactStore, scope: DepartmentScope, item: _ArtifactOperationItem
) -> bool:
    if item.resource_type == "source_stage":
        return store.remove_owned_source_stage(scope, item.resource_id, item.attempt_id)
    if item.resource_type == "dataset_stage":
        return store.remove_owned_dataset_stage(scope, item.resource_id, item.attempt_id)
    if item.resource_type == "source_final":
        return store.remove_owned_source_final(
            scope,
            item.resource_id,
            item.attempt_id,
            expected=item.ownership_manifest,
        )
    if item.resource_type == "dataset_final":
        return store.remove_owned_dataset_final(
            scope,
            item.resource_id,
            item.attempt_id,
            expected=item.ownership_manifest,
        )
    raise SftArtifactError("artifact_ownership_mismatch")


def _terminalize_artifact_item(
    factory: sessionmaker,
    *,
    operation_id: UUID,
    department_id: UUID,
    item_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    completed: bool,
    reason: str | None = None,
) -> None:
    with factory.begin() as session:
        operation = session.execute(
            select(SftArtifactReconciliationOperation)
            .where(
                SftArtifactReconciliationOperation.id == operation_id,
                SftArtifactReconciliationOperation.department_id == department_id,
                SftArtifactReconciliationOperation.status == "registered",
            )
            .with_for_update()
        ).scalar_one_or_none()
        item = session.execute(
            select(SftArtifactReconciliationOperationItem)
            .where(
                SftArtifactReconciliationOperationItem.id == item_id,
                SftArtifactReconciliationOperationItem.operation_id == operation_id,
                SftArtifactReconciliationOperationItem.department_id == department_id,
                SftArtifactReconciliationOperationItem.status == "registered",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if operation is None or item is None:
            return
        principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
        scope = DepartmentRequestScope(DepartmentScope(department_id))
        authorize_transaction(
            session,
            principal,
            scope,
            SFT_ADMIN_ROLES,
            lock=True,
            audit_action=f"sft.artifact.{operation.operation_type}.authorization",
        )
        now = session.scalar(select(func.clock_timestamp()))
        if completed and operation.operation_type == "purge":
            if not _finalize_purge_resource(session, item, department_id, now):
                completed = False
                reason = "artifact_state_changed"
        if completed:
            item.status = "completed"
            item.completed_at = now
            session.flush()
            if operation.operation_type == "reconcile":
                _finalize_reconciliation_resource(
                    session,
                    operation_id=operation.id,
                    item=item,
                    department_id=department_id,
                    now=now,
                )
        else:
            item.status = "blocked"
            item.blocked_at = now
            item.blocked_reason_code = reason or "artifact_ownership_mismatch"


def _complete_artifact_operation(
    factory: sessionmaker,
    operation_id: UUID,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
) -> None:
    with factory.begin() as session:
        operation = session.execute(
            select(SftArtifactReconciliationOperation)
            .where(
                SftArtifactReconciliationOperation.id == operation_id,
                SftArtifactReconciliationOperation.department_id == department_id,
                SftArtifactReconciliationOperation.status == "registered",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if operation is None:
            return
        items = session.scalars(
            select(SftArtifactReconciliationOperationItem).where(
                SftArtifactReconciliationOperationItem.operation_id == operation_id
            )
        ).all()
        if any(item.status == "registered" for item in items):
            return
        principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
        scope = DepartmentRequestScope(DepartmentScope(department_id))
        authorization = authorize_transaction(
            session,
            principal,
            scope,
            SFT_ADMIN_ROLES,
            lock=True,
            audit_action=f"sft.artifact.{operation.operation_type}.authorization",
        )
        operation.status = (
            "completed_with_blocks"
            if any(item.status == "blocked" for item in items)
            else "completed"
        )
        operation.completed_at = session.scalar(select(func.clock_timestamp()))
        if not any(item.status == "blocked" for item in items) and any(
            item.status == "completed" for item in items
        ):
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=actor_subject,
                request_scope=scope,
                action=f"sft.artifact.{operation.operation_type}",
                resource_type="sft_artifact_reconciliation_operation",
                resource_id=operation.id,
            )


def _finalize_purge_resource(
    session: Session,
    item: SftArtifactReconciliationOperationItem,
    department_id: UUID,
    now,
) -> bool:
    if item.resource_type == "dataset_final":
        build = session.execute(
            select(SftDatasetBuild)
            .where(
                SftDatasetBuild.id == item.resource_id,
                SftDatasetBuild.department_id == department_id,
                SftDatasetBuild.status == "succeeded",
                SftDatasetBuild.review_status.in_(("rejected", "archived")),
                SftDatasetBuild.publication_attempt_id == item.attempt_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if build is None:
            return False
        build.review_status = "purged"
        build.purged_at = now
        build.version += 1
        return True
    if item.resource_type == "source_final":
        source = session.execute(
            select(SftSourceBundle)
            .where(
                SftSourceBundle.id == item.resource_id,
                SftSourceBundle.department_id == department_id,
                SftSourceBundle.status == "archived",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if source is None:
            return False
        source.status = "purged"
        source.purged_at = now
        source.version += 1
        return True
    return False


def _finalize_reconciliation_resource(
    session: Session,
    *,
    operation_id: UUID,
    item: SftArtifactReconciliationOperationItem,
    department_id: UUID,
    now,
) -> None:
    family = "source" if item.resource_type.startswith("source_") else "dataset"
    siblings = session.scalars(
        select(SftArtifactReconciliationOperationItem).where(
            SftArtifactReconciliationOperationItem.operation_id == operation_id,
            SftArtifactReconciliationOperationItem.department_id == department_id,
            SftArtifactReconciliationOperationItem.resource_id == item.resource_id,
            SftArtifactReconciliationOperationItem.attempt_id == item.attempt_id,
            SftArtifactReconciliationOperationItem.resource_type.in_(
                ("source_stage", "source_final")
                if family == "source"
                else ("dataset_stage", "dataset_final")
            ),
        )
    ).all()
    # A later reconciliation deliberately creates a new operation item rather
    # than mutating a blocked historical item.  Only a complete current surface
    # set can finalize the exact resource; a stage completion never masks a
    # blocked final artifact.
    if not siblings or any(sibling.status != "completed" for sibling in siblings):
        return
    if item.resource_type in {"source_stage", "source_final"}:
        attempt = session.execute(
            select(SftSourceImportAttempt)
            .where(
                SftSourceImportAttempt.department_id == department_id,
                SftSourceImportAttempt.source_bundle_id == item.resource_id,
                SftSourceImportAttempt.import_attempt_id == item.attempt_id,
                SftSourceImportAttempt.status.in_(("registered", "staged", "published")),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if attempt is not None:
            attempt.status = "failed"
            attempt.failed_at = now
            attempt.cleanup_confirmed_at = now
            attempt.version += 1
        return
    if item.resource_type in {"dataset_stage", "dataset_final"}:
        attempt = session.execute(
            select(SftDatasetBuildAttempt)
            .where(
                SftDatasetBuildAttempt.build_id == item.resource_id,
                SftDatasetBuildAttempt.department_id == department_id,
                SftDatasetBuildAttempt.publication_attempt_id == item.attempt_id,
                SftDatasetBuildAttempt.status.in_(("reclaimed", "failed", "cancelled")),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if attempt is not None:
            attempt.cleanup_confirmed_at = now
            attempt.version += 1
        build = session.execute(
            select(SftDatasetBuild)
            .where(
                SftDatasetBuild.id == item.resource_id,
                SftDatasetBuild.department_id == department_id,
                SftDatasetBuild.publication_attempt_id == item.attempt_id,
                SftDatasetBuild.status.in_(("failed", "cancelled")),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if build is not None and attempt is not None:
            build.artifact_cleanup_confirmed_at = now
            build.version += 1


def _blocked_reason(code: str) -> str:
    return (
        code
        if code
        in {
            "staging_path_unsafe",
            "artifact_ownership_mismatch",
            "artifact_permissions_invalid",
        }
        else "artifact_manifest_invalid"
    )
