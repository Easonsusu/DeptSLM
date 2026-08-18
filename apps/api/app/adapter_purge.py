"""Crash-resumable, department-scoped adapter artifact purge.

This module is intentionally separate from the Phase 12.1E-A reconciliation
service.  PostgreSQL is the durable authority for the exact adapter, source,
attempts, manifests, tombstone namespace, and unlink progress; external
filesystem operations are descriptor-bound compensating steps.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_maintenance_artifacts import (
    AdapterMaintenanceArtifactError,
    AdapterPurgeArtifactStore,
    BoundSurface,
    InspectedSurface,
    PhysicalSurfaceIdentifier,
    RetryablePurgeTombstoneNamespaceConflict,
    physical_surface_identifier,
)
from app.adapter_registry_domain import canonical_json_bytes
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    ADAPTER_PURGE_BLOCKED_REASONS,
    Adapter,
    AdapterArtifactOperationItem,
    AdapterDeploymentOperation,
    AdapterEvaluationRun,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterPurgeItem,
    AdapterPurgeOperation,
    AdapterPurgeReservation,
    AdapterRegistryAttempt,
    AdapterReview,
    AdapterRollbackRetention,
    DepartmentAdapterDeployment,
    RagAnswerRun,
    RagAnswerRuntimeSnapshot,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction

ADAPTER_PURGE_ADMIN_ROLES = frozenset(
    {DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN}
)
PURGE_SURFACES = ("registry_final", "source_final")
ACTIVE_PURGE_STATUSES = ("registered", "deleting")
ACTIVE_ITEM_STATUSES = ("registered", "verified", "tombstone_bound", "deleting")


class AdapterPurgeConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterPurgeSettings:
    database_url: str
    data_dir: Path
    max_operations: int
    max_items: int

    @classmethod
    def from_environment(cls) -> AdapterPurgeSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        raw_operations = os.getenv("DEPTSLM_ADAPTER_PURGE_MAX_OPERATIONS", "1").strip()
        raw_items = os.getenv("DEPTSLM_ADAPTER_PURGE_MAX_ITEMS", "2").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise AdapterPurgeConfigurationError("Database configuration is invalid.")
        if not raw_data_dir:
            raise AdapterPurgeConfigurationError("DEPTSLM_DATA_DIR is required.")
        try:
            max_operations = _bounded_int(raw_operations, 1, 10)
            max_items = _bounded_int(raw_items, 2, 2000)
        except ValueError as error:
            raise AdapterPurgeConfigurationError("Adapter purge bounds are invalid.") from error
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not _private_directory(data_dir):
            raise AdapterPurgeConfigurationError("Adapter storage is unavailable.")
        required = (
            data_dir / "adapters",
            data_dir / "adapters" / "imports",
            data_dir / "adapters" / "registry",
            data_dir / "adapters" / ".staging" / "imports",
            data_dir / "adapters" / ".staging" / "registry",
            data_dir / "adapters" / ".purge-deleting" / "source_stage",
            data_dir / "adapters" / ".purge-deleting" / "source_final",
            data_dir / "adapters" / ".purge-deleting" / "registry_stage",
            data_dir / "adapters" / ".purge-deleting" / "registry_final",
        )
        try:
            if any(not _private_directory(path) for path in required):
                raise AdapterPurgeConfigurationError("Adapter storage is unavailable.")
        except OSError as error:
            raise AdapterPurgeConfigurationError("Adapter storage is unavailable.") from error
        return cls(database_url, data_dir, max_operations, max_items)


@dataclass(frozen=True, slots=True)
class AdapterPurgeResult:
    eligible_count: int
    applied_count: int
    blocked_count: int
    operation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class _Authority:
    adapter: Adapter
    source: AdapterImportSource
    source_attempt: AdapterImportAttempt
    registry_attempt: AdapterRegistryAttempt


@dataclass(frozen=True, slots=True)
class _MoveContext:
    """Immutable in-memory context captured around one filesystem inspection."""

    scope: PhysicalSurfaceIdentifier
    authority_snapshot: dict[str, object]
    expected_manifest: dict[str, object]
    expected_manifest_sha256: str | None
    expected_manifest_byte_size: int | None
    expected_tombstone_namespace: dict[str, object]
    target_adapter_version: int
    expected_resource_version: int
    expected_attempt_version: int
    item_version: int
    reservation_version: int


def _bounded_int(raw: object, minimum: int, maximum: int) -> int:
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdecimal():
        raise ValueError
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError
    return value


def _private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        raise ServiceError(422, "Invalid adapter purge operation limit")


def _item_limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 2 <= value <= 2000:
        raise ServiceError(422, "Invalid adapter purge item limit")


def _safe_adapter_id(value: object) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ServiceError(422, "Invalid adapter selector")
    return value


def _authority_snapshot(authority: _Authority) -> dict[str, object]:
    adapter = authority.adapter
    source = authority.source
    source_attempt = authority.source_attempt
    registry_attempt = authority.registry_attempt
    return {
        "adapter": {
            "id": str(adapter.id),
            "department_id": str(adapter.department_id),
            "version": adapter.version,
            "status": adapter.status,
            "source_bundle_id": str(adapter.source_bundle_id),
            "publication_attempt_id": str(adapter.publication_attempt_id),
            "attempt_number": adapter.attempt_number,
            "registry_manifest_sha256": adapter.registry_manifest_sha256,
            "registry_adapter_config_sha256": adapter.registry_adapter_config_sha256,
            "registry_adapter_config_byte_size": adapter.registry_adapter_config_byte_size,
            "registry_adapter_model_sha256": adapter.registry_adapter_model_sha256,
            "registry_adapter_model_byte_size": adapter.registry_adapter_model_byte_size,
        },
        "source": {
            "id": str(source.id),
            "department_id": str(source.department_id),
            "version": source.version,
            "status": source.status,
            "authoritative_attempt_id": str(source.authoritative_attempt_id),
            "intake_manifest_sha256": source.intake_manifest_sha256,
            "intake_manifest_byte_size": source.intake_manifest_byte_size,
            "adapter_config_sha256": source.adapter_config_sha256,
            "adapter_config_byte_size": source.adapter_config_byte_size,
            "adapter_model_sha256": source.adapter_model_sha256,
            "adapter_model_byte_size": source.adapter_model_byte_size,
        },
        "source_attempt": {
            "id": str(source_attempt.id),
            "department_id": str(source_attempt.department_id),
            "source_bundle_id": str(source_attempt.source_bundle_id),
            "publication_attempt_id": str(source_attempt.publication_attempt_id),
            "attempt_number": source_attempt.attempt_number,
            "version": source_attempt.version,
            "status": source_attempt.status,
        },
        "registry_attempt": {
            "id": str(registry_attempt.id),
            "department_id": str(registry_attempt.department_id),
            "adapter_id": str(registry_attempt.adapter_id),
            "publication_attempt_id": str(registry_attempt.publication_attempt_id),
            "attempt_number": registry_attempt.attempt_number,
            "version": registry_attempt.version,
            "status": registry_attempt.status,
        },
    }


def _target_adapter_version(snapshot: object) -> int:
    """Return the immutable adapter version captured before E-B registration."""

    if not isinstance(snapshot, dict):
        raise ServiceError(409, "Adapter purge authority changed")
    adapter = snapshot.get("adapter")
    version = adapter.get("version") if isinstance(adapter, dict) else None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ServiceError(409, "Adapter purge authority changed")
    return version


def _load_authority(
    session: Session,
    department_id: UUID,
    adapter_id: UUID,
) -> _Authority:
    adapter = session.execute(
        select(Adapter)
        .where(Adapter.id == adapter_id, Adapter.department_id == department_id)
        .with_for_update()
    ).scalar_one_or_none()
    if adapter is None:
        raise ServiceError(404, "Adapter not found")
    source = session.execute(
        select(AdapterImportSource)
        .where(
            AdapterImportSource.id == adapter.source_bundle_id,
            AdapterImportSource.department_id == department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    source_attempt = session.execute(
        select(AdapterImportAttempt)
        .where(
            AdapterImportAttempt.id == adapter.source_authoritative_attempt_id,
            AdapterImportAttempt.department_id == department_id,
            AdapterImportAttempt.source_bundle_id == adapter.source_bundle_id,
            AdapterImportAttempt.publication_attempt_id == adapter.source_publication_attempt_id,
            AdapterImportAttempt.attempt_number == adapter.source_attempt_number,
        )
        .with_for_update()
    ).scalar_one_or_none()
    registry_attempt = session.execute(
        select(AdapterRegistryAttempt)
        .where(
            AdapterRegistryAttempt.execution_scope_id == adapter.execution_scope_id,
            AdapterRegistryAttempt.department_id == department_id,
            AdapterRegistryAttempt.adapter_id == adapter.id,
            AdapterRegistryAttempt.publication_attempt_id == adapter.publication_attempt_id,
            AdapterRegistryAttempt.attempt_number == adapter.attempt_number,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if source is None or source_attempt is None or registry_attempt is None:
        raise ServiceError(409, "Adapter purge authority is unavailable")
    authority = _Authority(adapter, source, source_attempt, registry_attempt)
    _assert_eligible(session, authority, department_id)
    return authority


def _assert_eligible(
    session: Session,
    authority: _Authority,
    department_id: UUID,
    *,
    target_adapter_version: int | None = None,
) -> None:
    adapter = authority.adapter
    source = authority.source
    source_attempt = authority.source_attempt
    registry_attempt = authority.registry_attempt
    if (
        adapter.department_id != department_id
        or source.department_id != department_id
        or source_attempt.department_id != department_id
        or registry_attempt.department_id != department_id
        or adapter.status not in {"validated", "purge_pending"}
        or source.status not in {"consumed", "purge_pending"}
        or source.claimed_adapter_id != adapter.id
        or source.authoritative_attempt_id != source_attempt.id
        or source_attempt.status != "committed"
        or registry_attempt.status != "succeeded"
        or adapter.execution_scope_id != registry_attempt.execution_scope_id
        or adapter.verified_governance_lineage is not True
        or adapter.verified_artifact_compatibility is not True
        or adapter.worker_id is not None
        or adapter.claim_token is not None
        or adapter.lease_expires_at is not None
        or not isinstance(source_attempt.ownership_manifest, dict)
        or not isinstance(registry_attempt.ownership_manifest, dict)
    ):
        raise ServiceError(409, "Adapter purge authority changed")
    active_reconcile = session.scalar(
        select(AdapterArtifactOperationItem.id)
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.status.in_(ACTIVE_ITEM_STATUSES),
            (
                (AdapterArtifactOperationItem.surface_type == "source_final")
                & (AdapterArtifactOperationItem.source_bundle_id == source.id)
            )
            | (
                (AdapterArtifactOperationItem.surface_type == "registry_final")
                & (AdapterArtifactOperationItem.adapter_id == adapter.id)
            ),
        )
        .limit(1)
    )
    if active_reconcile is not None:
        raise ServiceError(409, "Adapter purge conflicts with reconciliation")
    _assert_governance_fences(
        session,
        adapter,
        department_id,
        target_adapter_version=target_adapter_version,
    )
    if adapter.status == "validated" and source.status == "consumed":
        active_purge = session.scalar(
            select(AdapterPurgeOperation.id).where(
                AdapterPurgeOperation.department_id == department_id,
                AdapterPurgeOperation.adapter_id == adapter.id,
                AdapterPurgeOperation.status.in_(ACTIVE_PURGE_STATUSES),
            )
        )
        if active_purge is not None:
            raise ServiceError(409, "Adapter purge is already active")


def _assert_governance_fences(
    session: Session,
    adapter: Adapter,
    department_id: UUID,
    *,
    target_adapter_version: int | None = None,
) -> None:
    """Keep E-B registration and finalization fenced by governance metadata."""

    active_evaluation = session.scalar(
        select(AdapterEvaluationRun.id)
        .where(
            AdapterEvaluationRun.department_id == department_id,
            AdapterEvaluationRun.adapter_id == adapter.id,
            AdapterEvaluationRun.status.in_(("queued", "running")),
        )
        .limit(1)
    )
    if active_evaluation is not None:
        raise ServiceError(409, "Adapter purge conflicts with active evaluation")
    active_review = session.scalar(
        select(AdapterReview.id).where(
            AdapterReview.department_id == department_id,
            AdapterReview.adapter_id == adapter.id,
            AdapterReview.adapter_version == adapter.version,
            AdapterReview.status == "pending",
        )
    )
    if active_review is not None:
        raise ServiceError(409, "Adapter purge conflicts with pending review")
    deployment = session.scalar(
        select(DepartmentAdapterDeployment.id).where(
            DepartmentAdapterDeployment.department_id == department_id,
            DepartmentAdapterDeployment.target_kind == "adapter",
            DepartmentAdapterDeployment.adapter_id == adapter.id,
            DepartmentAdapterDeployment.adapter_version
            == (adapter.version if target_adapter_version is None else target_adapter_version),
        )
    )
    if deployment is not None:
        raise ServiceError(409, "Adapter purge conflicts with current adapter deployment")
    retention = session.scalar(
        select(AdapterRollbackRetention.id).where(
            AdapterRollbackRetention.department_id == department_id,
            AdapterRollbackRetention.adapter_id == adapter.id,
            AdapterRollbackRetention.adapter_version == adapter.version,
            AdapterRollbackRetention.status == "active",
        )
    )
    if retention is not None:
        raise ServiceError(409, "Adapter purge conflicts with rollback retention")
    deployment_operation = session.scalar(
        select(AdapterDeploymentOperation.id).where(
            AdapterDeploymentOperation.department_id == department_id,
            AdapterDeploymentOperation.status.in_(("queued", "running")),
            (
                (AdapterDeploymentOperation.target_adapter_id == adapter.id)
                | (AdapterDeploymentOperation.current_adapter_id == adapter.id)
            ),
        )
    )
    if deployment_operation is not None:
        raise ServiceError(409, "Adapter purge conflicts with deployment operation")
    active_runtime = session.scalar(
        select(RagAnswerRuntimeSnapshot.id)
        .join(
            RagAnswerRun,
            (RagAnswerRun.id == RagAnswerRuntimeSnapshot.run_id)
            & (RagAnswerRun.department_id == RagAnswerRuntimeSnapshot.department_id),
        )
        .where(
            RagAnswerRuntimeSnapshot.department_id == department_id,
            RagAnswerRuntimeSnapshot.target_kind == "adapter",
            RagAnswerRuntimeSnapshot.adapter_id == adapter.id,
            RagAnswerRuntimeSnapshot.adapter_version
            == (adapter.version if target_adapter_version is None else target_adapter_version),
            RagAnswerRun.status == "running",
        )
        .limit(1)
    )
    if active_runtime is not None:
        raise ServiceError(409, "Adapter purge conflicts with active RAG runtime snapshot")


def _address(
    surface_type: str, department_id: UUID, authority: _Authority
) -> PhysicalSurfaceIdentifier:
    if surface_type == "source_final":
        return physical_surface_identifier(surface_type, department_id, authority.source.id, None)
    return physical_surface_identifier(surface_type, department_id, authority.adapter.id, None)


def _manifest_for(surface_type: str, authority: _Authority) -> dict[str, object]:
    manifest = (
        authority.source_attempt.ownership_manifest
        if surface_type == "source_final"
        else authority.registry_attempt.ownership_manifest
    )
    if not isinstance(manifest, dict):
        raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
    return dict(manifest)


def _manifest_digest_fields(
    surface_type: str, authority: _Authority
) -> tuple[str | None, int | None]:
    if surface_type == "source_final":
        return authority.source.intake_manifest_sha256, authority.source.intake_manifest_byte_size
    raw = canonical_json_bytes(authority.registry_attempt.ownership_manifest)
    digest = hashlib.sha256(raw).hexdigest()
    if authority.adapter.registry_manifest_sha256 not in {None, digest}:
        raise AdapterMaintenanceArtifactError("artifact_authority_changed")
    return digest, len(raw)


def _register_or_resume(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    adapter_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int,
    item_limit: int,
    apply: bool,
) -> tuple[UUID | None, tuple[UUID, ...]]:
    try:
        with factory.begin() as session:
            principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
            scope = DepartmentRequestScope(DepartmentScope(department_id))
            authorization = authorize_transaction(
                session,
                principal,
                scope,
                ADAPTER_PURGE_ADMIN_ROLES,
                lock=True,
                audit_action=None,
            )
            existing = session.execute(
                select(AdapterPurgeOperation)
                .where(
                    AdapterPurgeOperation.department_id == department_id,
                    AdapterPurgeOperation.adapter_id == adapter_id,
                    AdapterPurgeOperation.status.in_(ACTIVE_PURGE_STATUSES),
                )
                .order_by(AdapterPurgeOperation.created_at, AdapterPurgeOperation.id)
                .limit(1)
                .with_for_update()
            ).scalar_one_or_none()
            if existing is not None:
                authority = _load_authority_allow_pending(session, department_id, adapter_id)
                _assert_governance_fences(
                    session,
                    authority.adapter,
                    department_id,
                    target_adapter_version=_target_adapter_version(existing.authority_snapshot),
                )
                items = session.scalars(
                    select(AdapterPurgeItem.id)
                    .where(
                        AdapterPurgeItem.operation_id == existing.id,
                        AdapterPurgeItem.department_id == department_id,
                        AdapterPurgeItem.status != "completed",
                    )
                    .order_by(AdapterPurgeItem.created_at, AdapterPurgeItem.id)
                ).all()
                return existing.id, tuple(items)
            selected = session.execute(
                select(Adapter)
                .where(Adapter.id == adapter_id, Adapter.department_id == department_id)
                .with_for_update()
            ).scalar_one_or_none()
            if selected is None:
                raise ServiceError(404, "Adapter not found")
            if selected.status == "purged":
                return None, ()
            authority = _load_authority(session, department_id, adapter_id)
            if not apply:
                return None, (uuid4(), uuid4())
            if item_limit < 2:
                raise ServiceError(422, "Adapter purge item limit must cover source and registry")
            operation = AdapterPurgeOperation(
                id=uuid4(),
                department_id=department_id,
                adapter_id=adapter_id,
                source_bundle_id=authority.source.id,
                requested_by_user_id=authorization.identity.id,
                limit_value=limit,
                item_limit_value=item_limit,
                status="registered",
                expected_adapter_version=authority.adapter.version,
                expected_source_version=authority.source.version,
                expected_source_attempt_version=authority.source_attempt.version,
                expected_registry_attempt_version=authority.registry_attempt.version,
                source_authoritative_attempt_id=authority.source_attempt.id,
                source_publication_attempt_id=authority.source_attempt.publication_attempt_id,
                source_attempt_number=authority.source_attempt.attempt_number,
                registry_attempt_id=authority.registry_attempt.id,
                registry_publication_attempt_id=authority.registry_attempt.publication_attempt_id,
                registry_attempt_number=authority.registry_attempt.attempt_number,
                authority_snapshot=_authority_snapshot(authority),
                eligible_item_count=2,
                version=1,
            )
            session.add(operation)
            authority.adapter.status = "purge_pending"
            authority.adapter.version += 1
            authority.source.status = "purge_pending"
            authority.source.version += 1
            operation.expected_adapter_version = authority.adapter.version
            operation.expected_source_version = authority.source.version
            operation.expected_source_attempt_version = authority.source_attempt.version
            operation.expected_registry_attempt_version = authority.registry_attempt.version
            session.flush()
            for surface_type in PURGE_SURFACES:
                if surface_type == "source_final":
                    resource_version = authority.source.version
                    attempt_version = authority.source_attempt.version
                    resource_status = "consumed"
                    attempt_status = "committed"
                    attempt_id = authority.source_attempt.id
                    publication_id = authority.source_attempt.publication_attempt_id
                    attempt_number = authority.source_attempt.attempt_number
                else:
                    resource_version = authority.adapter.version
                    attempt_version = authority.registry_attempt.version
                    resource_status = "validated"
                    attempt_status = "succeeded"
                    attempt_id = authority.registry_attempt.id
                    publication_id = authority.registry_attempt.publication_attempt_id
                    attempt_number = authority.registry_attempt.attempt_number
                reservation = AdapterPurgeReservation(
                    id=uuid4(),
                    operation_id=operation.id,
                    department_id=department_id,
                    adapter_id=authority.adapter.id,
                    source_bundle_id=authority.source.id,
                    surface_type=surface_type,
                    import_attempt_id=attempt_id if surface_type == "source_final" else None,
                    registry_attempt_id=attempt_id if surface_type == "registry_final" else None,
                    publication_attempt_id=publication_id,
                    attempt_number=attempt_number,
                    expected_resource_version=resource_version,
                    expected_attempt_version=attempt_version,
                    expected_resource_status=resource_status,
                    expected_attempt_status=attempt_status,
                    authority_manifest=_manifest_for(surface_type, authority),
                    authority_snapshot=_authority_snapshot(authority),
                    status="registered",
                    version=1,
                )
                session.add(reservation)
                session.flush()
                session.add(
                    AdapterPurgeItem(
                        id=uuid4(),
                        operation_id=operation.id,
                        reservation_id=reservation.id,
                        department_id=department_id,
                        surface_type=surface_type,
                        adapter_id=authority.adapter.id,
                        source_bundle_id=authority.source.id,
                        import_attempt_id=reservation.import_attempt_id,
                        registry_attempt_id=reservation.registry_attempt_id,
                        publication_attempt_id=publication_id,
                        attempt_number=attempt_number,
                        expected_item_version=1,
                        ownership_manifest=_manifest_for(surface_type, authority),
                        status="registered",
                        version=1,
                    )
                )
            session.flush()
            return operation.id, tuple(
                session.scalars(
                    select(AdapterPurgeItem.id)
                    .where(AdapterPurgeItem.operation_id == operation.id)
                    .order_by(AdapterPurgeItem.created_at, AdapterPurgeItem.id)
                ).all()
            )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def purge_adapter_artifacts(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    adapter_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int = 1,
    item_limit: int = 2,
    apply: bool = False,
) -> AdapterPurgeResult:
    """Perform one bounded dry-run or resumable exact adapter purge."""

    _limit(limit)
    _item_limit(item_limit)
    adapter_id = _safe_adapter_id(adapter_id)
    operation_id, item_ids = _register_or_resume(
        factory,
        department_id=department_id,
        adapter_id=adapter_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        limit=limit,
        item_limit=item_limit,
        apply=apply,
    )
    if not apply or operation_id is None:
        return AdapterPurgeResult(len(item_ids), 0, 0, None)
    _mark_operation_deleting(factory, operation_id, department_id)
    applied = blocked = 0
    # Registry final is intentionally first.  Source bytes are never removed
    # until the authoritative registry final has independently been verified.
    for surface_type in PURGE_SURFACES:
        if surface_type == "source_final" and not _registry_completed(
            factory, operation_id, department_id
        ):
            for item_id in _ordered_item_ids(factory, operation_id, department_id, surface_type):
                _block_item(
                    factory,
                    operation_id,
                    item_id,
                    department_id,
                    "purge_dependency_active",
                )
            break
        for item_id in _ordered_item_ids(factory, operation_id, department_id, surface_type):
            try:
                if _execute_item(
                    factory,
                    data_dir=data_dir,
                    operation_id=operation_id,
                    item_id=item_id,
                    department_id=department_id,
                    actor_issuer=actor_issuer,
                    actor_subject=actor_subject,
                ):
                    applied += 1
            except RetryablePurgeTombstoneNamespaceConflict as error:
                # The expected item and its durable move intent remain valid,
                # but an external canonical/private sibling exists in the
                # exact namespace. Do not terminalize the item or start
                # source deletion; an operator must remove only that sibling
                # before this same operation can resume.
                raise ServiceError(
                    409, "Adapter purge recovery requires external conflict resolution"
                ) from error
            except AdapterMaintenanceArtifactError as error:
                _block_item(factory, operation_id, item_id, department_id, error.code)
                blocked += 1
            except ServiceError:
                raise
    _finalize_operation(
        factory,
        data_dir=data_dir,
        operation_id=operation_id,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
    )
    return AdapterPurgeResult(len(item_ids), applied, blocked, operation_id)


def _registry_completed(
    factory: sessionmaker[Session], operation_id: UUID, department_id: UUID
) -> bool:
    with factory.begin() as session:
        item = session.scalar(
            select(AdapterPurgeItem).where(
                AdapterPurgeItem.operation_id == operation_id,
                AdapterPurgeItem.department_id == department_id,
                AdapterPurgeItem.surface_type == "registry_final",
            )
        )
        return item is not None and item.status == "completed"


def _ordered_item_ids(
    factory: sessionmaker[Session], operation_id: UUID, department_id: UUID, surface_type: str
) -> tuple[UUID, ...]:
    with factory.begin() as session:
        return tuple(
            session.scalars(
                select(AdapterPurgeItem.id)
                .where(
                    AdapterPurgeItem.operation_id == operation_id,
                    AdapterPurgeItem.department_id == department_id,
                    AdapterPurgeItem.surface_type == surface_type,
                    AdapterPurgeItem.status.in_(ACTIVE_ITEM_STATUSES),
                )
                .order_by(AdapterPurgeItem.created_at, AdapterPurgeItem.id)
            ).all()
        )


def _mark_operation_deleting(
    factory: sessionmaker[Session], operation_id: UUID, department_id: UUID
) -> None:
    with factory.begin() as session:
        operation = session.execute(
            select(AdapterPurgeOperation)
            .where(
                AdapterPurgeOperation.id == operation_id,
                AdapterPurgeOperation.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if operation is None:
            raise ServiceError(409, "Adapter purge operation is unavailable")
        if operation.status == "registered":
            operation.status = "deleting"
            operation.version += 1


def _item_inspection(item: AdapterPurgeItem) -> InspectedSurface:
    if (
        not isinstance(item.observed_identity, dict)
        or not isinstance(item.deletion_plan, list)
        or not all(
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and isinstance(entry.get("identity"), dict)
            for entry in item.deletion_plan
        )
    ):
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    resource_id = item.source_bundle_id if item.surface_type == "source_final" else item.adapter_id
    return InspectedSurface(
        item.surface_type,
        item.department_id,
        resource_id,
        None,
        item.id,
        dict(item.observed_identity),
        [
            {"name": entry["name"], "identity": dict(entry["identity"])}
            for entry in item.deletion_plan
        ],
    )


def _bound_surface(item: AdapterPurgeItem) -> BoundSurface:
    if (
        not isinstance(item.observed_identity, dict)
        or not isinstance(item.deletion_plan, list)
        or not isinstance(item.tombstone_identity, dict)
        or not isinstance(item.expected_tombstone_namespace, dict)
    ):
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    resource_id = item.source_bundle_id if item.surface_type == "source_final" else item.adapter_id
    return BoundSurface(
        item.surface_type,
        item.department_id,
        resource_id,
        None,
        item.id,
        dict(item.observed_identity),
        list(item.deletion_plan),
        dict(item.tombstone_identity),
    )


def _load_item(
    factory: sessionmaker[Session], operation_id: UUID, item_id: UUID, department_id: UUID
):
    with factory.begin() as session:
        item = session.execute(
            select(AdapterPurgeItem)
            .where(
                AdapterPurgeItem.id == item_id,
                AdapterPurgeItem.operation_id == operation_id,
                AdapterPurgeItem.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        reservation = session.execute(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.department_id == department_id,
                AdapterPurgeReservation.surface_type
                == (item.surface_type if item is not None else "registry_final"),
            )
            .with_for_update()
        ).scalar_one_or_none()
        if item is None or reservation is None:
            raise ServiceError(409, "Adapter purge item is unavailable")
        # The application factory disables expiration, but keep this helper
        # safe for ordinary SQLAlchemy sessionmakers used by maintenance tests.
        session.expunge(item)
        session.expunge(reservation)
        return item, reservation


def _move_context_for(
    current: AdapterPurgeItem,
    reservation: AdapterPurgeReservation,
    authority: _Authority,
    department_id: UUID,
    target_adapter_version: int,
) -> _MoveContext:
    resource_id = (
        current.source_bundle_id if current.surface_type == "source_final" else current.adapter_id
    )
    scope = physical_surface_identifier(current.surface_type, department_id, resource_id, None)
    expected_sha, expected_size = _manifest_digest_fields(current.surface_type, authority)
    return _MoveContext(
        scope=scope,
        authority_snapshot=_authority_snapshot(authority),
        expected_manifest=_manifest_for(current.surface_type, authority),
        expected_manifest_sha256=expected_sha,
        expected_manifest_byte_size=expected_size,
        expected_tombstone_namespace={
            "surface_type": current.surface_type,
            "department_id": str(department_id),
            "resource_id": str(resource_id),
            "item_id": str(current.id),
        },
        target_adapter_version=target_adapter_version,
        expected_resource_version=(
            authority.source.version
            if current.surface_type == "source_final"
            else authority.adapter.version
        ),
        expected_attempt_version=(
            authority.source_attempt.version
            if current.surface_type == "source_final"
            else authority.registry_attempt.version
        ),
        item_version=current.version,
        reservation_version=reservation.version,
    )


def _assert_move_context(
    session: Session,
    current: AdapterPurgeItem,
    reservation: AdapterPurgeReservation,
    authority: _Authority,
    context: _MoveContext,
    department_id: UUID,
) -> None:
    """Prove the short authorization transaction still owns the inspected state."""

    _assert_eligible(
        session,
        authority,
        department_id,
        target_adapter_version=context.target_adapter_version,
    )
    if (
        current.status != "registered"
        or reservation.status != "registered"
        or current.version != context.item_version
        or reservation.version != context.reservation_version
        or _authority_snapshot(authority) != context.authority_snapshot
        or (
            authority.source.version
            if current.surface_type == "source_final"
            else authority.adapter.version
        )
        != context.expected_resource_version
        or (
            authority.source_attempt.version
            if current.surface_type == "source_final"
            else authority.registry_attempt.version
        )
        != context.expected_attempt_version
    ):
        raise AdapterMaintenanceArtifactError("artifact_authority_changed")


def _capture_registered_context(
    factory: sessionmaker[Session],
    operation_id: UUID,
    item_id: UUID,
    department_id: UUID,
) -> _MoveContext:
    """Capture exact authority while holding only the short row locks."""

    with factory.begin() as session:
        current = session.execute(
            select(AdapterPurgeItem)
            .where(
                AdapterPurgeItem.id == item_id,
                AdapterPurgeItem.operation_id == operation_id,
                AdapterPurgeItem.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        reservation = session.execute(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.department_id == department_id,
                AdapterPurgeReservation.surface_type == current.surface_type,
            )
            .with_for_update()
        ).scalar_one()
        operation = session.execute(
            select(AdapterPurgeOperation)
            .where(
                AdapterPurgeOperation.id == operation_id,
                AdapterPurgeOperation.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        authority = _load_authority_allow_pending(session, department_id, current.adapter_id)
        target_adapter_version = _target_adapter_version(operation.authority_snapshot)
        _assert_eligible(
            session,
            authority,
            department_id,
            target_adapter_version=target_adapter_version,
        )
        if current.status != "registered" or reservation.status != "registered":
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        return _move_context_for(
            current,
            reservation,
            authority,
            department_id,
            target_adapter_version,
        )


def _persist_move_intent(
    factory: sessionmaker[Session],
    *,
    operation_id: UUID,
    item_id: UUID,
    department_id: UUID,
    inspected: InspectedSurface,
    context: _MoveContext,
) -> AdapterPurgeItem:
    """Commit the verified observation before any filesystem rename."""

    if inspected.address != context.scope or inspected.item_id != item_id:
        raise AdapterMaintenanceArtifactError("artifact_authority_changed")
    with factory.begin() as session:
        current = session.execute(
            select(AdapterPurgeItem)
            .where(
                AdapterPurgeItem.id == item_id,
                AdapterPurgeItem.operation_id == operation_id,
                AdapterPurgeItem.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        reservation = session.execute(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.department_id == department_id,
                AdapterPurgeReservation.surface_type == current.surface_type,
            )
            .with_for_update()
        ).scalar_one()
        authority = _load_authority_allow_pending(session, department_id, current.adapter_id)
        _assert_move_context(session, current, reservation, authority, context, department_id)
        current.observed_identity = dict(inspected.observed_identity)
        current.deletion_plan = list(inspected.deletion_plan)
        current.expected_tombstone_namespace = dict(context.expected_tombstone_namespace)
        current.status = "verified"
        current.verified_at = session.scalar(select(func.clock_timestamp()))
        current.move_authorized_at = current.verified_at
        current.version += 1
        reservation.status = "deletion_authorized"
        reservation.deletion_authorized_at = current.verified_at
        reservation.expected_tombstone_namespace = dict(context.expected_tombstone_namespace)
        reservation.observed_identity = dict(current.observed_identity)
        reservation.deletion_plan = list(current.deletion_plan)
        reservation.version += 1
        session.flush()
        session.expunge(current)
        return current


def _assert_verified_move_context(
    factory: sessionmaker[Session],
    *,
    operation_id: UUID,
    item_id: UUID,
    department_id: UUID,
) -> None:
    """Recheck the immutable target immediately before moving retained bytes."""

    with factory.begin() as session:
        current = session.execute(
            select(AdapterPurgeItem)
            .where(
                AdapterPurgeItem.id == item_id,
                AdapterPurgeItem.operation_id == operation_id,
                AdapterPurgeItem.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        reservation = session.execute(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.department_id == department_id,
                AdapterPurgeReservation.surface_type == current.surface_type,
            )
            .with_for_update()
        ).scalar_one()
        operation = session.execute(
            select(AdapterPurgeOperation)
            .where(
                AdapterPurgeOperation.id == operation_id,
                AdapterPurgeOperation.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        if current.status != "verified" or reservation.status != "deletion_authorized":
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        authority = _load_authority_allow_pending(session, department_id, current.adapter_id)
        _assert_eligible(
            session,
            authority,
            department_id,
            target_adapter_version=_target_adapter_version(operation.authority_snapshot),
        )


def _bind_move_intent(
    factory: sessionmaker[Session],
    *,
    operation_id: UUID,
    item_id: UUID,
    department_id: UUID,
    bound: BoundSurface,
) -> AdapterPurgeItem:
    """Commit the exact tombstone identity in a transaction after the move."""

    with factory.begin() as session:
        current = session.execute(
            select(AdapterPurgeItem)
            .where(
                AdapterPurgeItem.id == item_id,
                AdapterPurgeItem.operation_id == operation_id,
                AdapterPurgeItem.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        reservation = session.execute(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.department_id == department_id,
                AdapterPurgeReservation.surface_type == current.surface_type,
            )
            .with_for_update()
        ).scalar_one()
        operation = session.execute(
            select(AdapterPurgeOperation)
            .where(
                AdapterPurgeOperation.id == operation_id,
                AdapterPurgeOperation.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        if current.status == "tombstone_bound":
            if current.tombstone_identity != bound.tombstone_identity:
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            session.expunge(current)
            return current
        if current.status != "verified" or reservation.status != "deletion_authorized":
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        authority = _load_authority_allow_pending(session, department_id, current.adapter_id)
        _assert_eligible(
            session,
            authority,
            department_id,
            target_adapter_version=_target_adapter_version(operation.authority_snapshot),
        )
        expected_snapshot = reservation.authority_snapshot
        if (
            not isinstance(expected_snapshot, dict)
            or _authority_snapshot(authority) != expected_snapshot
            or current.expected_tombstone_namespace != reservation.expected_tombstone_namespace
            or bound.tombstone_identity is None
        ):
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        current.tombstone_identity = dict(bound.tombstone_identity)
        current.status = "tombstone_bound"
        current.tombstone_bound_at = session.scalar(select(func.clock_timestamp()))
        current.deletion_started_at = current.tombstone_bound_at
        current.version += 1
        reservation.tombstone_identity = dict(bound.tombstone_identity)
        reservation.status = "tombstone_bound"
        reservation.tombstone_bound_at = current.tombstone_bound_at
        reservation.deletion_started_at = current.deletion_started_at
        reservation.version += 1
        session.flush()
        session.expunge(current)
        return current


def _execute_item(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    operation_id: UUID,
    item_id: UUID,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
) -> bool:
    item, reservation = _load_item(factory, operation_id, item_id, department_id)
    if item.status == "completed":
        return False
    if item.status == "registered":
        context = _capture_registered_context(factory, operation_id, item_id, department_id)
        with AdapterPurgeArtifactStore(data_dir) as store:
            inspected = store.inspect_surface(
                context.scope,
                item_id,
                expected_manifest=context.expected_manifest,
                expected_manifest_sha256=context.expected_manifest_sha256,
                expected_manifest_byte_size=context.expected_manifest_byte_size,
            )
        if inspected is None:
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        _persist_move_intent(
            factory,
            operation_id=operation_id,
            item_id=item_id,
            department_id=department_id,
            inspected=inspected,
            context=context,
        )
        item, reservation = _load_item(factory, operation_id, item_id, department_id)
    if item.status == "verified":
        inspected = _item_inspection(item)
        expected_namespace = item.expected_tombstone_namespace or {}
        _assert_verified_move_context(
            factory,
            operation_id=operation_id,
            item_id=item_id,
            department_id=department_id,
        )
        with AdapterPurgeArtifactStore(data_dir) as store:
            try:
                bound = store.move_verified_surface_to_tombstone(
                    inspected,
                    expected_tombstone_namespace=expected_namespace,
                )
            except RetryablePurgeTombstoneNamespaceConflict:
                # A valid external sibling existed before the initial rename.
                # It is not evidence that the expected move happened, so do
                # not send this retryable conflict into post-rename recovery.
                raise
            except AdapterMaintenanceArtifactError:
                # A move may have completed before a process crash or a
                # post-rename storage error. Recovery never uses a boolean
                # membership probe: it opens the exact resource namespace and
                # requires the durable item to be its sole tombstone.
                bound = store.open_exact_authorized_recovery_tombstone(
                    inspected,
                    expected_tombstone_namespace=expected_namespace,
                )
            if bound is None:
                bound = store.open_exact_authorized_recovery_tombstone(
                    inspected,
                    expected_tombstone_namespace=expected_namespace,
                )
        item = _bind_move_intent(
            factory,
            operation_id=operation_id,
            item_id=item_id,
            department_id=department_id,
            bound=bound,
        )
    bound = _bound_surface(item)
    plan = list(item.deletion_plan or [])
    for index, entry in enumerate(plan):
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
        with factory.begin() as session:
            current = session.execute(
                select(AdapterPurgeItem)
                .where(
                    AdapterPurgeItem.id == item_id, AdapterPurgeItem.department_id == department_id
                )
                .with_for_update()
            ).scalar_one()
            if current.status == "completed":
                return False
            if current.next_entry_index > index:
                continue
            if current.next_entry_index != index:
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            persisted_in_flight = (
                current.in_flight_entry if isinstance(current.in_flight_entry, dict) else None
            )
            missing_is_exact_retry = bool(
                persisted_in_flight is not None
                and persisted_in_flight.get("index") == index
                and persisted_in_flight.get("name") == name
            )
            if persisted_in_flight is not None and not missing_is_exact_retry:
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            current.status = "deleting"
            current.in_flight_entry = {"name": name, "index": index}
            current.version += 1
            reservation = session.execute(
                select(AdapterPurgeReservation)
                .where(
                    AdapterPurgeReservation.operation_id == operation_id,
                    AdapterPurgeReservation.department_id == department_id,
                    AdapterPurgeReservation.surface_type == current.surface_type,
                )
                .with_for_update()
            ).scalar_one()
            reservation.status = "deleting"
            reservation.in_flight_entry = {"name": name, "index": index}
            reservation.next_entry_index = current.next_entry_index
            reservation.version += 1
            session.flush()
        with AdapterPurgeArtifactStore(data_dir) as store:
            store.unlink_committed_tombstone_entry(
                bound, name, allow_missing=missing_is_exact_retry
            )
        with factory.begin() as session:
            current = session.execute(
                select(AdapterPurgeItem)
                .where(
                    AdapterPurgeItem.id == item_id, AdapterPurgeItem.department_id == department_id
                )
                .with_for_update()
            ).scalar_one()
            current.next_entry_index = max(current.next_entry_index, index + 1)
            current.in_flight_entry = None
            current.version += 1
            reservation = session.execute(
                select(AdapterPurgeReservation)
                .where(
                    AdapterPurgeReservation.operation_id == operation_id,
                    AdapterPurgeReservation.department_id == department_id,
                    AdapterPurgeReservation.surface_type == current.surface_type,
                )
                .with_for_update()
            ).scalar_one()
            reservation.next_entry_index = current.next_entry_index
            reservation.in_flight_entry = None
            reservation.version += 1
            session.flush()
    with factory.begin() as session:
        current = session.execute(
            select(AdapterPurgeItem)
            .where(
                AdapterPurgeItem.id == item_id,
                AdapterPurgeItem.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one()
        if current.next_entry_index != len(plan) or current.in_flight_entry is not None:
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        prior_directory_unlink_started_at = current.directory_unlink_started_at
        if prior_directory_unlink_started_at is None:
            current.directory_unlink_started_at = session.scalar(select(func.clock_timestamp()))
            current.version += 1
        reservation = session.execute(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.department_id == department_id,
                AdapterPurgeReservation.surface_type == current.surface_type,
            )
            .with_for_update()
        ).scalar_one()
        if reservation.directory_unlink_started_at is None:
            reservation.directory_unlink_started_at = current.directory_unlink_started_at
            reservation.version += 1
        allow_missing_directory = prior_directory_unlink_started_at is not None
    with AdapterPurgeArtifactStore(data_dir) as store:
        store.remove_committed_tombstone_directory(bound, allow_missing=allow_missing_directory)
    with factory.begin() as session:
        current = session.execute(
            select(AdapterPurgeItem)
            .where(AdapterPurgeItem.id == item_id, AdapterPurgeItem.department_id == department_id)
            .with_for_update()
        ).scalar_one()
        reservation = session.execute(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.department_id == department_id,
                AdapterPurgeReservation.surface_type == current.surface_type,
            )
            .with_for_update()
        ).scalar_one()
        current.status = "completed"
        current.completed_at = session.scalar(select(func.clock_timestamp()))
        current.in_flight_entry = None
        current.version += 1
        reservation.status = "completed"
        reservation.completed_at = current.completed_at
        reservation.version += 1
    return True


def _block_item(
    factory: sessionmaker[Session],
    operation_id: UUID,
    item_id: UUID,
    department_id: UUID,
    code: str,
) -> None:
    if code not in ADAPTER_PURGE_BLOCKED_REASONS:
        code = "purge_authority_changed"
    try:
        with factory.begin() as session:
            item = session.execute(
                select(AdapterPurgeItem)
                .where(
                    AdapterPurgeItem.id == item_id,
                    AdapterPurgeItem.operation_id == operation_id,
                    AdapterPurgeItem.department_id == department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if item is None or item.status == "completed":
                return
            item.status = "blocked"
            item.blocked_reason_code = code
            item.blocked_at = session.scalar(select(func.clock_timestamp()))
            item.version += 1
            reservation = session.execute(
                select(AdapterPurgeReservation)
                .where(
                    AdapterPurgeReservation.operation_id == operation_id,
                    AdapterPurgeReservation.department_id == department_id,
                    AdapterPurgeReservation.surface_type == item.surface_type,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if reservation is not None:
                reservation.status = "blocked"
                reservation.blocked_reason_code = code
                reservation.blocked_at = item.blocked_at
                reservation.version += 1
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _finalize_operation(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    operation_id: UUID,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
) -> None:
    try:
        with factory.begin() as session:
            # The department authorization fence is always acquired before
            # the active purge operation, reservations, items, or upstream
            # rows. This is the shared lock-order boundary with E-C and the
            # Phase 12.1C registry enqueue path.
            principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
            scope = DepartmentRequestScope(DepartmentScope(department_id))
            authorization = authorize_transaction(
                session, principal, scope, ADAPTER_PURGE_ADMIN_ROLES, lock=True, audit_action=None
            )
            operation = session.execute(
                select(AdapterPurgeOperation)
                .where(
                    AdapterPurgeOperation.id == operation_id,
                    AdapterPurgeOperation.department_id == department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if operation is None or operation.status not in ACTIVE_PURGE_STATUSES:
                return
            active_evaluation = session.scalar(
                select(AdapterEvaluationRun.id)
                .where(
                    AdapterEvaluationRun.department_id == department_id,
                    AdapterEvaluationRun.adapter_id == operation.adapter_id,
                    AdapterEvaluationRun.status.in_(("queued", "running")),
                )
                .limit(1)
            )
            if active_evaluation is not None:
                raise ServiceError(409, "Adapter purge conflicts with active evaluation")
            items = session.scalars(
                select(AdapterPurgeItem)
                .where(
                    AdapterPurgeItem.operation_id == operation_id,
                    AdapterPurgeItem.department_id == department_id,
                )
                .with_for_update()
            ).all()
            blocked = [item for item in items if item.status == "blocked"]
            if any(item.status not in {"completed", "blocked"} for item in items):
                return
            if not blocked:
                try:
                    with AdapterPurgeArtifactStore(data_dir) as store:
                        for item in items:
                            resource_id = (
                                item.source_bundle_id
                                if item.surface_type == "source_final"
                                else item.adapter_id
                            )
                            store.assert_tombstone_namespace_empty(
                                physical_surface_identifier(
                                    item.surface_type,
                                    item.department_id,
                                    resource_id,
                                    None,
                                )
                            )
                except AdapterMaintenanceArtifactError as error:
                    # Filesystem and PostgreSQL are not atomically fenced.
                    # Keep the completed item state and exact operation
                    # authority active so an operator can remove only the
                    # unexpected tombstone and rerun finalization.
                    raise ServiceError(
                        409, "Adapter purge finalization requires external conflict resolution"
                    ) from error
            operation.completed_item_count = sum(item.status == "completed" for item in items)
            operation.blocked_item_count = len(blocked)
            operation.status = "completed_with_blocks" if blocked else "completed"
            operation.completed_at = session.scalar(select(func.clock_timestamp()))
            operation.version += 1
            if blocked:
                return
            authority = _load_authority_allow_pending(session, department_id, operation.adapter_id)
            snapshot = (
                operation.authority_snapshot
                if isinstance(operation.authority_snapshot, dict)
                else {}
            )
            target_adapter_version = _target_adapter_version(snapshot)
            _assert_governance_fences(
                session,
                authority.adapter,
                department_id,
                target_adapter_version=target_adapter_version,
            )
            _assert_operation_authority(operation, authority, department_id)
            authority.source.status = "purged"
            authority.source.purged_at = operation.completed_at
            authority.source.version += 1
            authority.adapter.status = "purged"
            authority.adapter.purged_at = operation.completed_at
            authority.adapter.version += 1
            if operation.success_audited_at is None:
                append_mutation_audit(
                    session,
                    actor=authorization.identity,
                    actor_subject=actor_subject,
                    request_scope=scope,
                    action="adapter.purge",
                    resource_type="adapter_purge_operation",
                    resource_id=operation.id,
                )
                operation.success_audited_at = operation.completed_at
                operation.version += 1
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _load_authority_allow_pending(
    session: Session, department_id: UUID, adapter_id: UUID
) -> _Authority:
    adapter = session.execute(
        select(Adapter)
        .where(Adapter.id == adapter_id, Adapter.department_id == department_id)
        .with_for_update()
    ).scalar_one()
    source = session.execute(
        select(AdapterImportSource)
        .where(
            AdapterImportSource.id == adapter.source_bundle_id,
            AdapterImportSource.department_id == department_id,
        )
        .with_for_update()
    ).scalar_one()
    source_attempt = session.execute(
        select(AdapterImportAttempt)
        .where(
            AdapterImportAttempt.id == adapter.source_authoritative_attempt_id,
            AdapterImportAttempt.department_id == department_id,
            AdapterImportAttempt.source_bundle_id == adapter.source_bundle_id,
            AdapterImportAttempt.publication_attempt_id == adapter.source_publication_attempt_id,
            AdapterImportAttempt.attempt_number == adapter.source_attempt_number,
        )
        .with_for_update()
    ).scalar_one()
    registry_attempt = session.execute(
        select(AdapterRegistryAttempt)
        .where(
            AdapterRegistryAttempt.execution_scope_id == adapter.execution_scope_id,
            AdapterRegistryAttempt.department_id == department_id,
            AdapterRegistryAttempt.adapter_id == adapter.id,
            AdapterRegistryAttempt.publication_attempt_id == adapter.publication_attempt_id,
            AdapterRegistryAttempt.attempt_number == adapter.attempt_number,
        )
        .with_for_update()
    ).scalar_one()
    if adapter.status != "purge_pending" or source.status != "purge_pending":
        raise ServiceError(409, "Adapter purge authority changed")
    return _Authority(adapter, source, source_attempt, registry_attempt)


def _assert_operation_authority(
    operation: AdapterPurgeOperation, authority: _Authority, department_id: UUID
) -> None:
    """Revalidate every immutable authority field before final lifecycle mutation."""

    adapter = authority.adapter
    source = authority.source
    source_attempt = authority.source_attempt
    registry_attempt = authority.registry_attempt
    if (
        adapter.department_id != department_id
        or source.department_id != department_id
        or source_attempt.department_id != department_id
        or registry_attempt.department_id != department_id
        or adapter.id != operation.adapter_id
        or source.id != operation.source_bundle_id
        or adapter.source_bundle_id != source.id
        or source.authoritative_attempt_id != source_attempt.id
        or adapter.source_authoritative_attempt_id != source_attempt.id
        or adapter.source_publication_attempt_id != source_attempt.publication_attempt_id
        or adapter.source_attempt_number != source_attempt.attempt_number
        or operation.source_authoritative_attempt_id != source_attempt.id
        or operation.source_publication_attempt_id != source_attempt.publication_attempt_id
        or operation.source_attempt_number != source_attempt.attempt_number
        or operation.registry_attempt_id != registry_attempt.id
        or operation.registry_publication_attempt_id != registry_attempt.publication_attempt_id
        or operation.registry_attempt_number != registry_attempt.attempt_number
        or adapter.publication_attempt_id != registry_attempt.publication_attempt_id
        or adapter.attempt_number != registry_attempt.attempt_number
        or adapter.execution_scope_id != registry_attempt.execution_scope_id
        or adapter.version != operation.expected_adapter_version
        or source.version != operation.expected_source_version
        or source_attempt.version != operation.expected_source_attempt_version
        or registry_attempt.version != operation.expected_registry_attempt_version
        or adapter.status != "purge_pending"
        or source.status != "purge_pending"
        or source.claimed_adapter_id != adapter.id
        or adapter.worker_id is not None
        or adapter.claim_token is not None
        or adapter.lease_expires_at is not None
        or source_attempt.status != "committed"
        or registry_attempt.status != "succeeded"
        or adapter.verified_governance_lineage is not True
        or adapter.verified_artifact_compatibility is not True
        or not isinstance(source_attempt.ownership_manifest, dict)
        or not isinstance(registry_attempt.ownership_manifest, dict)
    ):
        raise ServiceError(409, "Adapter purge authority changed")
    expected = operation.authority_snapshot
    current = _authority_snapshot(authority)
    if not isinstance(expected, dict):
        raise ServiceError(409, "Adapter purge authority changed")
    for section in ("adapter", "source", "source_attempt", "registry_attempt"):
        expected_section = expected.get(section)
        current_section = current.get(section)
        if not isinstance(expected_section, dict) or not isinstance(current_section, dict):
            raise ServiceError(409, "Adapter purge authority changed")
        for key, expected_value in expected_section.items():
            if key in {"status", "version"}:
                continue
            if current_section.get(key) != expected_value:
                raise ServiceError(409, "Adapter purge authority changed")


__all__ = [
    "AdapterPurgeConfigurationError",
    "AdapterPurgeResult",
    "AdapterPurgeSettings",
    "purge_adapter_artifacts",
]
