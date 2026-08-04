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
    expected_item_version: int = 1


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


def _surface_item_history(
    session: Session,
    *,
    department_id: UUID,
    surface_type: str,
    resource_column,
    attempt_column,
) -> dict[tuple[UUID, UUID], frozenset[str]]:
    """Return immutable status history keyed by the exact resource/attempt."""

    history: dict[tuple[UUID, UUID], set[str]] = {}
    rows = session.execute(
        select(resource_column, attempt_column, AdapterArtifactOperationItem.status).where(
            AdapterArtifactOperationItem.department_id == department_id,
            AdapterArtifactOperationItem.surface_type == surface_type,
        )
    ).all()
    for resource_id, attempt_id, status in rows:
        if resource_id is None or attempt_id is None:
            continue
        history.setdefault((resource_id, attempt_id), set()).add(status)
    return {key: frozenset(statuses) for key, statuses in history.items()}


def _surface_item_state(
    history: dict[tuple[UUID, UUID], frozenset[str]],
    resource_id: UUID,
    attempt_id: UUID,
) -> str:
    """Classify one exact surface without mutating historical rows."""

    statuses = history.get((resource_id, attempt_id), frozenset())
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


def _prioritize_final_siblings(
    rows: list[tuple[object, object]],
    *,
    resource_id_for,
    attempt_id_for,
    final_history: dict[tuple[UUID, UUID], frozenset[str]],
    valid_final_keys: set[tuple[UUID, UUID]],
) -> tuple[tuple[object, object], ...]:
    """Prefer the first untried final sibling, then the first blocked retry."""

    grouped: dict[UUID, list[tuple[object, object]]] = {}
    for row in rows:
        grouped.setdefault(resource_id_for(row), []).append(row)
    ordered: list[tuple[object, object]] = []
    for group in grouped.values():
        preferred = None
        valid = [
            row for row in group if (resource_id_for(row), attempt_id_for(row)) in valid_final_keys
        ]
        for desired_state in ("untried", "blocked"):
            preferred = next(
                (
                    row
                    for row in valid
                    if _surface_item_state(
                        final_history,
                        resource_id_for(row),
                        attempt_id_for(row),
                    )
                    == desired_state
                ),
                None,
            )
            if preferred is not None:
                break
        if preferred is not None:
            ordered.append(preferred)
            ordered.extend(row for row in group if row is not preferred)
        else:
            ordered.extend(group)
    return tuple(ordered)


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
    source_stage_history = _surface_item_history(
        session,
        department_id=department_id,
        surface_type="source_stage",
        resource_column=AdapterArtifactOperationItem.source_bundle_id,
        attempt_column=AdapterArtifactOperationItem.import_attempt_id,
    )
    source_final_history = _surface_item_history(
        session,
        department_id=department_id,
        surface_type="source_final",
        resource_column=AdapterArtifactOperationItem.source_bundle_id,
        attempt_column=AdapterArtifactOperationItem.import_attempt_id,
    )
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
    ).all()
    eligible_source_rows: list[tuple[AdapterImportAttempt, AdapterImportSource]] = []
    source_final_manifests: dict[tuple[UUID, UUID], dict[str, object]] = {}
    for attempt, source in source_rows:
        committed_sibling = session.scalar(
            select(AdapterImportAttempt.id)
            .where(
                AdapterImportAttempt.department_id == department_id,
                AdapterImportAttempt.source_bundle_id == source.id,
                AdapterImportAttempt.status == "committed",
            )
            .limit(1)
        )
        if committed_sibling is not None:
            continue
        stale = attempt.status not in TERMINAL_SOURCE_ATTEMPT_STATUSES
        if stale and (source.status != "staging" or source.authoritative_attempt_id is not None):
            continue
        eligible_source_rows.append((attempt, source))
        if (
            isinstance(attempt.ownership_manifest, dict)
            and source.status not in PROTECTED_SOURCE_STATUSES
        ):
            try:
                _persisted_manifest_authority("source_final", attempt.ownership_manifest)
            except AdapterMaintenanceArtifactError:
                continue
            source_final_manifests[(source.id, attempt.id)] = dict(attempt.ownership_manifest)
    ordered_source_rows = _prioritize_final_siblings(
        eligible_source_rows,
        resource_id_for=lambda row: row[1].id,
        attempt_id_for=lambda row: row[0].id,
        final_history=source_final_history,
        valid_final_keys=set(source_final_manifests),
    )
    source_final_untried_by_resource: dict[UUID, bool] = {}
    for resource_id, attempt_id in source_final_manifests:
        source_final_untried_by_resource[resource_id] = (
            source_final_untried_by_resource.get(resource_id, False)
            or _surface_item_state(source_final_history, resource_id, attempt_id) == "untried"
        )
    source_final_blocked_retries: set[UUID] = set()
    for attempt, source in ordered_source_rows:
        if selected_attempts >= limit:
            break
        selected_this_attempt = False
        final_key = (source.id, attempt.id)
        final_state = _surface_item_state(source_final_history, source.id, attempt.id)
        stage_state = _surface_item_state(source_stage_history, source.id, attempt.id)
        stage_retryable = stage_state == "untried" or (
            stage_state == "blocked"
            and (
                final_state == "untried"
                or not source_final_untried_by_resource.get(source.id, False)
            )
        )
        if stage_retryable:
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
            selected_this_attempt = True
        preferred_final = final_key in source_final_manifests and (
            final_state == "untried"
            or (
                final_state == "blocked"
                and not source_final_untried_by_resource.get(source.id, False)
                and source.id not in source_final_blocked_retries
            )
        )
        if source.id not in selected_source_finals and preferred_final:
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
                    source_final_manifests[final_key],
                )
            )
            selected_source_finals.add(source.id)
            selected_this_attempt = True
            if final_state == "blocked":
                source_final_blocked_retries.add(source.id)
        if selected_this_attempt:
            selected_attempts += 1
    remaining_attempts = max(0, limit - selected_attempts)
    if remaining_attempts == 0:
        return tuple(result)
    registry_stage_history = _surface_item_history(
        session,
        department_id=department_id,
        surface_type="registry_stage",
        resource_column=AdapterArtifactOperationItem.adapter_id,
        attempt_column=AdapterArtifactOperationItem.registry_attempt_id,
    )
    registry_final_history = _surface_item_history(
        session,
        department_id=department_id,
        surface_type="registry_final",
        resource_column=AdapterArtifactOperationItem.adapter_id,
        attempt_column=AdapterArtifactOperationItem.registry_attempt_id,
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
        )
        .order_by(AdapterRegistryAttempt.created_at, AdapterRegistryAttempt.id)
        .with_for_update(skip_locked=True)
    ).all()
    eligible_registry_rows: list[tuple[AdapterRegistryAttempt, Adapter]] = []
    registry_final_manifests: dict[tuple[UUID, UUID], dict[str, object]] = {}
    for attempt, adapter in registry_rows:
        succeeded_sibling = session.scalar(
            select(AdapterRegistryAttempt.id)
            .where(
                AdapterRegistryAttempt.department_id == department_id,
                AdapterRegistryAttempt.adapter_id == adapter.id,
                AdapterRegistryAttempt.status == "succeeded",
            )
            .limit(1)
        )
        active_sibling = session.scalar(
            select(AdapterRegistryAttempt.id)
            .where(
                AdapterRegistryAttempt.department_id == department_id,
                AdapterRegistryAttempt.adapter_id == adapter.id,
                AdapterRegistryAttempt.status.in_(("registered", "running", "staged", "published")),
            )
            .limit(1)
        )
        if succeeded_sibling is not None or active_sibling is not None:
            continue
        eligible_registry_rows.append((attempt, adapter))
        if isinstance(attempt.ownership_manifest, dict) and adapter.status in {
            "failed",
            "validation_failed",
        }:
            try:
                _persisted_manifest_authority("registry_final", attempt.ownership_manifest)
            except AdapterMaintenanceArtifactError:
                continue
            registry_final_manifests[(adapter.id, attempt.id)] = dict(attempt.ownership_manifest)
    ordered_registry_rows = _prioritize_final_siblings(
        eligible_registry_rows,
        resource_id_for=lambda row: row[1].id,
        attempt_id_for=lambda row: row[0].id,
        final_history=registry_final_history,
        valid_final_keys=set(registry_final_manifests),
    )
    registry_final_untried_by_resource: dict[UUID, bool] = {}
    for resource_id, attempt_id in registry_final_manifests:
        registry_final_untried_by_resource[resource_id] = (
            registry_final_untried_by_resource.get(resource_id, False)
            or _surface_item_state(registry_final_history, resource_id, attempt_id) == "untried"
        )
    registry_final_blocked_retries: set[UUID] = set()
    for attempt, adapter in ordered_registry_rows:
        if selected_attempts >= limit:
            break
        selected_this_attempt = False
        final_key = (adapter.id, attempt.id)
        final_state = _surface_item_state(registry_final_history, adapter.id, attempt.id)
        stage_state = _surface_item_state(registry_stage_history, adapter.id, attempt.id)
        stage_retryable = stage_state == "untried" or (
            stage_state == "blocked"
            and (
                final_state == "untried"
                or not registry_final_untried_by_resource.get(adapter.id, False)
            )
        )
        if stage_retryable:
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
            selected_this_attempt = True
        preferred_final = final_key in registry_final_manifests and (
            final_state == "untried"
            or (
                final_state == "blocked"
                and not registry_final_untried_by_resource.get(adapter.id, False)
                and adapter.id not in registry_final_blocked_retries
            )
        )
        if adapter.id not in selected_registry_finals and preferred_final:
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
                    registry_final_manifests[final_key],
                )
            )
            selected_registry_finals.add(adapter.id)
            selected_this_attempt = True
            if final_state == "blocked":
                registry_final_blocked_retries.add(adapter.id)
        if selected_this_attempt:
            selected_attempts += 1
    return tuple(result)


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
        item.version,
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
        with factory.begin() as session:
            item = _load_item(session, operation_id, candidate, actor_issuer, actor_subject)
            item_id = item.id
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
    session: Session, item: AdapterArtifactOperationItem, *, data_dir: Path
) -> None:
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
            return
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
            return
        resource_id = item.source_bundle_id
        final_applicable = False
        if isinstance(attempt.ownership_manifest, dict):
            try:
                expected_sha256, expected_size = _persisted_manifest_authority(
                    "source_final", attempt.ownership_manifest
                )
            except AdapterMaintenanceArtifactError:
                return
            if source.intake_manifest_sha256 not in (
                None,
                expected_sha256,
            ) or source.intake_manifest_byte_size not in (None, expected_size):
                return
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
            return
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
            return
        resource_id = item.adapter_id
        final_applicable = False
        if isinstance(attempt.ownership_manifest, dict):
            try:
                expected_sha256, _expected_size = _persisted_manifest_authority(
                    "registry_final", attempt.ownership_manifest
                )
            except AdapterMaintenanceArtifactError:
                return
            if adapter.registry_manifest_sha256 not in (None, expected_sha256):
                return
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
            )
            .with_for_update()
        ).all()
    if attempt.cleanup_confirmed_at is not None:
        return
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
            return
        if not any(row.status == "completed" for row in rows):
            return
    if any(surface not in by_surface for surface in expected_surfaces):
        return
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
                return
    except AdapterMaintenanceArtifactError:
        return
    now = session.scalar(select(func.clock_timestamp()))
    if attempt.cleanup_confirmed_at is None:
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
        if operation.blocked_count == 0:
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
