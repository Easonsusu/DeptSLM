"""Narrow Phase 12.1E-C release of one completed adapter retention fence.

The command in this module is deliberately metadata-only.  It independently
revalidates a completed Phase 12.1E-B purge and reads the two exact adapter
storage namespaces without changing them before releasing one exact upstream
dependency.  PostgreSQL remains the lifecycle authority; filesystem and
database observations are not transactionally atomic.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_maintenance_artifacts import (
    AdapterMaintenanceArtifactError,
    AdapterPurgeArtifactStore,
    physical_surface_identifier,
)
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    Adapter,
    AdapterArtifactOperationItem,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterPurgeItem,
    AdapterPurgeOperation,
    AdapterPurgeReservation,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    PersistentAuditEvent,
    SftDatasetBuild,
    TrainingJob,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction

ADAPTER_LIFECYCLE_RELEASE_ADMIN_ROLES = frozenset(
    {DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN}
)
_ACTIVE_PURGE_STATUSES = ("registered", "deleting")
_ACTIVE_RECONCILIATION_ITEM_STATUSES = ("registered", "verified", "tombstone_bound", "deleting")
_REQUIRED_SURFACES = ("registry_final", "source_final")


class AdapterLifecycleReleaseConfigurationError(RuntimeError):
    """Raised when the isolated E-C command environment is not usable."""


class _ReleaseConflict(RuntimeError):
    """A content-free lifecycle or read-only storage authority conflict."""


class _ReleaseStorageUnavailable(RuntimeError):
    """The exact external adapters root cannot be inspected safely."""


@dataclass(frozen=True, slots=True)
class AdapterLifecycleReleaseSettings:
    """Minimal settings for metadata release plus read-only storage inspection."""

    database_url: str
    data_dir: Path

    @classmethod
    def from_environment(cls) -> AdapterLifecycleReleaseSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise AdapterLifecycleReleaseConfigurationError("Database configuration is invalid.")
        if not raw_data_dir:
            raise AdapterLifecycleReleaseConfigurationError("DEPTSLM_DATA_DIR is required.")
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute():
            raise AdapterLifecycleReleaseConfigurationError("Adapter storage is unavailable.")
        required = (
            data_dir,
            data_dir / "adapters",
            data_dir / "adapters" / "imports",
            data_dir / "adapters" / "registry",
            data_dir / "adapters" / ".staging",
            data_dir / "adapters" / ".staging" / "imports",
            data_dir / "adapters" / ".staging" / "registry",
            data_dir / "adapters" / ".purge-deleting",
            data_dir / "adapters" / ".purge-deleting" / "source_stage",
            data_dir / "adapters" / ".purge-deleting" / "source_final",
            data_dir / "adapters" / ".purge-deleting" / "registry_stage",
            data_dir / "adapters" / ".purge-deleting" / "registry_final",
        )
        try:
            if any(not _private_directory(path) for path in required):
                raise AdapterLifecycleReleaseConfigurationError("Adapter storage is unavailable.")
        except OSError as error:
            raise AdapterLifecycleReleaseConfigurationError(
                "Adapter storage is unavailable."
            ) from error
        return cls(database_url=database_url, data_dir=data_dir)


@dataclass(frozen=True, slots=True)
class AdapterLifecycleReleaseResult:
    """Closed, content-free result for an E-C validation or release."""

    adapter_id: UUID
    applied: bool
    already_released: bool
    adapter_version: int
    dependency_version: int


@dataclass(frozen=True, slots=True)
class _ReleaseAuthority:
    adapter: Adapter
    source: AdapterImportSource
    source_attempt: AdapterImportAttempt
    registry_attempt: AdapterRegistryAttempt
    dependency: AdapterUpstreamDependency
    training_job: TrainingJob
    dataset_build: SftDatasetBuild
    purge_operation: AdapterPurgeOperation | None
    reservations: tuple[AdapterPurgeReservation, ...]
    items: tuple[AdapterPurgeItem, ...]


@dataclass(frozen=True, slots=True)
class _StorageScope:
    department_id: UUID
    adapter_id: UUID
    source_bundle_id: UUID


def _private_directory(path: Path) -> bool:
    metadata = path.lstat()
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _safe_uuid(value: object, *, label: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ServiceError(422, f"Invalid {label} selector")
    return value


def _safe_version(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ServiceError(422, f"Invalid expected {label} version")
    return value


def _expected_purge_snapshot(
    operation: AdapterPurgeOperation,
    authority: _ReleaseAuthority,
    *,
    pending: bool,
) -> dict[str, object]:
    """Reconstruct the closed E-B registration snapshot from live immutable rows.

    E-B retains one registration snapshot on the operation and one pending
    snapshot on each reservation. Reconstructing both closed states catches
    altered manifests, hashes, identifiers, versions, or state instead of
    accepting a merely matching subset of fields.
    """

    adapter = authority.adapter
    source = authority.source
    source_attempt = authority.source_attempt
    registry_attempt = authority.registry_attempt
    return {
        "adapter": {
            "id": str(adapter.id),
            "department_id": str(adapter.department_id),
            "version": operation.expected_adapter_version - (0 if pending else 1),
            "status": "purge_pending" if pending else "validated",
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
            "version": operation.expected_source_version - (0 if pending else 1),
            "status": "purge_pending" if pending else "consumed",
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
            "version": operation.expected_source_attempt_version,
            "status": "committed",
        },
        "registry_attempt": {
            "id": str(registry_attempt.id),
            "department_id": str(registry_attempt.department_id),
            "adapter_id": str(registry_attempt.adapter_id),
            "publication_attempt_id": str(registry_attempt.publication_attempt_id),
            "attempt_number": registry_attempt.attempt_number,
            "version": operation.expected_registry_attempt_version,
            "status": "succeeded",
        },
    }


def _snapshot_matches(
    snapshot: object,
    operation: AdapterPurgeOperation,
    authority: _ReleaseAuthority,
    *,
    pending: bool,
) -> bool:
    return isinstance(snapshot, dict) and snapshot == _expected_purge_snapshot(
        operation, authority, pending=pending
    )


def _load_authority(
    session: Session,
    *,
    department_id: UUID,
    adapter_id: UUID,
    expected_adapter_version: int,
    expected_source_version: int,
    expected_dependency_version: int,
) -> _ReleaseAuthority:
    """Lock and prove one exact E-B completion and retention dependency."""

    operations = session.scalars(
        select(AdapterPurgeOperation)
        .where(
            AdapterPurgeOperation.department_id == department_id,
            AdapterPurgeOperation.adapter_id == adapter_id,
        )
        .order_by(AdapterPurgeOperation.created_at, AdapterPurgeOperation.id)
        .with_for_update()
    ).all()
    if any(operation.status in _ACTIVE_PURGE_STATUSES for operation in operations):
        raise _ReleaseConflict

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
            AdapterRegistryAttempt.department_id == department_id,
            AdapterRegistryAttempt.adapter_id == adapter.id,
            AdapterRegistryAttempt.execution_scope_id == adapter.execution_scope_id,
            AdapterRegistryAttempt.publication_attempt_id == adapter.publication_attempt_id,
            AdapterRegistryAttempt.attempt_number == adapter.attempt_number,
        )
        .with_for_update()
    ).scalar_one_or_none()
    training_job = session.execute(
        select(TrainingJob)
        .where(
            TrainingJob.id == adapter.training_job_id,
            TrainingJob.department_id == department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    dataset_build = session.execute(
        select(SftDatasetBuild)
        .where(
            SftDatasetBuild.id == adapter.dataset_build_id,
            SftDatasetBuild.department_id == department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    # Lock upstream resource rows before their dependency. The established
    # Phase 10/11 maintenance paths take the same upstream-before-dependency
    # order, so lifecycle release cannot invert their retention-fence locks.
    dependencies = session.scalars(
        select(AdapterUpstreamDependency)
        .where(
            AdapterUpstreamDependency.department_id == department_id,
            AdapterUpstreamDependency.adapter_id == adapter.id,
        )
        .order_by(AdapterUpstreamDependency.id)
        .with_for_update()
    ).all()
    if (
        source is None
        or source_attempt is None
        or registry_attempt is None
        or training_job is None
        or dataset_build is None
        or len(dependencies) != 1
    ):
        raise _ReleaseConflict
    dependency = dependencies[0]
    provisional = _ReleaseAuthority(
        adapter,
        source,
        source_attempt,
        registry_attempt,
        dependency,
        training_job,
        dataset_build,
        None,
        (),
        (),
    )
    _assert_lifecycle_and_lineage(
        provisional,
        department_id=department_id,
        expected_adapter_version=expected_adapter_version,
        expected_source_version=expected_source_version,
        expected_dependency_version=expected_dependency_version,
    )
    operation = _successful_operation(operations, provisional)
    reservations = tuple(
        session.scalars(
            select(AdapterPurgeReservation)
            .where(
                AdapterPurgeReservation.operation_id == operation.id,
                AdapterPurgeReservation.department_id == department_id,
            )
            .order_by(AdapterPurgeReservation.surface_type, AdapterPurgeReservation.id)
            .with_for_update()
        ).all()
    )
    items = tuple(
        session.scalars(
            select(AdapterPurgeItem)
            .where(
                AdapterPurgeItem.operation_id == operation.id,
                AdapterPurgeItem.department_id == department_id,
            )
            .order_by(AdapterPurgeItem.surface_type, AdapterPurgeItem.id)
            .with_for_update()
        ).all()
    )
    authority = _ReleaseAuthority(
        adapter,
        source,
        source_attempt,
        registry_attempt,
        dependency,
        training_job,
        dataset_build,
        operation,
        reservations,
        items,
    )
    _assert_completed_purge(authority)
    purge_audits = session.scalars(
        select(PersistentAuditEvent)
        .where(
            PersistentAuditEvent.department_id == department_id,
            PersistentAuditEvent.action == "adapter.purge",
            PersistentAuditEvent.resource_type == "adapter_purge_operation",
            PersistentAuditEvent.resource_id == str(operation.id),
            PersistentAuditEvent.result == "allowed",
            PersistentAuditEvent.reason_code == "mutation_applied",
        )
        .order_by(PersistentAuditEvent.created_at, PersistentAuditEvent.id)
        .with_for_update()
    ).all()
    if len(purge_audits) != 1:
        raise _ReleaseConflict
    _assert_no_active_reconciliation(session, authority, department_id)
    return authority


def _assert_lifecycle_and_lineage(
    authority: _ReleaseAuthority,
    *,
    department_id: UUID,
    expected_adapter_version: int,
    expected_source_version: int,
    expected_dependency_version: int,
) -> None:
    adapter = authority.adapter
    source = authority.source
    dependency = authority.dependency
    training_job = authority.training_job
    dataset_build = authority.dataset_build
    if (
        adapter.department_id != department_id
        or source.department_id != department_id
        or authority.source_attempt.department_id != department_id
        or authority.registry_attempt.department_id != department_id
        or dependency.department_id != department_id
        or training_job.department_id != department_id
        or dataset_build.department_id != department_id
        or adapter.status != "purged"
        or adapter.purged_at is None
        or adapter.worker_id is not None
        or adapter.claim_token is not None
        or adapter.lease_expires_at is not None
        or adapter.verified_governance_lineage is not True
        or adapter.verified_artifact_compatibility is not True
        or source.status != "purged"
        or source.purged_at is None
        or source.claimed_adapter_id != adapter.id
        or source.authoritative_attempt_id != adapter.source_authoritative_attempt_id
        or authority.source_attempt.status != "committed"
        or authority.registry_attempt.status != "succeeded"
        or dependency.adapter_id != adapter.id
        or dependency.training_job_id != adapter.training_job_id
        or dependency.dataset_build_id != adapter.dataset_build_id
        or dependency.status not in {"active", "released"}
        or (dependency.status == "active") != (dependency.released_at is None)
        or training_job.id != dependency.training_job_id
        or dataset_build.id != dependency.dataset_build_id
        or training_job.dataset_build_id != dataset_build.id
        or adapter.training_job_id != training_job.id
        or adapter.dataset_build_id != dataset_build.id
        or adapter.version != expected_adapter_version
        or source.version != expected_source_version
        or dependency.version != expected_dependency_version
    ):
        raise _ReleaseConflict


def _successful_operation(
    operations: list[AdapterPurgeOperation], authority: _ReleaseAuthority
) -> AdapterPurgeOperation:
    matches = [
        operation
        for operation in operations
        if operation.status == "completed"
        and operation.source_bundle_id == authority.source.id
        and operation.source_authoritative_attempt_id == authority.source_attempt.id
        and operation.source_publication_attempt_id
        == authority.source_attempt.publication_attempt_id
        and operation.source_attempt_number == authority.source_attempt.attempt_number
        and operation.registry_attempt_id == authority.registry_attempt.id
        and operation.registry_publication_attempt_id
        == authority.registry_attempt.publication_attempt_id
        and operation.registry_attempt_number == authority.registry_attempt.attempt_number
        and operation.completed_at is not None
        and operation.success_audited_at is not None
        and operation.eligible_item_count == 2
        and operation.completed_item_count == 2
        and operation.blocked_item_count == 0
        and _snapshot_matches(operation.authority_snapshot, operation, authority, pending=False)
    ]
    if len(matches) != 1:
        raise _ReleaseConflict
    return matches[0]


def _assert_completed_purge(authority: _ReleaseAuthority) -> None:
    operation = authority.purge_operation
    if operation is None or len(authority.reservations) != 2 or len(authority.items) != 2:
        raise _ReleaseConflict
    if (
        authority.source.version != operation.expected_source_version + 1
        or authority.source_attempt.version != operation.expected_source_attempt_version
        or authority.registry_attempt.version != operation.expected_registry_attempt_version
        or authority.adapter.version
        != operation.expected_adapter_version
        + (2 if authority.dependency.status == "released" else 1)
    ):
        raise _ReleaseConflict
    reservations = {reservation.surface_type: reservation for reservation in authority.reservations}
    if set(reservations) != set(_REQUIRED_SURFACES):
        raise _ReleaseConflict
    source_reservation = reservations["source_final"]
    registry_reservation = reservations["registry_final"]
    expected = (
        (
            source_reservation,
            authority.source_attempt.id,
            authority.source_attempt.publication_attempt_id,
            authority.source_attempt.attempt_number,
            "source_final",
            authority.source_attempt.ownership_manifest,
        ),
        (
            registry_reservation,
            authority.registry_attempt.id,
            authority.registry_attempt.publication_attempt_id,
            authority.registry_attempt.attempt_number,
            "registry_final",
            authority.registry_attempt.ownership_manifest,
        ),
    )
    for reservation, attempt_id, publication_id, attempt_number, surface, manifest in expected:
        if (
            reservation.operation_id != operation.id
            or reservation.adapter_id != authority.adapter.id
            or reservation.source_bundle_id != authority.source.id
            or reservation.status != "completed"
            or reservation.completed_at is None
            or reservation.blocked_at is not None
            or reservation.blocked_reason_code is not None
            or reservation.publication_attempt_id != publication_id
            or reservation.attempt_number != attempt_number
            or reservation.expected_resource_version
            != (
                operation.expected_source_version
                if surface == "source_final"
                else operation.expected_adapter_version
            )
            or reservation.expected_attempt_version
            != (
                operation.expected_source_attempt_version
                if surface == "source_final"
                else operation.expected_registry_attempt_version
            )
            or reservation.expected_resource_status
            != ("consumed" if surface == "source_final" else "validated")
            or reservation.expected_attempt_status
            != ("committed" if surface == "source_final" else "succeeded")
            or not isinstance(reservation.authority_manifest, dict)
            or not isinstance(reservation.authority_snapshot, dict)
            or not isinstance(reservation.expected_tombstone_namespace, dict)
            or not isinstance(reservation.observed_identity, dict)
            or not isinstance(reservation.tombstone_identity, dict)
            or not isinstance(reservation.deletion_plan, list)
            or not isinstance(manifest, dict)
            or reservation.authority_manifest != manifest
            or not _snapshot_matches(
                reservation.authority_snapshot, operation, authority, pending=True
            )
        ):
            raise _ReleaseConflict
        if surface == "source_final" and (
            reservation.import_attempt_id != attempt_id
            or reservation.registry_attempt_id is not None
        ):
            raise _ReleaseConflict
        if surface == "registry_final" and (
            reservation.registry_attempt_id != attempt_id
            or reservation.import_attempt_id is not None
        ):
            raise _ReleaseConflict
    items = {item.reservation_id: item for item in authority.items}
    if set(items) != {reservation.id for reservation in authority.reservations}:
        raise _ReleaseConflict
    for reservation in authority.reservations:
        item = items[reservation.id]
        resource_id = (
            authority.source.id
            if reservation.surface_type == "source_final"
            else authority.adapter.id
        )
        expected_tombstone_namespace = {
            "surface_type": reservation.surface_type,
            "department_id": str(authority.adapter.department_id),
            "resource_id": str(resource_id),
            "item_id": str(item.id),
        }
        if (
            item.operation_id != operation.id
            or item.department_id != authority.adapter.department_id
            or item.status != "completed"
            or item.completed_at is None
            or item.blocked_at is not None
            or item.blocked_reason_code is not None
            or item.surface_type != reservation.surface_type
            or item.adapter_id != authority.adapter.id
            or item.source_bundle_id != authority.source.id
            or item.publication_attempt_id != reservation.publication_attempt_id
            or item.attempt_number != reservation.attempt_number
            or item.ownership_manifest != reservation.authority_manifest
            or item.observed_identity != reservation.observed_identity
            or reservation.expected_tombstone_namespace != expected_tombstone_namespace
            or item.expected_tombstone_namespace != expected_tombstone_namespace
            or item.expected_tombstone_namespace != reservation.expected_tombstone_namespace
            or item.tombstone_identity != reservation.tombstone_identity
            or item.deletion_plan != reservation.deletion_plan
        ):
            raise _ReleaseConflict
        if reservation.surface_type == "source_final" and (
            item.import_attempt_id != reservation.import_attempt_id
            or item.registry_attempt_id is not None
        ):
            raise _ReleaseConflict
        if reservation.surface_type == "registry_final" and (
            item.registry_attempt_id != reservation.registry_attempt_id
            or item.import_attempt_id is not None
        ):
            raise _ReleaseConflict


def _assert_no_active_reconciliation(
    session: Session, authority: _ReleaseAuthority, department_id: UUID
) -> None:
    active = session.scalars(
        select(AdapterArtifactOperationItem)
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.status.in_(_ACTIVE_RECONCILIATION_ITEM_STATUSES),
            (
                (AdapterArtifactOperationItem.surface_type == "source_final")
                & (AdapterArtifactOperationItem.source_bundle_id == authority.source.id)
            )
            | (
                (AdapterArtifactOperationItem.surface_type == "registry_final")
                & (AdapterArtifactOperationItem.adapter_id == authority.adapter.id)
            ),
        )
        .order_by(AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id)
        .with_for_update()
    ).all()
    if active:
        raise _ReleaseConflict


def _storage_scope(authority: _ReleaseAuthority) -> _StorageScope:
    return _StorageScope(
        department_id=authority.adapter.department_id,
        adapter_id=authority.adapter.id,
        source_bundle_id=authority.source.id,
    )


def _assert_purged_storage_absent(data_dir: Path, scope: _StorageScope) -> None:
    """Read only the two exact final paths and E-B resource namespaces."""

    addresses = (
        physical_surface_identifier("registry_final", scope.department_id, scope.adapter_id, None),
        physical_surface_identifier(
            "source_final", scope.department_id, scope.source_bundle_id, None
        ),
    )
    try:
        with AdapterPurgeArtifactStore(data_dir) as store:
            for address in addresses:
                if store.surface_exists(address):
                    raise _ReleaseConflict
                store.assert_tombstone_namespace_empty(address)
    except _ReleaseConflict:
        raise
    except AdapterMaintenanceArtifactError as error:
        if error.code == "artifact_permissions_invalid":
            raise _ReleaseStorageUnavailable from error
        raise _ReleaseConflict from error
    except OSError as error:
        raise _ReleaseStorageUnavailable from error


def _authorize_and_load(
    session: Session,
    *,
    department_id: UUID,
    adapter_id: UUID,
    expected_adapter_version: int,
    expected_source_version: int,
    expected_dependency_version: int,
    actor_issuer: str,
    actor_subject: str,
):
    principal = AuthenticatedPrincipal(subject=actor_subject, issuer=actor_issuer)
    scope = DepartmentRequestScope(DepartmentScope(department_id))
    # Verify access before reading target rows without holding the department
    # lock while waiting for E-B's operation lock. E-B finalization locks its
    # operation first, so the final locked authorization below preserves a
    # consistent order without exposing a foreign adapter to an unauthorised
    # caller.
    authorize_transaction(
        session,
        principal,
        scope,
        ADAPTER_LIFECYCLE_RELEASE_ADMIN_ROLES,
        lock=False,
        audit_action=None,
    )
    authority = _load_authority(
        session,
        department_id=department_id,
        adapter_id=adapter_id,
        expected_adapter_version=expected_adapter_version,
        expected_source_version=expected_source_version,
        expected_dependency_version=expected_dependency_version,
    )
    authorization = authorize_transaction(
        session,
        principal,
        scope,
        ADAPTER_LIFECYCLE_RELEASE_ADMIN_ROLES,
        lock=True,
        audit_action=None,
    )
    # Re-run the complete locked proof after the department fence is held. This
    # closes the window in which E-A/E-B could register a new exact mutation
    # item after the preliminary operation scan.
    authority = _load_authority(
        session,
        department_id=department_id,
        adapter_id=adapter_id,
        expected_adapter_version=expected_adapter_version,
        expected_source_version=expected_source_version,
        expected_dependency_version=expected_dependency_version,
    )
    return authorization, scope, authority


def _result(authority: _ReleaseAuthority, *, applied: bool) -> AdapterLifecycleReleaseResult:
    return AdapterLifecycleReleaseResult(
        adapter_id=authority.adapter.id,
        applied=applied,
        already_released=not applied and authority.dependency.status == "released",
        adapter_version=authority.adapter.version,
        dependency_version=authority.dependency.version,
    )


def release_adapter_upstream_dependency(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    adapter_id: UUID,
    expected_adapter_version: int,
    expected_source_version: int,
    expected_dependency_version: int,
    actor_issuer: str,
    actor_subject: str,
    apply: bool = False,
) -> AdapterLifecycleReleaseResult:
    """Validate or release exactly one active E-C upstream retention fence."""

    department_id = _safe_uuid(department_id, label="department")
    adapter_id = _safe_uuid(adapter_id, label="adapter")
    expected_adapter_version = _safe_version(expected_adapter_version, label="adapter")
    expected_source_version = _safe_version(expected_source_version, label="source")
    expected_dependency_version = _safe_version(expected_dependency_version, label="dependency")
    try:
        with factory.begin() as session:
            _authorization, _scope, authority = _authorize_and_load(
                session,
                department_id=department_id,
                adapter_id=adapter_id,
                expected_adapter_version=expected_adapter_version,
                expected_source_version=expected_source_version,
                expected_dependency_version=expected_dependency_version,
                actor_issuer=actor_issuer,
                actor_subject=actor_subject,
            )
            storage_scope = _storage_scope(authority)
            dry_result = _result(authority, applied=False)
        _assert_purged_storage_absent(data_dir, storage_scope)
        if not apply:
            return dry_result
        with factory.begin() as session:
            authorization, scope, authority = _authorize_and_load(
                session,
                department_id=department_id,
                adapter_id=adapter_id,
                expected_adapter_version=expected_adapter_version,
                expected_source_version=expected_source_version,
                expected_dependency_version=expected_dependency_version,
                actor_issuer=actor_issuer,
                actor_subject=actor_subject,
            )
            _assert_purged_storage_absent(data_dir, _storage_scope(authority))
            if authority.dependency.status == "released":
                return _result(authority, applied=False)
            released_at = session.scalar(select(func.clock_timestamp()))
            authority.dependency.status = "released"
            authority.dependency.released_at = released_at
            authority.dependency.version += 1
            authority.adapter.version += 1
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=actor_subject,
                request_scope=scope,
                action="adapter.upstream_dependency.release",
                resource_type="adapter",
                resource_id=authority.adapter.id,
            )
            return _result(authority, applied=True)
    except ServiceError:
        raise
    except _ReleaseConflict as error:
        raise ServiceError(409, "Adapter lifecycle release authority changed") from error
    except _ReleaseStorageUnavailable as error:
        raise ServiceError(503, "Adapter storage unavailable") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


__all__ = [
    "ADAPTER_LIFECYCLE_RELEASE_ADMIN_ROLES",
    "AdapterLifecycleReleaseConfigurationError",
    "AdapterLifecycleReleaseResult",
    "AdapterLifecycleReleaseSettings",
    "release_adapter_upstream_dependency",
]
