"""Explicit, bounded Phase 10 source/archive/purge/reconciliation operations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    PersistentAuditEvent,
    SftArtifactReconciliationOperation,
    SftArtifactReconciliationOperationItem,
    SftDatasetBuild,
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
    candidates: list[tuple[str, UUID, UUID]] = []
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
            attempts = session.scalars(
                select(SftSourceImportAttempt)
                .where(
                    SftSourceImportAttempt.department_id == department_id,
                    SftSourceImportAttempt.status.in_(("registered", "staged", "published")),
                )
                .order_by(SftSourceImportAttempt.created_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).all()
            builds = session.scalars(
                select(SftDatasetBuild)
                .where(
                    SftDatasetBuild.department_id == department_id,
                    SftDatasetBuild.status.in_(("failed", "cancelled")),
                    SftDatasetBuild.publication_attempt_id.is_not(None),
                )
                .order_by(SftDatasetBuild.created_at)
                .with_for_update(skip_locked=True)
                .limit(max(0, limit - len(attempts)))
            ).all()
            if not apply:
                return SftMaintenanceResult(len(attempts) + len(builds), 0, 0)
            operation = SftArtifactReconciliationOperation(
                department_id=department_id,
                requested_by_user_id=authorization.identity.id,
                limit_value=limit,
                status="registered",
            )
            session.add(operation)
            session.flush()
            for attempt in attempts:
                session.add(
                    SftArtifactReconciliationOperationItem(
                        operation_id=operation.id,
                        department_id=department_id,
                        resource_type="source_import",
                        resource_id=attempt.id,
                        status="registered",
                    )
                )
                candidates.append(("source", attempt.source_bundle_id, attempt.import_attempt_id))
            for build in builds:
                session.add(
                    SftArtifactReconciliationOperationItem(
                        operation_id=operation.id,
                        department_id=department_id,
                        resource_type="dataset_build",
                        resource_id=build.id,
                        status="registered",
                    )
                )
                candidates.append(("dataset", build.id, build.publication_attempt_id))
            operation.status = "completed"
            operation.completed_at = session.scalar(select(func.clock_timestamp()))
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=actor_subject,
                request_scope=scope,
                action="sft.artifact.reconcile",
                resource_type="sft_artifact_reconciliation_operation",
                resource_id=operation.id,
            )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error
    store = SftArtifactStore(data_dir)
    applied = blocked = 0
    for kind, resource_id, attempt_id in candidates:
        try:
            removed = (
                store.remove_owned_source_stage(
                    DepartmentScope(department_id), resource_id, attempt_id
                )
                if kind == "source"
                else store.remove_owned_dataset_stage(
                    DepartmentScope(department_id), resource_id, attempt_id
                )
            )
            applied += int(removed)
        except SftArtifactError:
            blocked += 1
    return SftMaintenanceResult(len(candidates), applied, blocked)


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
            if not apply:
                return SftMaintenanceResult(len(builds) + len(sources), 0, 0)
            store = SftArtifactStore(data_dir)
            applied = 0
            for build in builds:
                if build.publication_attempt_id is None or not all(
                    (
                        build.result_manifest_sha256,
                        build.train_sha256,
                        build.validation_sha256,
                        build.provenance_sha256,
                    )
                ):
                    continue
                if store.remove_owned_dataset_final(
                    scope.department,
                    build.id,
                    build.publication_attempt_id,
                    attempt_number=build.attempt_number,
                    code_revision=build.code_revision,
                    manifest_sha256=build.result_manifest_sha256,
                    train_sha256=build.train_sha256,
                    validation_sha256=build.validation_sha256,
                    provenance_sha256=build.provenance_sha256,
                ):
                    build.review_status = "purged"
                    build.purged_at = session.scalar(select(func.clock_timestamp()))
                    build.version += 1
                    applied += 1
            for source in sources:
                attempt = session.execute(
                    select(SftSourceImportAttempt)
                    .where(
                        SftSourceImportAttempt.department_id == department_id,
                        SftSourceImportAttempt.source_bundle_id == source.id,
                        SftSourceImportAttempt.status == "committed",
                    )
                    .order_by(SftSourceImportAttempt.committed_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if attempt is None:
                    continue
                if store.remove_owned_source_final(
                    scope.department,
                    source.id,
                    attempt.import_attempt_id,
                    manifest_sha256=source.manifest_sha256,
                    examples_sha256=source.examples_sha256,
                ):
                    source.status = "purged"
                    source.purged_at = session.scalar(select(func.clock_timestamp()))
                    source.version += 1
                    applied += 1
            if applied:
                session.add(
                    PersistentAuditEvent(
                        actor_subject=actor_subject,
                        actor_user_id=authorization.identity.id,
                        department_id=department_id,
                        action="sft.artifact.purge",
                        resource_type="sft_dataset_build",
                        resource_id="maintenance",
                        result="allowed",
                        reason_code="mutation_applied",
                    )
                )
            return SftMaintenanceResult(len(builds) + len(sources), applied, 0)
    except SftArtifactError as error:
        raise ServiceError(409, "SFT artifact is unavailable") from error
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ServiceError(422, "Invalid SFT maintenance limit")
