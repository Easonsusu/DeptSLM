"""Administrator-only Phase 12.1E-A artifact reconciliation.

The operation is deliberately narrower than purge.  It can abandon stale
source attempts and remove only non-authoritative source/registry stages and
failed terminal registry/source finals.  It never changes an adapter or source
to a purge state and never releases an upstream dependency.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_maintenance_artifacts import (
    AdapterMaintenanceArtifactError,
    AdapterMaintenanceArtifactStore,
    BoundSurface,
)
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    Adapter,
    AdapterArtifactOperation,
    AdapterArtifactOperationItem,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterRegistryAttempt,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction

ADAPTER_ARTIFACT_ADMIN_ROLES = frozenset(
    {DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN}
)
SURFACE_TYPES = ("source_stage", "source_final", "registry_stage", "registry_final")
PROTECTED_SOURCE_STATUSES = frozenset(
    {"committed", "claimed", "consumed", "purge_pending", "purged"}
)
PROTECTED_ADAPTER_STATUSES = frozenset({"validated", "purge_pending", "purged"})
TERMINAL_SOURCE_ATTEMPT_STATUSES = frozenset({"failed", "abandoned"})
TERMINAL_REGISTRY_ATTEMPT_STATUSES = frozenset({"validation_failed", "failed", "reclaimed"})


class AdapterArtifactMaintenanceConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdapterArtifactMaintenanceSettings:
    database_url: str
    data_dir: Path
    minimum_age_seconds: int

    @classmethod
    def from_environment(cls) -> AdapterArtifactMaintenanceSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        raw_age = os.getenv("DEPTSLM_ADAPTER_RECONCILIATION_MIN_AGE_SECONDS", "3600").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise AdapterArtifactMaintenanceConfigurationError("Database configuration is invalid.")
        if not raw_age.isascii() or not raw_age.isdecimal() or not 300 <= int(raw_age) <= 86400:
            raise AdapterArtifactMaintenanceConfigurationError(
                "DEPTSLM_ADAPTER_RECONCILIATION_MIN_AGE_SECONDS must be between 300 and 86400."
            )
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise AdapterArtifactMaintenanceConfigurationError("Adapter storage is unavailable.")
        adapters = data_dir / "adapters"
        required = (
            adapters,
            adapters / "imports",
            adapters / "registry",
            adapters / ".staging",
            adapters / ".staging" / "imports",
            adapters / ".staging" / "registry",
            adapters / ".deleting",
            *(adapters / ".deleting" / surface for surface in SURFACE_TYPES),
        )
        if any(not path.is_dir() for path in required):
            raise AdapterArtifactMaintenanceConfigurationError("Adapter storage is unavailable.")
        try:
            for path in required:
                if path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
                    raise AdapterArtifactMaintenanceConfigurationError(
                        "Adapter storage is unavailable."
                    )
        except OSError as error:
            raise AdapterArtifactMaintenanceConfigurationError(
                "Adapter storage is unavailable."
            ) from error
        return cls(database_url, data_dir, int(raw_age))


@dataclass(frozen=True, slots=True)
class AdapterArtifactMaintenanceResult:
    eligible_count: int
    completed_count: int
    blocked_count: int
    surface_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Candidate:
    surface_type: str
    department_id: UUID
    source_bundle_id: UUID | None
    adapter_id: UUID | None
    import_attempt_id: UUID | None
    registry_attempt_id: UUID | None
    publication_attempt_id: UUID
    attempt_number: int
    expected_resource_version: int
    expected_attempt_version: int
    ownership_manifest: dict[str, object] | None


def _limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ServiceError(422, "Invalid adapter reconciliation limit")


def _age(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 300 <= value <= 86400:
        raise ServiceError(422, "Invalid adapter reconciliation minimum age")


def reconcile_adapter_artifacts(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int = 100,
    minimum_age_seconds: int = 3600,
    apply: bool = False,
) -> AdapterArtifactMaintenanceResult:
    """Run one bounded dry-run or crash-resumable reconciliation operation."""

    _limit(limit)
    _age(minimum_age_seconds)
    candidates, operation_id = _register_or_resume(
        factory,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
        limit=limit,
        minimum_age_seconds=minimum_age_seconds,
        apply=apply,
    )
    counts = {
        surface: sum(item.surface_type == surface for item in candidates)
        for surface in SURFACE_TYPES
    }
    if not apply or operation_id is None:
        return AdapterArtifactMaintenanceResult(len(candidates), 0, 0, counts)
    completed = blocked = 0
    for candidate in candidates:
        try:
            if _execute_item(
                factory,
                data_dir=data_dir,
                department_id=department_id,
                operation_id=operation_id,
                candidate=candidate,
                actor_issuer=actor_issuer,
                actor_subject=actor_subject,
            ):
                completed += 1
            else:
                blocked += 1
        except ServiceError:
            raise
    _finalize_operation(
        factory,
        operation_id=operation_id,
        department_id=department_id,
        actor_issuer=actor_issuer,
        actor_subject=actor_subject,
    )
    return AdapterArtifactMaintenanceResult(len(candidates), completed, blocked, counts)


def _authorize(session: Session, department_id: UUID, issuer: str, subject: str, *, lock: bool):
    return authorize_transaction(
        session,
        AuthenticatedPrincipal(subject, issuer),
        DepartmentRequestScope(DepartmentScope(department_id)),
        ADAPTER_ARTIFACT_ADMIN_ROLES,
        lock=lock,
        audit_action="adapter.artifact.reconcile.authorization",
    )


def _register_or_resume(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int,
    minimum_age_seconds: int,
    apply: bool,
) -> tuple[tuple[_Candidate, ...], UUID | None]:
    try:
        with factory.begin() as session:
            authorization = _authorize(
                session, department_id, actor_issuer, actor_subject, lock=apply
            )
            existing = session.execute(
                select(AdapterArtifactOperation)
                .where(
                    AdapterArtifactOperation.department_id == department_id,
                    AdapterArtifactOperation.operation_type == "reconcile",
                    AdapterArtifactOperation.status == "registered",
                )
                .order_by(AdapterArtifactOperation.created_at, AdapterArtifactOperation.id)
                .with_for_update()
                .limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                items = session.scalars(
                    select(AdapterArtifactOperationItem)
                    .where(
                        AdapterArtifactOperationItem.operation_id == existing.id,
                        AdapterArtifactOperationItem.department_id == department_id,
                        AdapterArtifactOperationItem.status.in_(
                            ("registered", "verified", "tombstone_bound", "deleting")
                        ),
                    )
                    .order_by(
                        AdapterArtifactOperationItem.created_at, AdapterArtifactOperationItem.id
                    )
                ).all()
                return tuple(
                    _candidate_from_item(item) for item in items
                ), existing.id if apply else None
            candidates = _select_candidates(session, department_id, minimum_age_seconds, limit)
            if not apply or not candidates:
                return candidates, None
            operation = AdapterArtifactOperation(
                id=uuid4(),
                department_id=department_id,
                requested_by_user_id=authorization.identity.id,
                operation_type="reconcile",
                status="registered",
                limit_value=limit,
                minimum_age_seconds=minimum_age_seconds,
                eligible_count=len(candidates),
                completed_count=0,
                blocked_count=0,
                version=1,
            )
            session.add(operation)
            session.flush()
            for candidate in candidates:
                candidate = _abandon_stale_source_if_needed(session, candidate)
                session.add(
                    AdapterArtifactOperationItem(
                        id=uuid4(),
                        operation_id=operation.id,
                        department_id=department_id,
                        surface_type=candidate.surface_type,
                        source_bundle_id=candidate.source_bundle_id,
                        adapter_id=candidate.adapter_id,
                        import_attempt_id=candidate.import_attempt_id,
                        registry_attempt_id=candidate.registry_attempt_id,
                        publication_attempt_id=candidate.publication_attempt_id,
                        attempt_number=candidate.attempt_number,
                        expected_resource_version=candidate.expected_resource_version,
                        expected_attempt_version=candidate.expected_attempt_version,
                        ownership_manifest=candidate.ownership_manifest,
                        status="registered",
                        version=1,
                    )
                )
            session.flush()
            return candidates, operation.id
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _select_candidates(
    session: Session, department_id: UUID, minimum_age_seconds: int, limit: int
) -> tuple[_Candidate, ...]:
    cutoff = session.scalar(select(func.clock_timestamp()))
    if cutoff is None:
        raise ServiceError(503, "Database unavailable")
    cutoff = cutoff - timedelta(seconds=minimum_age_seconds)
    result: list[_Candidate] = []
    # Terminal source attempts are safe to inspect even when their source is
    # abandoned/rejected; protected source states are explicitly excluded.
    source_rows = session.execute(
        select(AdapterImportAttempt, AdapterImportSource)
        .join(
            AdapterImportSource,
            (AdapterImportSource.id == AdapterImportAttempt.source_bundle_id)
            & (AdapterImportSource.department_id == AdapterImportAttempt.department_id),
        )
        .where(
            AdapterImportAttempt.department_id == department_id,
            AdapterImportAttempt.cleanup_confirmed_at.is_(None),
            AdapterImportAttempt.status.in_(
                tuple(TERMINAL_SOURCE_ATTEMPT_STATUSES)
                + ("registered", "validated", "staged", "published")
            ),
            AdapterImportSource.status.not_in(tuple(PROTECTED_SOURCE_STATUSES)),
            (
                (AdapterImportAttempt.status.in_(tuple(TERMINAL_SOURCE_ATTEMPT_STATUSES)))
                | (AdapterImportAttempt.created_at <= cutoff)
            ),
        )
        .order_by(AdapterImportAttempt.created_at, AdapterImportAttempt.id)
        .with_for_update(skip_locked=True)
        .limit(limit),
    ).all()
    for attempt, source in source_rows:
        stale = attempt.status not in TERMINAL_SOURCE_ATTEMPT_STATUSES
        if stale and (source.status != "staging" or source.authoritative_attempt_id is not None):
            continue
        result.append(
            _Candidate(
                "source_stage",
                department_id,
                source.id,
                None,
                attempt.id,
                None,
                attempt.publication_attempt_id,
                attempt.attempt_number,
                source.version,
                attempt.version,
                None,
            )
        )
        if (
            isinstance(attempt.ownership_manifest, dict)
            and source.status not in PROTECTED_SOURCE_STATUSES
        ):
            result.append(
                _Candidate(
                    "source_final",
                    department_id,
                    source.id,
                    None,
                    attempt.id,
                    None,
                    attempt.publication_attempt_id,
                    attempt.attempt_number,
                    source.version,
                    attempt.version,
                    dict(attempt.ownership_manifest),
                )
            )
        if len(result) >= limit:
            return tuple(result[:limit])
    registry_rows = session.execute(
        select(AdapterRegistryAttempt, Adapter)
        .join(
            Adapter,
            (Adapter.id == AdapterRegistryAttempt.adapter_id)
            & (Adapter.department_id == AdapterRegistryAttempt.department_id),
        )
        .where(
            AdapterRegistryAttempt.department_id == department_id,
            AdapterRegistryAttempt.cleanup_confirmed_at.is_(None),
            AdapterRegistryAttempt.status.in_(tuple(TERMINAL_REGISTRY_ATTEMPT_STATUSES)),
            Adapter.status.in_(("failed", "validation_failed")),
        )
        .order_by(AdapterRegistryAttempt.created_at, AdapterRegistryAttempt.id)
        .with_for_update(skip_locked=True)
        .limit(max(0, limit - len(result))),
    ).all()
    for attempt, adapter in registry_rows:
        result.append(
            _Candidate(
                "registry_stage",
                department_id,
                None,
                adapter.id,
                None,
                attempt.id,
                attempt.publication_attempt_id,
                attempt.attempt_number,
                adapter.version,
                attempt.version,
                None,
            )
        )
        if isinstance(attempt.ownership_manifest, dict) and adapter.status in {
            "failed",
            "validation_failed",
        }:
            result.append(
                _Candidate(
                    "registry_final",
                    department_id,
                    None,
                    adapter.id,
                    None,
                    attempt.id,
                    attempt.publication_attempt_id,
                    attempt.attempt_number,
                    adapter.version,
                    attempt.version,
                    dict(attempt.ownership_manifest),
                )
            )
        if len(result) >= limit:
            break
    return tuple(result[:limit])


def _abandon_stale_source_if_needed(session: Session, candidate: _Candidate) -> _Candidate:
    if candidate.import_attempt_id is None or candidate.source_bundle_id is None:
        return candidate
    attempt = session.execute(
        select(AdapterImportAttempt)
        .where(
            AdapterImportAttempt.id == candidate.import_attempt_id,
            AdapterImportAttempt.department_id == candidate.department_id,
            AdapterImportAttempt.source_bundle_id == candidate.source_bundle_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    source = session.execute(
        select(AdapterImportSource)
        .where(
            AdapterImportSource.id == candidate.source_bundle_id,
            AdapterImportSource.department_id == candidate.department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if attempt is None or source is None:
        raise ServiceError(409, "Adapter reconciliation authority changed")
    if attempt.status in TERMINAL_SOURCE_ATTEMPT_STATUSES:
        return _Candidate(
            candidate.surface_type,
            candidate.department_id,
            candidate.source_bundle_id,
            candidate.adapter_id,
            candidate.import_attempt_id,
            candidate.registry_attempt_id,
            candidate.publication_attempt_id,
            candidate.attempt_number,
            source.version,
            attempt.version,
            candidate.ownership_manifest,
        )
    if (
        attempt.status not in {"registered", "validated", "staged", "published"}
        or source.status != "staging"
        or source.authoritative_attempt_id is not None
    ):
        raise ServiceError(409, "Adapter reconciliation authority changed")
    now = session.scalar(select(func.clock_timestamp()))
    attempt.status = "abandoned"
    attempt.error_code = "adapter_source_publication_failed"
    attempt.finished_at = now
    attempt.version += 1
    source.status = "abandoned"
    source.error_code = "adapter_source_publication_failed"
    source.abandoned_at = now
    source.version += 1
    # The abandonment is part of the same short authority transaction as
    # registration.  Persist the post-transition versions in the item rather
    # than carrying the stale pre-transition snapshot into execution.
    return _Candidate(
        candidate.surface_type,
        candidate.department_id,
        candidate.source_bundle_id,
        candidate.adapter_id,
        candidate.import_attempt_id,
        candidate.registry_attempt_id,
        candidate.publication_attempt_id,
        candidate.attempt_number,
        source.version,
        attempt.version,
        candidate.ownership_manifest,
    )


def _candidate_from_item(item: AdapterArtifactOperationItem) -> _Candidate:
    return _Candidate(
        item.surface_type,
        item.department_id,
        item.source_bundle_id,
        item.adapter_id,
        item.import_attempt_id,
        item.registry_attempt_id,
        item.publication_attempt_id,
        item.attempt_number,
        item.expected_resource_version,
        item.expected_attempt_version,
        dict(item.ownership_manifest) if isinstance(item.ownership_manifest, dict) else None,
    )


def _load_item(
    session: Session, operation_id: UUID, candidate: _Candidate, issuer: str, subject: str
) -> AdapterArtifactOperationItem:
    authorization = _authorize(session, candidate.department_id, issuer, subject, lock=True)
    operation = session.execute(
        select(AdapterArtifactOperation)
        .where(
            AdapterArtifactOperation.id == operation_id,
            AdapterArtifactOperation.department_id == candidate.department_id,
            AdapterArtifactOperation.status == "registered",
        )
        .with_for_update()
    ).scalar_one_or_none()
    item = session.execute(
        select(AdapterArtifactOperationItem)
        .where(
            AdapterArtifactOperationItem.operation_id == operation_id,
            AdapterArtifactOperationItem.department_id == candidate.department_id,
            AdapterArtifactOperationItem.surface_type == candidate.surface_type,
            AdapterArtifactOperationItem.publication_attempt_id == candidate.publication_attempt_id,
            AdapterArtifactOperationItem.status.in_(
                ("registered", "verified", "tombstone_bound", "deleting")
            ),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if operation is None or item is None:
        raise ServiceError(409, "Adapter reconciliation operation is unavailable")
    _check_authority(session, item)
    del authorization
    return item


def _check_authority(session: Session, item: AdapterArtifactOperationItem) -> None:
    if item.surface_type.startswith("source_"):
        source = session.execute(
            select(AdapterImportSource)
            .where(
                AdapterImportSource.id == item.source_bundle_id,
                AdapterImportSource.department_id == item.department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        attempt = session.execute(
            select(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.id == item.import_attempt_id,
                AdapterImportAttempt.department_id == item.department_id,
                AdapterImportAttempt.source_bundle_id == item.source_bundle_id,
                AdapterImportAttempt.publication_attempt_id == item.publication_attempt_id,
                AdapterImportAttempt.attempt_number == item.attempt_number,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if (
            source is None
            or attempt is None
            or source.version != item.expected_resource_version
            or attempt.version != item.expected_attempt_version
        ):
            raise ServiceError(409, "Adapter reconciliation authority changed")
        if (
            source.status in PROTECTED_SOURCE_STATUSES
            or attempt.status not in TERMINAL_SOURCE_ATTEMPT_STATUSES
        ):
            raise ServiceError(409, "Adapter reconciliation authority changed")
        if item.surface_type == "source_final" and not isinstance(item.ownership_manifest, dict):
            raise ServiceError(409, "Adapter reconciliation authority changed")
        return
    adapter = session.execute(
        select(Adapter)
        .where(
            Adapter.id == item.adapter_id,
            Adapter.department_id == item.department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    attempt = session.execute(
        select(AdapterRegistryAttempt)
        .where(
            AdapterRegistryAttempt.id == item.registry_attempt_id,
            AdapterRegistryAttempt.department_id == item.department_id,
            AdapterRegistryAttempt.adapter_id == item.adapter_id,
            AdapterRegistryAttempt.publication_attempt_id == item.publication_attempt_id,
            AdapterRegistryAttempt.attempt_number == item.attempt_number,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if (
        adapter is None
        or attempt is None
        or adapter.version != item.expected_resource_version
        or attempt.version != item.expected_attempt_version
    ):
        raise ServiceError(409, "Adapter reconciliation authority changed")
    if (
        attempt.status not in TERMINAL_REGISTRY_ATTEMPT_STATUSES
        or adapter.status in PROTECTED_ADAPTER_STATUSES
    ):
        raise ServiceError(409, "Adapter reconciliation authority changed")
    if item.surface_type == "registry_final" and not isinstance(item.ownership_manifest, dict):
        raise ServiceError(409, "Adapter reconciliation authority changed")


def _bound_from_item(item: AdapterArtifactOperationItem) -> BoundSurface:
    if (
        not isinstance(item.observed_identity, dict)
        or not isinstance(item.deletion_plan, list)
        or not isinstance(item.tombstone_identity, dict)
    ):
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    return BoundSurface(
        item.surface_type,
        item.department_id,
        item.source_bundle_id or item.adapter_id,  # type: ignore[arg-type]
        item.import_attempt_id or item.registry_attempt_id,  # type: ignore[arg-type]
        item.id,
        dict(item.observed_identity),
        list(item.deletion_plan),
        dict(item.tombstone_identity),
    )


def _execute_item(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    operation_id: UUID,
    candidate: _Candidate,
    actor_issuer: str,
    actor_subject: str,
) -> bool:
    try:
        with factory.begin() as session:
            item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
            item_id = item.id
            if item.status in {"tombstone_bound", "deleting"}:
                bound = _bound_from_item(item)
            else:
                bound = None
        with AdapterMaintenanceArtifactStore(data_dir) as store:
            if bound is None:
                resource_id = candidate.source_bundle_id or candidate.adapter_id
                path_attempt_id = (
                    candidate.import_attempt_id
                    if candidate.surface_type.startswith("source_")
                    else candidate.publication_attempt_id
                )
                if resource_id is None or path_attempt_id is None:
                    raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
                bound = store.bind_tombstone(
                    candidate.surface_type,
                    department_id,
                    resource_id,
                    path_attempt_id,
                    item_id,
                    expected_manifest=candidate.ownership_manifest,
                )
                if bound is None:
                    _mark_completed(factory, operation_id, candidate, actor_issuer, actor_subject)
                    return True
                with factory.begin() as session:
                    item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
                    item.observed_identity = bound.observed_identity
                    item.deletion_plan = bound.deletion_plan
                    item.tombstone_identity = bound.tombstone_identity
                    item.status = "tombstone_bound"
                    item.tombstone_bound_at = session.scalar(select(func.clock_timestamp()))
                    item.version += 1
            with factory.begin() as session:
                item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
                start_index = item.next_entry_index
            for index, entry in enumerate(bound.deletion_plan[start_index:], start=start_index):
                name = str(entry["name"])
                with factory.begin() as session:
                    item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
                    if item.status == "completed":
                        return True
                    resumed_unlink = False
                    if item.in_flight_entry is not None:
                        if (
                            not isinstance(item.in_flight_entry, dict)
                            or item.in_flight_entry.get("name") != name
                        ):
                            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
                        if item.next_entry_index != index:
                            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
                        resumed_unlink = True
                    elif item.next_entry_index != index:
                        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
                    item.status = "deleting"
                    item.deletion_started_at = item.deletion_started_at or session.scalar(
                        select(func.clock_timestamp())
                    )
                    item.in_flight_entry = {"name": name}
                    item.version += 1
                try:
                    store.unlink_tombstone_entry(bound, name, allow_missing=resumed_unlink)
                except AdapterMaintenanceArtifactError:
                    raise
                with factory.begin() as session:
                    item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
                    item.next_entry_index += 1
                    item.in_flight_entry = None
                    item.version += 1
            with factory.begin() as session:
                item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
                already_started = item.directory_unlink_started_at is not None
                item.directory_unlink_started_at = session.scalar(select(func.clock_timestamp()))
                item.version += 1
            store.remove_tombstone_directory(bound, allow_missing=already_started)
            _mark_completed(factory, operation_id, candidate, actor_issuer, actor_subject)
            return True
    except AdapterMaintenanceArtifactError as error:
        _mark_blocked(factory, operation_id, candidate, actor_issuer, actor_subject, error.code)
        return False
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _mark_completed(
    factory: sessionmaker[Session],
    operation_id: UUID,
    candidate: _Candidate,
    issuer: str,
    subject: str,
) -> None:
    with factory.begin() as session:
        item = _load_item(session, operation_id, candidate, issuer, subject)
        item.status = "completed"
        item.completed_at = session.scalar(select(func.clock_timestamp()))
        item.in_flight_entry = None
        item.version += 1
        _confirm_attempt_cleanup(session, item)


def _mark_blocked(
    factory: sessionmaker[Session],
    operation_id: UUID,
    candidate: _Candidate,
    issuer: str,
    subject: str,
    reason: str,
) -> None:
    with factory.begin() as session:
        item = _load_item(session, operation_id, candidate, issuer, subject)
        item.status = "blocked"
        item.blocked_at = session.scalar(select(func.clock_timestamp()))
        item.blocked_reason_code = reason
        item.version += 1


def _confirm_attempt_cleanup(session: Session, item: AdapterArtifactOperationItem) -> None:
    siblings = session.scalars(
        select(AdapterArtifactOperationItem)
        .where(
            AdapterArtifactOperationItem.operation_id == item.operation_id,
            AdapterArtifactOperationItem.department_id == item.department_id,
            AdapterArtifactOperationItem.publication_attempt_id == item.publication_attempt_id,
        )
        .with_for_update()
    ).all()
    if not siblings or any(sibling.status != "completed" for sibling in siblings):
        return
    now = session.scalar(select(func.clock_timestamp()))
    if item.import_attempt_id is not None:
        attempt = session.execute(
            select(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.id == item.import_attempt_id,
                AdapterImportAttempt.department_id == item.department_id,
                AdapterImportAttempt.source_bundle_id == item.source_bundle_id,
                AdapterImportAttempt.publication_attempt_id == item.publication_attempt_id,
                AdapterImportAttempt.attempt_number == item.attempt_number,
            )
            .with_for_update()
        ).scalar_one_or_none()
    else:
        attempt = session.execute(
            select(AdapterRegistryAttempt)
            .where(
                AdapterRegistryAttempt.id == item.registry_attempt_id,
                AdapterRegistryAttempt.department_id == item.department_id,
                AdapterRegistryAttempt.adapter_id == item.adapter_id,
                AdapterRegistryAttempt.publication_attempt_id == item.publication_attempt_id,
                AdapterRegistryAttempt.attempt_number == item.attempt_number,
            )
            .with_for_update()
        ).scalar_one_or_none()
    if (
        attempt is not None
        and attempt.cleanup_confirmed_at is None
        and attempt.status
        in (TERMINAL_SOURCE_ATTEMPT_STATUSES | TERMINAL_REGISTRY_ATTEMPT_STATUSES)
    ):
        attempt.cleanup_confirmed_at = now
        attempt.version += 1


def _finalize_operation(
    factory: sessionmaker[Session],
    *,
    operation_id: UUID,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
) -> None:
    with factory.begin() as session:
        authorization = _authorize(session, department_id, actor_issuer, actor_subject, lock=True)
        operation = session.execute(
            select(AdapterArtifactOperation)
            .where(
                AdapterArtifactOperation.id == operation_id,
                AdapterArtifactOperation.department_id == department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if operation is None or operation.status != "registered":
            return
        items = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.operation_id == operation_id,
                AdapterArtifactOperationItem.department_id == department_id,
            )
            .with_for_update()
        ).all()
        if any(
            item.status in {"registered", "verified", "tombstone_bound", "deleting"}
            for item in items
        ):
            return
        operation.completed_count = sum(item.status == "completed" for item in items)
        operation.blocked_count = sum(item.status == "blocked" for item in items)
        operation.status = "completed_with_blocks" if operation.blocked_count else "completed"
        operation.completed_at = session.scalar(select(func.clock_timestamp()))
        operation.version += 1
        scope = DepartmentRequestScope(DepartmentScope(department_id))
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=actor_subject,
            request_scope=scope,
            action="adapter.artifact.reconcile",
            resource_type="adapter_artifact_operation",
            resource_id=operation.id,
        )


__all__ = [
    "ADAPTER_ARTIFACT_ADMIN_ROLES",
    "AdapterArtifactMaintenanceConfigurationError",
    "AdapterArtifactMaintenanceResult",
    "AdapterArtifactMaintenanceSettings",
    "reconcile_adapter_artifacts",
]
