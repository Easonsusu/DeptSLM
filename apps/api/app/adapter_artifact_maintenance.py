"""Administrator-only Phase 12.1E-A artifact reconciliation.

The operation is deliberately narrower than purge.  It can abandon stale
source attempts and remove only non-authoritative source/registry stages and
failed terminal registry/source finals.  It never changes an adapter or source
to a purge state and never releases an upstream dependency.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.adapter_maintenance_artifacts import (
    AdapterMaintenanceArtifactError,
    AdapterMaintenanceArtifactStore,
    BoundSurface,
    InspectedSurface,
    PhysicalSurfaceIdentifier,
    physical_surface_identifier,
)
from app.adapter_registry_domain import (
    AdapterRegistryDomainError,
    parse_registry_manifest,
)
from app.adapter_registry_domain import (
    canonical_json_bytes as registry_canonical_json_bytes,
)
from app.adapter_source_artifacts import (
    AdapterSourceArtifactError,
    parse_source_manifest,
)
from app.adapter_source_artifacts import (
    canonical_manifest_bytes as source_canonical_manifest_bytes,
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
    PersistentAuditEvent,
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
ACTIVE_ITEM_STATUSES = frozenset({"registered", "verified", "tombstone_bound", "deleting"})
# A stage item with this closed, content-free marker is a confirmation-only
# retry.  It is never a final-manifest authority and therefore cannot trigger
# filesystem deletion or be confused with a persisted final manifest.
_CONFIRMATION_ONLY_MARKER = {"phase12_1e_a_confirmation_only": True}
_CONFIRMATION_ONLY_MARKER_KEY = "phase12_1e_a_confirmation_only"
# Candidate selection has two bounded phases.  Each source/registry lane may
# inspect at most this many rows using indexed SQL predicates and grouped
# history.  The final ``FOR UPDATE SKIP LOCKED`` query locks only the selected
# attempt/resource rows (at most ``limit`` distinct attempts); the preselection
# rows are never locked.  This lets untried work outside an old blocked prefix
# enter the next bounded window without widening the lock footprint.
RECONCILIATION_SCAN_MULTIPLIER = 8
RECONCILIATION_MAX_SCAN_ROWS = 1000


class AdapterArtifactMaintenanceConfigurationError(RuntimeError):
    pass


def _confirmation_marker_clause(column):
    """Match the closed confirmation marker without JSON equality.

    PostgreSQL's ``json`` type deliberately has no equality operator.  The
    marker is a boolean field in a closed, content-free object, so use the
    JSON scalar extraction operator instead of binding the whole dictionary.
    """

    return column[_CONFIRMATION_ONLY_MARKER_KEY].as_boolean().is_(True)


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
    expected_item_version: int = 1
    confirmation_only: bool = False


@dataclass(frozen=True, slots=True)
class _SurfaceHistory:
    statuses: frozenset[str]
    blocked_count: int
    latest_blocked_at: datetime | None
    confirmation_blocked_count: int = 0
    latest_confirmation_blocked_at: datetime | None = None


def _candidate_surface_address(candidate: _Candidate) -> PhysicalSurfaceIdentifier:
    """Resolve the one physical surface address used by every storage call."""

    resource_id = candidate.source_bundle_id or candidate.adapter_id
    if resource_id is None:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    path_attempt_id = None
    if candidate.surface_type == "source_stage":
        path_attempt_id = candidate.import_attempt_id
    elif candidate.surface_type == "registry_stage":
        path_attempt_id = candidate.publication_attempt_id
    return physical_surface_identifier(
        candidate.surface_type,
        candidate.department_id,
        resource_id,
        path_attempt_id,
    )


def _limit(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ServiceError(422, "Invalid adapter reconciliation limit")


def _age(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 300 <= value <= 86400:
        raise ServiceError(422, "Invalid adapter reconciliation minimum age")


def _persisted_manifest_authority(surface_type: str, ownership_manifest: object) -> tuple[str, int]:
    """Derive exact final-manifest bytes from the closed persisted attempt."""

    if not isinstance(ownership_manifest, dict):
        raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
    try:
        if surface_type == "source_final":
            raw = source_canonical_manifest_bytes(ownership_manifest)
            parse_source_manifest(raw)
        elif surface_type == "registry_final":
            raw = registry_canonical_json_bytes(ownership_manifest)
            parse_registry_manifest(raw)
        else:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    except AdapterMaintenanceArtifactError:
        raise
    except (AdapterRegistryDomainError, AdapterSourceArtifactError, TypeError, ValueError):
        raise AdapterMaintenanceArtifactError("artifact_manifest_invalid") from None
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _surface_item_history_for_rows(
    session: Session,
    *,
    department_id: UUID,
    surface_type: str,
    resource_column,
    attempt_column,
    rows: list[tuple[object, object]],
) -> dict[tuple[UUID, UUID], _SurfaceHistory]:
    """Aggregate exact status history for a bounded set of candidates."""

    keys = {
        (resource_id, attempt_id)
        for resource_id, attempt_id in rows
        if resource_id is not None and attempt_id is not None
    }
    if not keys:
        return {}
    key_filter = or_(
        *(
            and_(resource_column == resource_id, attempt_column == attempt_id)
            for resource_id, attempt_id in keys
        )
    )
    grouped = session.execute(
        select(
            resource_column,
            attempt_column,
            AdapterArtifactOperationItem.status,
            func.count(AdapterArtifactOperationItem.id),
            func.max(AdapterArtifactOperationItem.created_at),
        )
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == surface_type,
            key_filter,
        )
        .group_by(resource_column, attempt_column, AdapterArtifactOperationItem.status)
    ).all()
    history: dict[tuple[UUID, UUID], dict[str, object]] = {}
    for resource_id, attempt_id, status, count, latest_at in grouped:
        entry = history.setdefault(
            (resource_id, attempt_id),
            {"statuses": set(), "blocked_count": 0, "latest_blocked_at": None},
        )
        statuses = entry["statuses"]
        assert isinstance(statuses, set)
        statuses.add(status)
        if status == "blocked":
            entry["blocked_count"] = int(entry["blocked_count"]) + int(count)
            previous = entry["latest_blocked_at"]
            if previous is None or (latest_at is not None and latest_at > previous):
                entry["latest_blocked_at"] = latest_at
    confirmation_grouped = session.execute(
        select(
            resource_column,
            attempt_column,
            func.count(AdapterArtifactOperationItem.id),
            func.max(AdapterArtifactOperationItem.created_at),
        )
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == surface_type,
            AdapterArtifactOperationItem.status == "blocked",
            AdapterArtifactOperationItem.blocked_reason_code == "artifact_authority_changed",
            _confirmation_marker_clause(AdapterArtifactOperationItem.ownership_manifest),
            key_filter,
        )
        .group_by(resource_column, attempt_column)
    ).all()
    for resource_id, attempt_id, count, latest_at in confirmation_grouped:
        entry = history.setdefault(
            (resource_id, attempt_id),
            {"statuses": set(), "blocked_count": 0, "latest_blocked_at": None},
        )
        entry["blocked_count"] = int(entry["blocked_count"]) - int(count)
        entry["confirmation_blocked_count"] = int(count)
        entry["latest_confirmation_blocked_at"] = latest_at
    return {
        key: _SurfaceHistory(
            frozenset(value["statuses"]),
            int(value["blocked_count"]),
            value["latest_blocked_at"],
            int(value.get("confirmation_blocked_count", 0)),
            value.get("latest_confirmation_blocked_at"),
        )
        for key, value in history.items()
    }


def _surface_item_state(
    history: dict[tuple[UUID, UUID], _SurfaceHistory],
    resource_id: UUID,
    attempt_id: UUID,
) -> str:
    """Classify one exact surface without mutating historical rows."""

    record = history.get((resource_id, attempt_id))
    statuses = record.statuses if record is not None else frozenset()
    if not statuses:
        return "untried"
    if "completed" in statuses:
        return "completed"
    if statuses & ACTIVE_ITEM_STATUSES:
        return "active"
    if statuses == frozenset({"blocked"}):
        return "blocked"
    # Unknown mixtures are treated conservatively as active/unsafe.  The
    # historical row remains immutable and a later explicit review can retry
    # it only after the operation is no longer active.
    return "active"


def _surface_is_fully_completed(
    history: dict[tuple[UUID, UUID], _SurfaceHistory],
    resource_id: UUID,
    attempt_id: UUID,
) -> bool:
    """Return whether an exact surface has completed work and no live row."""

    record = history.get((resource_id, attempt_id))
    if record is None or "completed" not in record.statuses:
        return False
    return not record.statuses.intersection(ACTIVE_ITEM_STATUSES)


def _prioritize_final_siblings(
    rows: list[tuple[object, object]],
    *,
    resource_id_for,
    attempt_id_for,
    final_history: dict[tuple[UUID, UUID], _SurfaceHistory],
    valid_final_keys: set[tuple[UUID, UUID]],
) -> tuple[tuple[object, object], ...]:
    """Prefer untried finals, then rotate blocked generations fairly."""

    def rank(row: tuple[object, object]) -> tuple[object, ...]:
        resource_id = resource_id_for(row)
        attempt_id = attempt_id_for(row)
        record = final_history.get((resource_id, attempt_id))
        state = _surface_item_state(final_history, resource_id, attempt_id)
        if state == "untried":
            return (0, 0, 0.0, str(attempt_id))
        if state == "blocked" and record is not None:
            latest = (
                record.latest_blocked_at.timestamp()
                if record.latest_blocked_at is not None
                else 0.0
            )
            # Lowest retry count wins. Equal counts prefer the newest blocked
            # generation so a repaired newer sibling can win immediately.
            return (1, record.blocked_count, -latest, str(attempt_id))
        return (2, 0, 0.0, str(attempt_id))

    grouped: dict[UUID, list[tuple[object, object]]] = {}
    for row in rows:
        grouped.setdefault(resource_id_for(row), []).append(row)
    ordered: list[tuple[object, object]] = []
    for group in grouped.values():
        valid = [
            row for row in group if (resource_id_for(row), attempt_id_for(row)) in valid_final_keys
        ]
        valid.sort(key=rank)
        ordered.extend(valid)
        ordered.extend(row for row in group if row not in valid)
    return tuple(ordered)


def _bounded_scan_limit(limit: int) -> int:
    return min(RECONCILIATION_MAX_SCAN_ROWS, limit * RECONCILIATION_SCAN_MULTIPLIER)


def _row_fairness_key(
    row: tuple[object, object],
    *,
    resource_id_for,
    attempt_id_for,
    stage_history: dict[tuple[UUID, UUID], _SurfaceHistory],
    final_history: dict[tuple[UUID, UUID], _SurfaceHistory],
    final_applicable_keys: set[tuple[UUID, UUID]],
) -> tuple[object, ...]:
    """Order actionable surfaces before confirmation-only retry backlog."""

    resource_id = resource_id_for(row)
    attempt_id = attempt_id_for(row)

    def state_and_record(
        history: dict[tuple[UUID, UUID], _SurfaceHistory],
    ) -> tuple[str, _SurfaceHistory | None]:
        record = history.get((resource_id, attempt_id))
        state = _surface_item_state(history, resource_id, attempt_id)
        return state, record

    stage_state, stage_record = state_and_record(stage_history)
    if (resource_id, attempt_id) in final_applicable_keys:
        final_state, final_record = state_and_record(final_history)
    else:
        # A missing ownership manifest means the final surface is not part of
        # this attempt's cleanup contract.  It must not be treated as an
        # untried final and thereby make a blocked stage look fresh forever.
        final_state, final_record = "not_applicable", None
    if stage_state == "untried" or final_state == "untried":
        lane_rank = (0, 0, 0.0)
    elif final_state == "blocked" and final_record is not None:
        latest = (
            final_record.latest_blocked_at.timestamp()
            if final_record.latest_blocked_at is not None
            else 0.0
        )
        lane_rank = (1, final_record.blocked_count, latest)
    elif stage_state == "blocked" and stage_record is not None:
        latest = (
            stage_record.latest_blocked_at.timestamp()
            if stage_record.latest_blocked_at is not None
            else 0.0
        )
        lane_rank = (1, stage_record.blocked_count, latest)
    elif (
        stage_state == "completed"
        and stage_record is not None
        and (
            final_state in {"not_applicable", "completed"}
            and stage_record.confirmation_blocked_count > 0
        )
    ):
        # A blocked stage generation paired with a completed/no-final surface
        # is the durable signal for a fenced confirmation-only retry.  Keep it
        # below actionable final/stage work while still rotating by count/time.
        latest = (
            stage_record.latest_confirmation_blocked_at.timestamp()
            if stage_record.latest_confirmation_blocked_at is not None
            else 0.0
        )
        lane_rank = (2, stage_record.confirmation_blocked_count, latest)
    elif stage_state == "completed" and final_state in {"not_applicable", "completed"}:
        # The first confirmation check has no blocked history yet.  It is
        # still lower priority than an actionable physical surface.
        lane_rank = (2, 0, 0.0)
    else:
        lane_rank = (3, 0, 0.0)

    created_at = getattr(row[0], "created_at", None)
    created_key = created_at.timestamp() if created_at is not None else 0.0
    return (*lane_rank, created_key, str(attempt_id))


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
    selected_attempts = 0
    selected_source_finals: set[UUID] = set()
    selected_registry_finals: set[UUID] = set()
    scan_limit = _bounded_scan_limit(limit)
    source_sibling = aliased(AdapterImportAttempt)
    source_stage_seen = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_stage",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .exists()
    )
    source_stage_blocked_count = (
        select(func.count(AdapterArtifactOperationItem.id))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_stage",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
            ~and_(
                AdapterArtifactOperationItem.blocked_reason_code == "artifact_authority_changed",
                _confirmation_marker_clause(AdapterArtifactOperationItem.ownership_manifest),
            ),
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .scalar_subquery()
    )
    source_stage_confirmation_blocked_count = (
        select(func.count(AdapterArtifactOperationItem.id))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_stage",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
            AdapterArtifactOperationItem.blocked_reason_code == "artifact_authority_changed",
            _confirmation_marker_clause(AdapterArtifactOperationItem.ownership_manifest),
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .scalar_subquery()
    )
    source_final_blocked_count = (
        select(func.count(AdapterArtifactOperationItem.id))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_final",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .scalar_subquery()
    )
    source_stage_latest_blocked = (
        select(func.max(AdapterArtifactOperationItem.created_at))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_stage",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .scalar_subquery()
    )
    source_final_latest_blocked = (
        select(func.max(AdapterArtifactOperationItem.created_at))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_final",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .scalar_subquery()
    )
    source_stage_completed = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_stage",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "completed",
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .exists()
    )
    source_stage_active = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_stage",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status.in_(tuple(ACTIVE_ITEM_STATUSES)),
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .exists()
    )
    source_final_completed = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_final",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "completed",
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .exists()
    )
    source_final_active = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_final",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status.in_(tuple(ACTIVE_ITEM_STATUSES)),
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .exists()
    )
    source_invalid_final_quarantine = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "source_final",
            AdapterArtifactOperationItem.source_bundle_id == AdapterImportSource.id,
            AdapterArtifactOperationItem.import_attempt_id == AdapterImportAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
            AdapterArtifactOperationItem.blocked_reason_code == "artifact_manifest_invalid",
            AdapterArtifactOperationItem.expected_attempt_version == AdapterImportAttempt.version,
        )
        .correlate(AdapterImportAttempt, AdapterImportSource)
        .exists()
    )
    source_confirmation_ready = and_(
        source_stage_completed,
        ~source_stage_active,
        or_(
            AdapterImportAttempt.ownership_manifest.is_(None),
            and_(source_final_completed, ~source_final_active),
        ),
    )
    # SQL cannot be the canonical final-manifest parser.  Only an unseen
    # stage receives the high preselection rank; final applicability is
    # validated below.  Once a malformed final has produced a durable blocked
    # item for this exact attempt version, exclude that unchanged quarantine
    # from the bounded scan.  An administrator repair increments the attempt
    # version and makes the row eligible again without erasing history.
    source_has_untried = ~source_stage_seen
    source_action_priority = case(
        (source_has_untried, 0),
        (source_confirmation_ready, 2),
        else_=1,
    )
    source_blocked_rank = case(
        (
            source_stage_completed,
            source_final_blocked_count + (2 * source_stage_confirmation_blocked_count),
        ),
        else_=source_stage_blocked_count,
    )
    source_latest_blocked_rank = case(
        (source_stage_completed, source_final_latest_blocked),
        else_=source_stage_latest_blocked,
    )
    # Terminal source attempts are safe to inspect even when their source is
    # abandoned/rejected; protected source states and ineligible siblings are
    # excluded before the bounded scan limit is applied.
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
            AdapterImportSource.authoritative_attempt_id.is_(None),
            or_(
                AdapterImportAttempt.status.in_(tuple(TERMINAL_SOURCE_ATTEMPT_STATUSES)),
                and_(
                    AdapterImportAttempt.status.not_in(tuple(TERMINAL_SOURCE_ATTEMPT_STATUSES)),
                    AdapterImportSource.status == "staging",
                    AdapterImportSource.authoritative_attempt_id.is_(None),
                    AdapterImportAttempt.created_at <= cutoff,
                ),
            ),
            ~select(literal(1))
            .where(
                source_sibling.department_id == AdapterImportAttempt.department_id,
                source_sibling.source_bundle_id == AdapterImportAttempt.source_bundle_id,
                source_sibling.id != AdapterImportAttempt.id,
                source_sibling.status == "committed",
            )
            .correlate(AdapterImportAttempt)
            .exists(),
            ~select(literal(1))
            .where(
                source_sibling.department_id == AdapterImportAttempt.department_id,
                source_sibling.source_bundle_id == AdapterImportAttempt.source_bundle_id,
                source_sibling.id != AdapterImportAttempt.id,
                ~source_sibling.status.in_(tuple(TERMINAL_SOURCE_ATTEMPT_STATUSES)),
            )
            .correlate(AdapterImportAttempt)
            .exists(),
            ~and_(source_stage_completed, source_invalid_final_quarantine),
        )
        .order_by(
            source_action_priority,
            source_blocked_rank,
            source_latest_blocked_rank.asc().nullsfirst(),
            AdapterImportAttempt.created_at,
            AdapterImportAttempt.id,
        )
        .limit(scan_limit)
    ).all()
    source_ids = {source.id for _attempt, source in source_rows}
    committed_source_ids = (
        set(
            session.scalars(
                select(AdapterImportAttempt.source_bundle_id).where(
                    AdapterImportAttempt.department_id == department_id,
                    AdapterImportAttempt.source_bundle_id.in_(source_ids),
                    AdapterImportAttempt.status == "committed",
                )
            ).all()
        )
        if source_ids
        else set()
    )
    eligible_source_rows: list[tuple[AdapterImportAttempt, AdapterImportSource]] = []
    source_final_manifests: dict[tuple[UUID, UUID], dict[str, object]] = {}
    source_invalid_final_keys: set[tuple[UUID, UUID]] = set()
    for attempt, source in source_rows:
        if source.id in committed_source_ids:
            continue
        stale = attempt.status not in TERMINAL_SOURCE_ATTEMPT_STATUSES
        if stale and (source.status != "staging" or source.authoritative_attempt_id is not None):
            continue
        eligible_source_rows.append((attempt, source))
        if (
            attempt.ownership_manifest is not None
            and source.status not in PROTECTED_SOURCE_STATUSES
        ):
            try:
                _persisted_manifest_authority("source_final", attempt.ownership_manifest)
            except AdapterMaintenanceArtifactError:
                source_invalid_final_keys.add((source.id, attempt.id))
                continue
            if isinstance(attempt.ownership_manifest, dict):
                source_final_manifests[(source.id, attempt.id)] = dict(attempt.ownership_manifest)
    source_key_rows = [(source.id, attempt.id) for attempt, source in eligible_source_rows]
    source_stage_history = _surface_item_history_for_rows(
        session,
        department_id=department_id,
        surface_type="source_stage",
        resource_column=AdapterArtifactOperationItem.source_bundle_id,
        attempt_column=AdapterArtifactOperationItem.import_attempt_id,
        rows=source_key_rows,
    )
    source_final_history = _surface_item_history_for_rows(
        session,
        department_id=department_id,
        surface_type="source_final",
        resource_column=AdapterArtifactOperationItem.source_bundle_id,
        attempt_column=AdapterArtifactOperationItem.import_attempt_id,
        rows=source_key_rows,
    )
    ordered_source_rows = _prioritize_final_siblings(
        eligible_source_rows,
        resource_id_for=lambda row: row[1].id,
        attempt_id_for=lambda row: row[0].id,
        final_history=source_final_history,
        valid_final_keys=set(source_final_manifests),
    )
    ordered_source_rows = tuple(
        sorted(
            ordered_source_rows,
            key=lambda row: _row_fairness_key(
                row,
                resource_id_for=lambda value: value[1].id,
                attempt_id_for=lambda value: value[0].id,
                stage_history=source_stage_history,
                final_history=source_final_history,
                final_applicable_keys=set(source_final_manifests),
            ),
        )
    )
    source_final_untried_by_resource: dict[UUID, bool] = {}
    for resource_id, attempt_id in source_final_manifests:
        source_final_untried_by_resource[resource_id] = (
            source_final_untried_by_resource.get(resource_id, False)
            or _surface_item_state(source_final_history, resource_id, attempt_id) == "untried"
        )
    # Source and registry rows are merged only after each bounded SQL lane has
    # applied its eligibility predicates.  This is the cross-family fairness
    # boundary: a persistent source retry cannot consume every operation while
    # an untried registry attempt is available.
    registry_scan_limit = _bounded_scan_limit(limit)
    registry_sibling = aliased(AdapterRegistryAttempt)
    registry_stage_seen = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_stage",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .exists()
    )
    registry_stage_blocked_count = (
        select(func.count(AdapterArtifactOperationItem.id))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_stage",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
            ~and_(
                AdapterArtifactOperationItem.blocked_reason_code == "artifact_authority_changed",
                _confirmation_marker_clause(AdapterArtifactOperationItem.ownership_manifest),
            ),
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .scalar_subquery()
    )
    registry_stage_confirmation_blocked_count = (
        select(func.count(AdapterArtifactOperationItem.id))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_stage",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
            AdapterArtifactOperationItem.blocked_reason_code == "artifact_authority_changed",
            _confirmation_marker_clause(AdapterArtifactOperationItem.ownership_manifest),
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .scalar_subquery()
    )
    registry_final_blocked_count = (
        select(func.count(AdapterArtifactOperationItem.id))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_final",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .scalar_subquery()
    )
    registry_stage_latest_blocked = (
        select(func.max(AdapterArtifactOperationItem.created_at))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_stage",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .scalar_subquery()
    )
    registry_final_latest_blocked = (
        select(func.max(AdapterArtifactOperationItem.created_at))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_final",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .scalar_subquery()
    )
    registry_stage_completed = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_stage",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "completed",
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .exists()
    )
    registry_stage_active = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_stage",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status.in_(tuple(ACTIVE_ITEM_STATUSES)),
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .exists()
    )
    registry_final_completed = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_final",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "completed",
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .exists()
    )
    registry_final_active = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_final",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status.in_(tuple(ACTIVE_ITEM_STATUSES)),
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .exists()
    )
    registry_invalid_final_quarantine = (
        select(literal(1))
        .where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == "registry_final",
            AdapterArtifactOperationItem.adapter_id == Adapter.id,
            AdapterArtifactOperationItem.registry_attempt_id == AdapterRegistryAttempt.id,
            AdapterArtifactOperationItem.status == "blocked",
            AdapterArtifactOperationItem.blocked_reason_code == "artifact_manifest_invalid",
            AdapterArtifactOperationItem.expected_attempt_version == AdapterRegistryAttempt.version,
        )
        .correlate(AdapterRegistryAttempt, Adapter)
        .exists()
    )
    registry_confirmation_ready = and_(
        registry_stage_completed,
        ~registry_stage_active,
        or_(
            AdapterRegistryAttempt.ownership_manifest.is_(None),
            and_(registry_final_completed, ~registry_final_active),
        ),
    )
    # See the source lane above: a JSON non-null check is not final authority.
    registry_has_untried = ~registry_stage_seen
    registry_action_priority = case(
        (registry_has_untried, 0),
        (registry_confirmation_ready, 2),
        else_=1,
    )
    registry_blocked_rank = case(
        (
            registry_stage_completed,
            registry_final_blocked_count + (2 * registry_stage_confirmation_blocked_count),
        ),
        else_=registry_stage_blocked_count,
    )
    registry_latest_blocked_rank = case(
        (registry_stage_completed, registry_final_latest_blocked),
        else_=registry_stage_latest_blocked,
    )
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
            ~select(literal(1))
            .where(
                registry_sibling.department_id == AdapterRegistryAttempt.department_id,
                registry_sibling.adapter_id == AdapterRegistryAttempt.adapter_id,
                registry_sibling.id != AdapterRegistryAttempt.id,
                registry_sibling.status.in_(
                    ("succeeded", "registered", "running", "staged", "published")
                ),
            )
            .correlate(AdapterRegistryAttempt)
            .exists(),
            ~and_(registry_stage_completed, registry_invalid_final_quarantine),
        )
        .order_by(
            registry_action_priority,
            registry_blocked_rank,
            registry_latest_blocked_rank.asc().nullsfirst(),
            AdapterRegistryAttempt.created_at,
            AdapterRegistryAttempt.id,
        )
        .limit(registry_scan_limit)
    ).all()
    registry_ids = {adapter.id for _attempt, adapter in registry_rows}
    registry_sibling_statuses = (
        session.execute(
            select(AdapterRegistryAttempt.adapter_id, AdapterRegistryAttempt.status).where(
                AdapterRegistryAttempt.department_id == department_id,
                AdapterRegistryAttempt.adapter_id.in_(registry_ids),
                AdapterRegistryAttempt.status.in_(
                    ("succeeded", "registered", "running", "staged", "published")
                ),
            )
        ).all()
        if registry_ids
        else []
    )
    succeeded_registry_ids = {
        adapter_id for adapter_id, status in registry_sibling_statuses if status == "succeeded"
    }
    active_registry_ids = {
        adapter_id for adapter_id, status in registry_sibling_statuses if status != "succeeded"
    }
    eligible_registry_rows: list[tuple[AdapterRegistryAttempt, Adapter]] = []
    registry_final_manifests: dict[tuple[UUID, UUID], dict[str, object]] = {}
    registry_invalid_final_keys: set[tuple[UUID, UUID]] = set()
    for attempt, adapter in registry_rows:
        if adapter.id in succeeded_registry_ids or adapter.id in active_registry_ids:
            continue
        eligible_registry_rows.append((attempt, adapter))
        if attempt.ownership_manifest is not None and adapter.status in {
            "failed",
            "validation_failed",
        }:
            try:
                _persisted_manifest_authority("registry_final", attempt.ownership_manifest)
            except AdapterMaintenanceArtifactError:
                registry_invalid_final_keys.add((adapter.id, attempt.id))
                continue
            if isinstance(attempt.ownership_manifest, dict):
                registry_final_manifests[(adapter.id, attempt.id)] = dict(
                    attempt.ownership_manifest
                )
    registry_key_rows = [(adapter.id, attempt.id) for attempt, adapter in eligible_registry_rows]
    registry_stage_history = _surface_item_history_for_rows(
        session,
        department_id=department_id,
        surface_type="registry_stage",
        resource_column=AdapterArtifactOperationItem.adapter_id,
        attempt_column=AdapterArtifactOperationItem.registry_attempt_id,
        rows=registry_key_rows,
    )
    registry_final_history = _surface_item_history_for_rows(
        session,
        department_id=department_id,
        surface_type="registry_final",
        resource_column=AdapterArtifactOperationItem.adapter_id,
        attempt_column=AdapterArtifactOperationItem.registry_attempt_id,
        rows=registry_key_rows,
    )
    ordered_registry_rows = _prioritize_final_siblings(
        eligible_registry_rows,
        resource_id_for=lambda row: row[1].id,
        attempt_id_for=lambda row: row[0].id,
        final_history=registry_final_history,
        valid_final_keys=set(registry_final_manifests),
    )
    ordered_registry_rows = tuple(
        sorted(
            ordered_registry_rows,
            key=lambda row: _row_fairness_key(
                row,
                resource_id_for=lambda value: value[1].id,
                attempt_id_for=lambda value: value[0].id,
                stage_history=registry_stage_history,
                final_history=registry_final_history,
                final_applicable_keys=set(registry_final_manifests),
            ),
        )
    )
    registry_final_untried_by_resource: dict[UUID, bool] = {}
    for resource_id, attempt_id in registry_final_manifests:
        registry_final_untried_by_resource[resource_id] = (
            registry_final_untried_by_resource.get(resource_id, False)
            or _surface_item_state(registry_final_history, resource_id, attempt_id) == "untried"
        )
    # Merge source and registry lanes only after each bounded SQL query has
    # applied its eligibility predicates.  A persistent retry in one family
    # therefore cannot consume every operation while the other family has
    # untried work available.
    family_rows: list[tuple[str, object, object]] = [
        *(("source", attempt, source) for attempt, source in ordered_source_rows),
        *(("registry", attempt, adapter) for attempt, adapter in ordered_registry_rows),
    ]

    def family_row_rank(row: tuple[str, object, object]) -> tuple[object, ...]:
        family, attempt, resource = row
        if family == "source":
            return _row_fairness_key(
                (attempt, resource),
                resource_id_for=lambda value: value[1].id,
                attempt_id_for=lambda value: value[0].id,
                stage_history=source_stage_history,
                final_history=source_final_history,
                final_applicable_keys=set(source_final_manifests),
            )
        return _row_fairness_key(
            (attempt, resource),
            resource_id_for=lambda value: value[1].id,
            attempt_id_for=lambda value: value[0].id,
            stage_history=registry_stage_history,
            final_history=registry_final_history,
            final_applicable_keys=set(registry_final_manifests),
        )

    family_rows.sort(key=family_row_rank)
    selected_source_attempts: set[UUID] = set()
    selected_registry_attempts: set[UUID] = set()
    for family, attempt, resource in family_rows:
        if selected_attempts >= limit:
            break
        resource_id = resource.id
        final_key = (resource_id, attempt.id)
        if family == "source":
            stage_history = source_stage_history
            final_history = source_final_history
            final_manifests = source_final_manifests
            invalid_final_keys = source_invalid_final_keys
            final_untried_by_resource = source_final_untried_by_resource
            selected_finals = selected_source_finals
            stage_surface = "source_stage"
            final_surface = "source_final"
        else:
            stage_history = registry_stage_history
            final_history = registry_final_history
            final_manifests = registry_final_manifests
            invalid_final_keys = registry_invalid_final_keys
            final_untried_by_resource = registry_final_untried_by_resource
            selected_finals = selected_registry_finals
            stage_surface = "registry_stage"
            final_surface = "registry_final"
        selected_this_attempt = False
        stage_state = _surface_item_state(stage_history, resource_id, attempt.id)
        final_state = _surface_item_state(final_history, resource_id, attempt.id)
        if stage_state in {"untried", "blocked"}:
            result.append(
                _Candidate(
                    stage_surface,
                    department_id,
                    resource_id if family == "source" else None,
                    resource_id if family == "registry" else None,
                    attempt.id if family == "source" else None,
                    attempt.id if family == "registry" else None,
                    attempt.publication_attempt_id,
                    attempt.attempt_number,
                    resource.version,
                    attempt.version,
                    None,
                )
            )
            selected_this_attempt = True
        invalid_final = final_key in invalid_final_keys and final_state == "untried"
        preferred_final = (
            final_key in final_manifests
            and (
                final_state == "untried"
                or (
                    final_state == "blocked"
                    and not final_untried_by_resource.get(resource_id, False)
                )
            )
        ) or invalid_final
        if resource_id not in selected_finals and preferred_final:
            result.append(
                _Candidate(
                    final_surface,
                    department_id,
                    resource_id if family == "source" else None,
                    resource_id if family == "registry" else None,
                    attempt.id if family == "source" else None,
                    attempt.id if family == "registry" else None,
                    attempt.publication_attempt_id,
                    attempt.attempt_number,
                    resource.version,
                    attempt.version,
                    final_manifests.get(final_key, {}),
                )
            )
            selected_finals.add(resource_id)
            selected_this_attempt = True
        confirmation_ready = (
            final_key not in invalid_final_keys
            and _surface_is_fully_completed(stage_history, resource_id, attempt.id)
            and (
                final_key not in final_manifests
                or _surface_is_fully_completed(final_history, resource_id, attempt.id)
            )
        )
        if not selected_this_attempt and confirmation_ready:
            result.append(
                _Candidate(
                    stage_surface,
                    department_id,
                    resource_id if family == "source" else None,
                    resource_id if family == "registry" else None,
                    attempt.id if family == "source" else None,
                    attempt.id if family == "registry" else None,
                    attempt.publication_attempt_id,
                    attempt.attempt_number,
                    resource.version,
                    attempt.version,
                    dict(_CONFIRMATION_ONLY_MARKER),
                    confirmation_only=True,
                )
            )
            selected_this_attempt = True
        if selected_this_attempt:
            selected_attempts += 1
            (selected_source_attempts if family == "source" else selected_registry_attempts).add(
                attempt.id
            )

    # Preselection never locks.  Lock only the exact distinct attempts that
    # survived global fairness; rows skipped by a concurrent operation are
    # omitted instead of widening this operation's lock footprint.
    locked_source_attempts: set[UUID] = set()
    if selected_source_attempts:
        locked_source_attempts = set(
            session.scalars(
                select(AdapterImportAttempt.id)
                .join(
                    AdapterImportSource,
                    (AdapterImportSource.id == AdapterImportAttempt.source_bundle_id)
                    & (AdapterImportSource.department_id == AdapterImportAttempt.department_id),
                )
                .where(
                    AdapterImportAttempt.department_id == department_id,
                    AdapterImportAttempt.id.in_(selected_source_attempts),
                )
                .with_for_update(skip_locked=True)
            ).all()
        )
    locked_registry_attempts: set[UUID] = set()
    if selected_registry_attempts:
        locked_registry_attempts = set(
            session.scalars(
                select(AdapterRegistryAttempt.id)
                .join(
                    Adapter,
                    (Adapter.id == AdapterRegistryAttempt.adapter_id)
                    & (Adapter.department_id == AdapterRegistryAttempt.department_id),
                )
                .where(
                    AdapterRegistryAttempt.department_id == department_id,
                    AdapterRegistryAttempt.id.in_(selected_registry_attempts),
                )
                .with_for_update(skip_locked=True)
            ).all()
        )
    return tuple(
        candidate
        for candidate in result
        if (
            candidate.import_attempt_id in locked_source_attempts
            if candidate.import_attempt_id is not None
            else candidate.registry_attempt_id in locked_registry_attempts
        )
    )


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
    confirmation_only = (
        item.surface_type in {"source_stage", "registry_stage"}
        and item.ownership_manifest == _CONFIRMATION_ONLY_MARKER
    )
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
        None
        if confirmation_only
        else dict(item.ownership_manifest)
        if isinstance(item.ownership_manifest, dict)
        else None,
        item.version,
        confirmation_only,
    )


def _load_item(
    session: Session,
    operation_id: UUID,
    candidate: _Candidate,
    issuer: str,
    subject: str,
    *,
    expected_item_version: int | None = None,
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
    if expected_item_version is not None and item.version != expected_item_version:
        raise ServiceError(409, "Adapter reconciliation authority changed")
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
            or source.authoritative_attempt_id is not None
            or attempt.status not in TERMINAL_SOURCE_ATTEMPT_STATUSES
        ):
            raise ServiceError(409, "Adapter reconciliation authority changed")
        committed_sibling = session.scalar(
            select(AdapterImportAttempt.id)
            .where(
                AdapterImportAttempt.department_id == item.department_id,
                AdapterImportAttempt.source_bundle_id == item.source_bundle_id,
                AdapterImportAttempt.status == "committed",
            )
            .limit(1)
        )
        if committed_sibling is not None:
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
    succeeded_sibling = session.scalar(
        select(AdapterRegistryAttempt.id)
        .where(
            AdapterRegistryAttempt.department_id == item.department_id,
            AdapterRegistryAttempt.adapter_id == item.adapter_id,
            AdapterRegistryAttempt.status == "succeeded",
        )
        .limit(1)
    )
    active_sibling = session.scalar(
        select(AdapterRegistryAttempt.id)
        .where(
            AdapterRegistryAttempt.department_id == item.department_id,
            AdapterRegistryAttempt.adapter_id == item.adapter_id,
            AdapterRegistryAttempt.status.in_(("registered", "running", "staged", "published")),
        )
        .limit(1)
    )
    if succeeded_sibling is not None or active_sibling is not None:
        raise ServiceError(409, "Adapter reconciliation authority changed")
    if item.surface_type == "registry_final":
        if not isinstance(item.ownership_manifest, dict):
            raise ServiceError(409, "Adapter reconciliation authority changed")


def _manifest_authority(
    session: Session, item: AdapterArtifactOperationItem
) -> tuple[str | None, int | None]:
    """Return persisted-attempt manifest authority, checking optional resource fields."""

    if item.surface_type == "source_final":
        source = session.execute(
            select(AdapterImportSource)
            .where(
                AdapterImportSource.id == item.source_bundle_id,
                AdapterImportSource.department_id == item.department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if source is None:
            raise ServiceError(409, "Adapter reconciliation authority changed")
        expected_sha256, expected_size = _persisted_manifest_authority(
            "source_final", item.ownership_manifest
        )
        if (
            source.intake_manifest_sha256 is not None
            and source.intake_manifest_sha256 != expected_sha256
        ):
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        if (
            source.intake_manifest_byte_size is not None
            and source.intake_manifest_byte_size != expected_size
        ):
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        return expected_sha256, expected_size
    if item.surface_type == "registry_final":
        adapter = session.execute(
            select(Adapter)
            .where(
                Adapter.id == item.adapter_id,
                Adapter.department_id == item.department_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if adapter is None:
            raise ServiceError(409, "Adapter reconciliation authority changed")
        expected_sha256, expected_size = _persisted_manifest_authority(
            "registry_final", item.ownership_manifest
        )
        if (
            adapter.registry_manifest_sha256 is not None
            and adapter.registry_manifest_sha256 != expected_sha256
        ):
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        return expected_sha256, expected_size
    return None, None


def _bound_from_item(item: AdapterArtifactOperationItem) -> BoundSurface:
    if (
        not isinstance(item.observed_identity, dict)
        or not isinstance(item.deletion_plan, list)
        or not isinstance(item.tombstone_identity, dict)
        or not isinstance(item.expected_tombstone_namespace, dict)
    ):
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    namespace = _tombstone_namespace(item)
    if item.expected_tombstone_namespace != namespace:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    path_attempt_id = None
    if item.surface_type == "source_stage":
        path_attempt_id = item.import_attempt_id
    elif item.surface_type == "registry_stage":
        path_attempt_id = item.publication_attempt_id
    resource_id = item.source_bundle_id or item.adapter_id
    if resource_id is None:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    return BoundSurface(
        item.surface_type,
        item.department_id,
        resource_id,
        path_attempt_id,
        item.id,
        dict(item.observed_identity),
        list(item.deletion_plan),
        dict(item.tombstone_identity),
    )


def _inspection_from_item(item: AdapterArtifactOperationItem):
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
    resource_id = item.source_bundle_id or item.adapter_id
    if resource_id is None:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    path_attempt_id = None
    if item.surface_type == "source_stage":
        path_attempt_id = item.import_attempt_id
    elif item.surface_type == "registry_stage":
        path_attempt_id = item.publication_attempt_id
    address = physical_surface_identifier(
        item.surface_type, item.department_id, resource_id, path_attempt_id
    )
    return InspectedSurface(
        address.surface_type,
        address.department_id,
        address.resource_id,
        address.path_attempt_id,
        item.id,
        dict(item.observed_identity),
        [
            {"name": entry["name"], "identity": dict(entry["identity"])}
            for entry in item.deletion_plan
        ],
    )


def _tombstone_namespace(item: AdapterArtifactOperationItem) -> dict[str, object]:
    resource_id = item.source_bundle_id or item.adapter_id
    if resource_id is None:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    return {
        "surface_type": item.surface_type,
        "department_id": str(item.department_id),
        "resource_id": str(resource_id),
        "item_id": str(item.id),
    }


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
        inspected = None
        bound = None
        verified_version: int | None = None
        move_version: int | None = None
        expected_manifest_sha256: str | None = None
        expected_manifest_byte_size: int | None = None
        move_authorized = False
        confirmation_only = False
        with factory.begin() as session:
            item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
            item_id = item.id
            if candidate.confirmation_only:
                if item.ownership_manifest != _CONFIRMATION_ONLY_MARKER:
                    raise ServiceError(409, "Adapter reconciliation authority changed")
                confirmation_only = True
            address = _candidate_surface_address(candidate)
            move_authorized = item.move_authorized_at is not None
            if candidate.surface_type.endswith("_final"):
                expected_manifest_sha256, expected_manifest_byte_size = _manifest_authority(
                    session, item
                )
            if item.status in {"tombstone_bound", "deleting"}:
                bound = _bound_from_item(item)
            elif item.status == "verified":
                inspected = _inspection_from_item(item)
                verified_version = item.version
            elif item.status != "registered":
                return item.status == "completed"
        if confirmation_only:
            return _mark_confirmation_completed(
                factory,
                data_dir=data_dir,
                operation_id=operation_id,
                candidate=candidate,
                issuer=actor_issuer,
                subject=actor_subject,
            )
        with AdapterMaintenanceArtifactStore(data_dir) as store:
            if inspected is None and bound is None:
                # A marker or partial payload is never an ownership boundary;
                # only exact metadata and the item-scoped tombstone namespace
                # can authorize this check.
                if store.tombstone_exists(address, item_id):
                    raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
                inspected = store.inspect_surface(
                    address,
                    item_id,
                    expected_manifest=candidate.ownership_manifest,
                    expected_manifest_sha256=expected_manifest_sha256,
                    expected_manifest_byte_size=expected_manifest_byte_size,
                )
                if inspected is None:
                    _mark_completed(
                        factory,
                        data_dir=data_dir,
                        operation_id=operation_id,
                        candidate=candidate,
                        issuer=actor_issuer,
                        subject=actor_subject,
                    )
                    return True
                with factory.begin() as session:
                    item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
                    if item.status != "registered":
                        raise ServiceError(409, "Adapter reconciliation authority changed")
                    item.observed_identity = inspected.observed_identity
                    item.deletion_plan = inspected.deletion_plan
                    item.status = "verified"
                    item.verified_at = session.scalar(select(func.clock_timestamp()))
                    item.version += 1
                    verified_version = item.version
                # The short transaction above is the durable verification
                # boundary.  The move intent is a separate transaction so a
                # crash can distinguish an unbound tombstone from a move that
                # was authorized before rename.
                with factory.begin() as session:
                    item = _load_item(
                        session,
                        operation_id,
                        candidate,
                        actor_issuer,
                        actor_subject,
                        expected_item_version=verified_version,
                    )
                    if item.status != "verified":
                        raise ServiceError(409, "Adapter reconciliation authority changed")
                    inspected = _inspection_from_item(item)
                    namespace = _tombstone_namespace(item)
                    item.move_authorized_at = session.scalar(select(func.clock_timestamp()))
                    item.expected_tombstone_namespace = namespace
                    item.version += 1
                    move_version = item.version
                    move_authorized = True
            if inspected is not None and bound is None:
                preexisting_tombstone = store.tombstone_exists(address, item_id)
                if preexisting_tombstone and not move_authorized:
                    raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
                with factory.begin() as session:
                    expected_verified = verified_version if not move_authorized else None
                    item = _load_item(
                        session,
                        operation_id,
                        candidate,
                        actor_issuer,
                        actor_subject,
                        expected_item_version=expected_verified,
                    )
                    if item.status != "verified":
                        raise ServiceError(409, "Adapter reconciliation authority changed")
                    inspected = _inspection_from_item(item)
                    namespace = item.expected_tombstone_namespace
                    if not isinstance(namespace, dict):
                        item.move_authorized_at = session.scalar(select(func.clock_timestamp()))
                        namespace = _tombstone_namespace(item)
                        item.expected_tombstone_namespace = namespace
                        item.version += 1
                        move_authorized = True
                        move_version = item.version
                if preexisting_tombstone:
                    # A committed move intent permits only exact recovery when
                    # the original is absent.  If it is still present, this is
                    # an unbound conflict and neither surface is touched.
                    if store.surface_exists(address):
                        raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
                    bound = store.recover_authorized_move(
                        inspected, expected_tombstone_namespace=namespace
                    )
                else:
                    bound = store.move_verified_surface_to_tombstone(
                        inspected, expected_tombstone_namespace=namespace
                    )
                    if bound is None:
                        raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                with factory.begin() as session:
                    item = _load_item(
                        session,
                        operation_id,
                        candidate,
                        actor_issuer,
                        actor_subject,
                        expected_item_version=move_version,
                    )
                    if item.status != "verified" or item.tombstone_identity is not None:
                        raise ServiceError(409, "Adapter reconciliation authority changed")
                    if (
                        item.observed_identity != bound.observed_identity
                        or item.deletion_plan != bound.deletion_plan
                    ):
                        raise ServiceError(409, "Adapter reconciliation authority changed")
                    item.tombstone_identity = bound.tombstone_identity
                    item.status = "tombstone_bound"
                    item.tombstone_bound_at = session.scalar(select(func.clock_timestamp()))
                    item.version += 1
            with factory.begin() as session:
                item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
                if item.status == "verified":
                    raise ServiceError(409, "Adapter reconciliation authority changed")
                bound = _bound_from_item(item)
                start_index = item.next_entry_index
            if start_index == 0:
                store.open_committed_tombstone(bound)
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
                    store.unlink_committed_tombstone_entry(
                        bound, name, allow_missing=resumed_unlink
                    )
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
            store.remove_committed_tombstone_directory(bound, allow_missing=already_started)
            _mark_completed(
                factory,
                data_dir=data_dir,
                operation_id=operation_id,
                candidate=candidate,
                issuer=actor_issuer,
                subject=actor_subject,
            )
            return True
    except AdapterMaintenanceArtifactError as error:
        if not _retain_active_after_artifact_error(
            factory, data_dir, operation_id, candidate, error
        ):
            _mark_blocked(factory, operation_id, candidate, actor_issuer, actor_subject, error.code)
        return False
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _retain_active_after_artifact_error(
    factory: sessionmaker[Session],
    data_dir: Path,
    operation_id: UUID,
    candidate: _Candidate,
    error: AdapterMaintenanceArtifactError,
) -> bool:
    """Keep only an exactly recoverable physical move resumable."""

    try:
        with factory.begin() as session:
            row = session.execute(
                select(AdapterArtifactOperationItem)
                .where(
                    AdapterArtifactOperationItem.operation_id == operation_id,
                    AdapterArtifactOperationItem.department_id == candidate.department_id,
                    AdapterArtifactOperationItem.surface_type == candidate.surface_type,
                    AdapterArtifactOperationItem.publication_attempt_id
                    == candidate.publication_attempt_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return False
            address = _candidate_surface_address(candidate)
            with AdapterMaintenanceArtifactStore(data_dir) as store:
                # Mutation recovery is scoped to this exact resource.  A
                # sibling resource's tombstone is handled by the later
                # attempt-wide cleanup confirmation and must not starve a
                # valid item in this operation.
                tombstones = store.enumerate_tombstones(address)
                original_exists = store.surface_exists(address)
                item_tombstone = row.id in tombstones
                if any(tombstone_item != row.id for tombstone_item in tombstones):
                    error.code = "artifact_tombstone_conflict"
                    return False
                if original_exists and item_tombstone:
                    error.code = "artifact_tombstone_conflict"
                    return False
                if not original_exists and item_tombstone:
                    if row.status == "verified" and row.move_authorized_at is not None:
                        inspected = _inspection_from_item(row)
                        store.recover_authorized_move(
                            inspected,
                            expected_tombstone_namespace=_tombstone_namespace(row),
                        )
                        return True
                    if row.status in {"tombstone_bound", "deleting"}:
                        store.open_committed_tombstone(_bound_from_item(row))
                        return True
                    error.code = "artifact_authority_changed"
                    return False
                if not original_exists:
                    if error.code != "artifact_manifest_invalid":
                        error.code = "artifact_authority_changed"
                return False
    except AdapterMaintenanceArtifactError as nested:
        error.code = nested.code
        return False
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _mark_completed(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
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
        _confirm_attempt_cleanup(session, item, data_dir=data_dir)


def _mark_confirmation_completed(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    operation_id: UUID,
    candidate: _Candidate,
    issuer: str,
    subject: str,
) -> bool:
    """Complete confirmation work without reopening a physical surface."""

    with factory.begin() as session:
        item = _load_item(session, operation_id, candidate, issuer, subject)
        if item.ownership_manifest != _CONFIRMATION_ONLY_MARKER:
            raise ServiceError(409, "Adapter reconciliation authority changed")
        confirmed = _confirm_attempt_cleanup(
            session,
            item,
            data_dir=data_dir,
            ignore_item_id=item.id,
        )
        now = session.scalar(select(func.clock_timestamp()))
        item.status = "completed" if confirmed else "blocked"
        item.completed_at = now if confirmed else None
        item.blocked_at = None if confirmed else now
        item.blocked_reason_code = None if confirmed else "artifact_authority_changed"
        item.in_flight_entry = None
        item.version += 1
        return confirmed


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


def _confirm_attempt_cleanup(
    session: Session,
    item: AdapterArtifactOperationItem,
    *,
    data_dir: Path,
    ignore_item_id: UUID | None = None,
) -> bool:
    """Confirm an exact attempt only after every surface and tombstone is absent."""

    if item.import_attempt_id is not None:
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
            or attempt.status not in TERMINAL_SOURCE_ATTEMPT_STATUSES
            or source.status in PROTECTED_SOURCE_STATUSES
            or source.authoritative_attempt_id is not None
        ):
            return False
        committed_sibling = session.scalar(
            select(AdapterImportAttempt.id)
            .where(
                AdapterImportAttempt.department_id == item.department_id,
                AdapterImportAttempt.source_bundle_id == item.source_bundle_id,
                AdapterImportAttempt.status == "committed",
            )
            .limit(1)
        )
        active_sibling = session.scalar(
            select(AdapterImportAttempt.id)
            .where(
                AdapterImportAttempt.department_id == item.department_id,
                AdapterImportAttempt.source_bundle_id == item.source_bundle_id,
                ~AdapterImportAttempt.status.in_(tuple(TERMINAL_SOURCE_ATTEMPT_STATUSES)),
            )
            .limit(1)
        )
        if committed_sibling is not None or active_sibling is not None:
            return False
        resource_id = item.source_bundle_id
        final_applicable = False
        if isinstance(attempt.ownership_manifest, dict):
            try:
                expected_sha256, expected_size = _persisted_manifest_authority(
                    "source_final", attempt.ownership_manifest
                )
            except AdapterMaintenanceArtifactError:
                return False
            if source.intake_manifest_sha256 not in (
                None,
                expected_sha256,
            ) or source.intake_manifest_byte_size not in (None, expected_size):
                return False
            final_applicable = True
        expected_surfaces = ("source_stage",) + (("source_final",) if final_applicable else ())
        item_rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == item.department_id,
                AdapterArtifactOperationItem.source_bundle_id == resource_id,
                AdapterArtifactOperationItem.import_attempt_id == attempt.id,
                AdapterArtifactOperationItem.publication_attempt_id
                == attempt.publication_attempt_id,
                AdapterArtifactOperationItem.attempt_number == attempt.attempt_number,
                (
                    AdapterArtifactOperationItem.id != ignore_item_id
                    if ignore_item_id is not None
                    else True
                ),
            )
            .with_for_update()
        ).all()
    else:
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
            or attempt.status not in TERMINAL_REGISTRY_ATTEMPT_STATUSES
            or adapter.status in PROTECTED_ADAPTER_STATUSES
        ):
            return False
        succeeded_sibling = session.scalar(
            select(AdapterRegistryAttempt.id)
            .where(
                AdapterRegistryAttempt.department_id == item.department_id,
                AdapterRegistryAttempt.adapter_id == item.adapter_id,
                AdapterRegistryAttempt.status == "succeeded",
            )
            .limit(1)
        )
        active_sibling = session.scalar(
            select(AdapterRegistryAttempt.id)
            .where(
                AdapterRegistryAttempt.department_id == item.department_id,
                AdapterRegistryAttempt.adapter_id == item.adapter_id,
                AdapterRegistryAttempt.status.in_(("registered", "running", "staged", "published")),
            )
            .limit(1)
        )
        if succeeded_sibling is not None or active_sibling is not None:
            return False
        resource_id = item.adapter_id
        final_applicable = False
        if isinstance(attempt.ownership_manifest, dict):
            try:
                expected_sha256, _expected_size = _persisted_manifest_authority(
                    "registry_final", attempt.ownership_manifest
                )
            except AdapterMaintenanceArtifactError:
                return False
            if adapter.registry_manifest_sha256 not in (None, expected_sha256):
                return False
            final_applicable = True
        expected_surfaces = ("registry_stage",) + (("registry_final",) if final_applicable else ())
        item_rows = session.scalars(
            select(AdapterArtifactOperationItem)
            .where(
                AdapterArtifactOperationItem.department_id == item.department_id,
                AdapterArtifactOperationItem.adapter_id == resource_id,
                AdapterArtifactOperationItem.registry_attempt_id == attempt.id,
                AdapterArtifactOperationItem.publication_attempt_id
                == attempt.publication_attempt_id,
                AdapterArtifactOperationItem.attempt_number == attempt.attempt_number,
                (
                    AdapterArtifactOperationItem.id != ignore_item_id
                    if ignore_item_id is not None
                    else True
                ),
            )
            .with_for_update()
        ).all()
    if attempt.cleanup_confirmed_at is not None:
        return True
    by_surface: dict[str, list[AdapterArtifactOperationItem]] = {}
    for row in item_rows:
        by_surface.setdefault(row.surface_type, []).append(row)
    # A blocked row is immutable history, not a permanent denylist.  A later
    # operation records a fresh item for the same surface; cleanup is
    # confirmed once every applicable surface has a completed generation and
    # no generation is still active.
    for surface in expected_surfaces:
        rows = by_surface.get(surface, [])
        if not rows or any(row.status in ACTIVE_ITEM_STATUSES for row in rows):
            return False
        if not any(row.status == "completed" for row in rows):
            return False
    if any(surface not in by_surface for surface in expected_surfaces):
        return False
    try:
        with AdapterMaintenanceArtifactStore(data_dir) as store:
            all_surfaces = (
                ("source_stage", "source_final")
                if item.import_attempt_id is not None
                else ("registry_stage", "registry_final")
            )
            unsafe_surface = False
            for surface in all_surfaces:
                path_attempt = (
                    attempt.id
                    if surface == "source_stage"
                    else attempt.publication_attempt_id
                    if surface == "registry_stage"
                    else None
                )
                address = physical_surface_identifier(
                    surface, item.department_id, resource_id, path_attempt
                )
                if store.surface_exists(address):
                    unsafe_surface = True
                tombstones = store.enumerate_department_tombstones(surface, item.department_id)
                if tombstones:
                    # Cleanup confirmation requires the complete department
                    # namespace to be empty, not merely the item IDs currently
                    # represented by this operation. This also fences an
                    # unknown resource or sibling attempt tombstone.
                    unsafe_surface = True
            if unsafe_surface:
                return False
    except AdapterMaintenanceArtifactError:
        return False
    now = session.scalar(select(func.clock_timestamp()))
    if attempt.cleanup_confirmed_at is None:
        attempt.cleanup_confirmed_at = now
        attempt.version += 1
    return True


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
        # A completed item is only progress.  A success audit requires a
        # durable exact-attempt cleanup_confirmed_at row; a mixed operation
        # may still contain blocked work for another attempt/resource.
        if _has_unaudited_completion(session, operation_id, department_id, items):
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


def _has_unaudited_completion(
    session: Session,
    operation_id: UUID,
    department_id: UUID,
    items: list[AdapterArtifactOperationItem],
) -> bool:
    """Return whether this operation has an uncovered confirmed resource.

    ``completed`` item rows are only progress records.  The durable attempt
    ``cleanup_confirmed_at`` timestamp is the success authority.  Resource
    keys stay at the reviewed department/resource-family scope so a later
    retry of the same resource does not duplicate an earlier mixed-operation
    audit, while a different source or registry resource remains auditable.
    Every ``IN`` set comes from this bounded operation's items.
    """

    source_attempt_ids = {
        item.import_attempt_id
        for item in items
        if item.status == "completed"
        and item.import_attempt_id is not None
        and item.source_bundle_id is not None
    }
    registry_attempt_ids = {
        item.registry_attempt_id
        for item in items
        if item.status == "completed"
        and item.registry_attempt_id is not None
        and item.adapter_id is not None
    }
    confirmed_keys: set[tuple[str, UUID]] = set()
    if source_attempt_ids:
        confirmed_keys.update(
            (source_id, "source")
            for (source_id,) in session.execute(
                select(AdapterImportAttempt.source_bundle_id).where(
                    AdapterImportAttempt.department_id == department_id,
                    AdapterImportAttempt.id.in_(source_attempt_ids),
                    AdapterImportAttempt.cleanup_confirmed_at.is_not(None),
                )
            ).all()
        )
    if registry_attempt_ids:
        confirmed_keys.update(
            (adapter_id, "registry")
            for (adapter_id,) in session.execute(
                select(AdapterRegistryAttempt.adapter_id).where(
                    AdapterRegistryAttempt.department_id == department_id,
                    AdapterRegistryAttempt.id.in_(registry_attempt_ids),
                    AdapterRegistryAttempt.cleanup_confirmed_at.is_not(None),
                )
            ).all()
        )
    if not confirmed_keys:
        return False

    prior_operation = aliased(AdapterArtifactOperation)
    prior_completed = aliased(AdapterArtifactOperationItem)
    prior_audit = aliased(PersistentAuditEvent)
    prior_source_attempt = aliased(AdapterImportAttempt)
    prior_registry_attempt = aliased(AdapterRegistryAttempt)

    def covered_resources(
        resource_column,
        resource_ids: set[UUID],
        attempt_model,
        attempt_column,
        resource_attempt_column,
    ) -> set[UUID]:
        if not resource_ids:
            return set()
        prior_attempt = (
            prior_source_attempt
            if attempt_model is AdapterImportAttempt
            else prior_registry_attempt
        )
        statement = (
            select(resource_column)
            .select_from(prior_completed)
            .join(
                prior_operation,
                and_(
                    prior_operation.id == prior_completed.operation_id,
                    prior_operation.department_id == department_id,
                ),
            )
            .join(
                prior_attempt,
                and_(
                    prior_attempt.id == attempt_column,
                    prior_attempt.department_id == department_id,
                    resource_attempt_column == resource_column,
                    prior_attempt.cleanup_confirmed_at.is_not(None),
                ),
            )
            .join(
                prior_audit,
                and_(
                    prior_audit.resource_id == cast(prior_operation.id, String),
                    prior_audit.department_id == department_id,
                ),
            )
            .where(
                prior_completed.department_id == department_id,
                prior_completed.status == "completed",
                prior_operation.id != operation_id,
                prior_audit.action == "adapter.artifact.reconcile",
                prior_audit.resource_type == "adapter_artifact_operation",
                prior_audit.result == "allowed",
                resource_column.in_(resource_ids),
                prior_operation.completed_at.is_not(None),
                prior_attempt.cleanup_confirmed_at <= prior_operation.completed_at,
            )
            .distinct()
        )
        return set(session.scalars(statement).all())

    source_ids = {resource_id for resource_id, family in confirmed_keys if family == "source"}
    registry_ids = {resource_id for resource_id, family in confirmed_keys if family == "registry"}
    covered = {
        (resource_id, "source")
        for resource_id in covered_resources(
            prior_completed.source_bundle_id,
            source_ids,
            AdapterImportAttempt,
            prior_completed.import_attempt_id,
            prior_source_attempt.source_bundle_id,
        )
    }
    covered.update(
        (resource_id, "registry")
        for resource_id in covered_resources(
            prior_completed.adapter_id,
            registry_ids,
            AdapterRegistryAttempt,
            prior_completed.registry_attempt_id,
            prior_registry_attempt.adapter_id,
        )
    )
    return bool(confirmed_keys - covered)


__all__ = [
    "ADAPTER_ARTIFACT_ADMIN_ROLES",
    "AdapterArtifactMaintenanceConfigurationError",
    "AdapterArtifactMaintenanceResult",
    "AdapterArtifactMaintenanceSettings",
    "reconcile_adapter_artifacts",
]
