"""PostgreSQL 16 coverage for Phase 12.1E-C lifecycle release."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, select, update
from test_phase12_1c_integration import _enqueue
from test_phase12_1e_b_postgres import (
    _adapter_id,
    _prepare_authority,
    _purge,
    _storage,
)
from test_phase12_1e_b_postgres import (
    authority as _phase12_1e_b_authority,
)
from test_phase12_1e_b_postgres import (
    engine as _phase12_1e_b_engine,
)
from test_phase12_1e_b_postgres import (
    factory as _phase12_1e_b_factory,
)

from app import adapter_lifecycle_release, adapter_purge
from app.adapter_lifecycle_release import release_adapter_upstream_dependency
from app.adapter_maintenance_artifacts import AdapterPurgeArtifactStore
from app.adapter_registry_read_services import _project
from app.adapter_source_artifacts import canonical_manifest_bytes
from app.models import (
    Adapter,
    AdapterArtifactOperation,
    AdapterArtifactOperationItem,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterPurgeItem,
    AdapterPurgeOperation,
    AdapterPurgeReservation,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    Department,
    Membership,
    PersistentAuditEvent,
)
from app.services import ServiceError
from app.sft_maintenance import _has_active_dataset_dependency
from app.training_job_maintenance import _has_active_adapter_dependency

_PHASE12_1E_B_SHARED_FIXTURES = (
    _phase12_1e_b_authority,
    _phase12_1e_b_engine,
    _phase12_1e_b_factory,
)

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def engine(request):
    """Reuse the Phase 12.1E-B isolated PostgreSQL fixture explicitly."""

    return request.getfixturevalue("_phase12_1e_b_engine")


@pytest.fixture
def factory(request):
    """Reuse the Phase 12.1E-B session factory without plugin discovery."""

    return request.getfixturevalue("_phase12_1e_b_factory")


@pytest.fixture
def authority(request):
    """Reuse the Phase 12.1E-B authority fixture and its exact cleanup."""

    return request.getfixturevalue("_phase12_1e_b_authority")


def _versions(factory, authority) -> tuple[UUID, int, int, int]:
    with factory() as session:
        adapter = session.get(Adapter, _adapter_id(factory, authority))
        source = session.get(AdapterImportSource, authority.source_id)
        dependency = session.scalar(
            select(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.department_id == authority.department_id,
                AdapterUpstreamDependency.adapter_id == _adapter_id(factory, authority),
            )
        )
        assert adapter is not None and source is not None and dependency is not None
        return adapter.id, adapter.version, source.version, dependency.version


def _complete_purge(factory, authority, root: Path) -> tuple[UUID, int, int, int]:
    _prepare_authority(factory, authority, root)
    result = _purge(factory, authority, root, apply=True)
    assert result.applied_count == 2
    return _versions(factory, authority)


def _release(
    factory,
    authority,
    root: Path,
    *,
    apply: bool,
    versions=None,
    department_id: UUID | None = None,
):
    adapter_id, adapter_version, source_version, dependency_version = versions or _versions(
        factory, authority
    )
    return release_adapter_upstream_dependency(
        factory,
        data_dir=root,
        department_id=department_id or authority.department_id,
        adapter_id=adapter_id,
        expected_adapter_version=adapter_version,
        expected_source_version=source_version,
        expected_dependency_version=dependency_version,
        actor_issuer=authority.issuer,
        actor_subject=authority.subject,
        apply=apply,
    )


def _release_audit_count(factory, authority) -> int:
    with factory() as session:
        return int(
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.upstream_dependency.release",
                )
            )
            or 0
        )


def _assert_no_deadlock(outcome: object) -> None:
    if isinstance(outcome, BaseException):
        assert "deadlock" not in str(outcome).lower()
        if isinstance(outcome, ServiceError):
            assert outcome.status_code != 503


def test_release_requires_completed_eb_purge_and_releases_one_dependency(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    adapter_id, adapter_version, source_version, dependency_version = _complete_purge(
        factory, authority, root
    )
    result = _release(
        factory,
        authority,
        root,
        apply=True,
        versions=(adapter_id, adapter_version, source_version, dependency_version),
    )

    assert result.applied and not result.already_released
    assert result.adapter_version == adapter_version + 1
    assert result.dependency_version == dependency_version + 1
    with factory() as session:
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        dependency = session.scalar(
            select(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.department_id == authority.department_id,
                AdapterUpstreamDependency.adapter_id == adapter_id,
            )
        )
        assert adapter is not None and adapter.status == "purged"
        assert source is not None and source.status == "purged"
        assert dependency is not None and dependency.status == "released"
        assert dependency.released_at is not None
    assert _release_audit_count(factory, authority) == 1


def _add_shared_active_dependency(
    factory, authority, *, enqueue: bool = True
) -> tuple[UUID, UUID | None]:
    """Create a second valid source that independently retains the same upstream rows."""

    source_id = uuid4()
    attempt_id = uuid4()
    publication_attempt_id = uuid4()
    now = datetime.now(UTC)
    with factory.begin() as session:
        source = session.get(AdapterImportSource, authority.source_id)
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert source is not None and attempt is not None
        manifest = dict(attempt.ownership_manifest or {})
        manifest.update(
            {
                "source_bundle_id": str(source_id),
                "import_attempt_id": str(attempt_id),
                "publication_attempt_id": str(publication_attempt_id),
            }
        )
        raw = canonical_manifest_bytes(manifest)
        copied = AdapterImportSource(
            id=source_id,
            department_id=source.department_id,
            imported_by_user_id=source.imported_by_user_id,
            status="staging",
            source_contract_version=source.source_contract_version,
            intake_contract_version=source.intake_contract_version,
            config_contract_version=source.config_contract_version,
            tensor_contract_version=source.tensor_contract_version,
            base_model_id=source.base_model_id,
            base_model_revision=source.base_model_revision,
            base_model_license=source.base_model_license,
            peft_version=source.peft_version,
            safetensors_format=source.safetensors_format,
            code_revision=source.code_revision,
        )
        session.add(copied)
        session.flush()
        session.add(
            AdapterImportAttempt(
                id=attempt_id,
                department_id=source.department_id,
                source_bundle_id=source_id,
                attempt_number=1,
                publication_attempt_id=publication_attempt_id,
                status="committed",
                ownership_manifest=manifest,
                code_revision=source.code_revision,
                validated_at=now,
                staged_at=now,
                published_at=now,
                committed_at=now,
                finished_at=now,
                version=attempt.version,
            )
        )
        session.flush()
        attempt_version = attempt.version
        copied.status = "committed"
        copied.authoritative_attempt_id = attempt_id
        copied.adapter_config_sha256 = source.adapter_config_sha256
        copied.adapter_config_byte_size = source.adapter_config_byte_size
        copied.adapter_model_sha256 = source.adapter_model_sha256
        copied.adapter_model_byte_size = source.adapter_model_byte_size
        copied.intake_manifest_sha256 = hashlib.sha256(raw).hexdigest()
        copied.intake_manifest_byte_size = len(raw)
        copied.tensor_dtype = source.tensor_dtype
        copied.tensor_count = source.tensor_count
        copied.tensor_element_count = source.tensor_element_count
        copied.tensor_payload_byte_size = source.tensor_payload_byte_size
        copied.committed_at = now
        copied.version = 2
    shared_authority = replace(
        authority,
        source_id=source_id,
        source_version=2,
        source_attempt_id=attempt_id,
        source_attempt_version=attempt_version,
        source_publication_attempt_id=publication_attempt_id,
    )
    if not enqueue:
        return source_id, None
    result = _enqueue(factory, shared_authority, apply=True)
    assert result.adapter_id is not None
    return source_id, result.adapter_id


def _prepare_shared_source_for_fixture_cleanup(factory, source_id: UUID) -> None:
    """Break only the temporary source/attempt cycle before fixture cleanup."""

    with factory.begin() as session:
        session.execute(
            update(AdapterImportSource)
            .where(AdapterImportSource.id == source_id)
            .values(
                status="staging",
                authoritative_attempt_id=None,
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
                committed_at=None,
                intake_manifest_sha256=None,
                intake_manifest_byte_size=None,
                adapter_config_sha256=None,
                adapter_config_byte_size=None,
                adapter_model_sha256=None,
                adapter_model_byte_size=None,
                tensor_dtype=None,
                tensor_count=None,
                tensor_element_count=None,
                tensor_payload_byte_size=None,
                purged_at=None,
                error_code=None,
                version=1,
            )
        )


def test_dry_run_is_read_only(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)

    result = _release(factory, authority, root, apply=False, versions=versions)

    assert not result.applied and not result.already_released
    assert _versions(factory, authority) == versions
    assert _release_audit_count(factory, authority) == 0


def test_replay_is_idempotent_and_does_not_duplicate_audit(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    first = _release(factory, authority, root, apply=True, versions=versions)
    current = _versions(factory, authority)

    replay = _release(factory, authority, root, apply=True, versions=current)

    assert first.applied and replay.already_released and not replay.applied
    assert _versions(factory, authority) == current
    assert _release_audit_count(factory, authority) == 1


def test_already_released_requires_its_single_success_audit(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    released = _release(factory, authority, root, apply=True, versions=versions)
    assert released.applied
    current = _versions(factory, authority)
    with factory.begin() as session:
        session.execute(
            delete(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == authority.department_id,
                PersistentAuditEvent.action == "adapter.upstream_dependency.release",
            )
        )

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=current)


def test_historical_terminal_purge_rows_are_not_materialized_or_locked(
    factory, engine, authority, tmp_path: Path
) -> None:
    """E-C probes active history and at most two successful candidates."""

    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory() as session:
        completed = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id,
                AdapterPurgeOperation.status == "completed",
            )
        )
        adapter = session.get(Adapter, versions[0])
        assert completed is not None and adapter is not None
        historical = [
            AdapterPurgeOperation(
                id=uuid4(),
                department_id=authority.department_id,
                adapter_id=completed.adapter_id,
                source_bundle_id=completed.source_bundle_id,
                requested_by_user_id=completed.requested_by_user_id,
                limit_value=completed.limit_value,
                item_limit_value=completed.item_limit_value,
                status="blocked",
                expected_adapter_version=completed.expected_adapter_version,
                expected_source_version=completed.expected_source_version,
                expected_source_attempt_version=completed.expected_source_attempt_version,
                expected_registry_attempt_version=completed.expected_registry_attempt_version,
                source_authoritative_attempt_id=completed.source_authoritative_attempt_id,
                source_publication_attempt_id=completed.source_publication_attempt_id,
                source_attempt_number=completed.source_attempt_number,
                registry_attempt_id=completed.registry_attempt_id,
                registry_publication_attempt_id=completed.registry_publication_attempt_id,
                registry_attempt_number=completed.registry_attempt_number,
                authority_snapshot=completed.authority_snapshot,
                eligible_item_count=2,
                completed_item_count=0,
                blocked_item_count=2,
                completed_at=datetime.now(UTC),
                version=1,
            )
            for _ in range(32)
        ]
        session.add_all(historical)
        session.commit()

    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        if "adapter_purge_operations" in statement:
            statements.append(statement.upper())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = _release(factory, authority, root, apply=False, versions=versions)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert not result.applied
    operation_queries = [statement for statement in statements if "SELECT" in statement]
    assert operation_queries
    assert all("LIMIT" in statement for statement in operation_queries)
    assert all("FOR UPDATE" not in statement for statement in operation_queries)


def test_stale_second_apply_cannot_duplicate_transition(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    first = _release(factory, authority, root, apply=True, versions=versions)

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)

    assert first.applied
    assert _release_audit_count(factory, authority) == 1


def test_concurrent_apply_has_one_transition_and_one_audit(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    barrier = Barrier(2)

    def apply_once():
        barrier.wait(timeout=10)
        try:
            return _release(factory, authority, root, apply=True, versions=versions)
        except ServiceError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _ignored: apply_once(), range(2)))

    applied = [outcome for outcome in outcomes if getattr(outcome, "applied", False)]
    conflicts = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, ServiceError) and outcome.status_code == 409
    ]
    assert len(applied) == 1
    assert len(conflicts) == 1
    assert _release_audit_count(factory, authority) == 1


def test_release_vs_real_eb_registration_has_no_deadlock(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    barrier = Barrier(2)

    def release_once():
        barrier.wait(timeout=10)
        try:
            return _release(factory, authority, root, apply=True, versions=versions)
        except BaseException as error:  # assert the concrete outcome below
            return error

    def purge_once():
        barrier.wait(timeout=10)
        try:
            return _purge(factory, authority, root, apply=True)
        except BaseException as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        release_future = executor.submit(release_once)
        purge_future = executor.submit(purge_once)
        release_outcome, purge_outcome = release_future.result(), purge_future.result()
    _assert_no_deadlock(release_outcome)
    _assert_no_deadlock(purge_outcome)
    assert not isinstance(release_outcome, BaseException)
    assert not isinstance(purge_outcome, BaseException)
    assert getattr(release_outcome, "applied", False)
    assert getattr(purge_outcome, "eligible_count", 0) == 0
    assert _release_audit_count(factory, authority) == 1


def test_release_vs_real_eb_finalization_has_no_deadlock(
    factory, authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)

    # Drive the real E-B worker through both item deletions, then hold its
    # durable operation immediately before finalization. The concurrent call
    # below invokes the real E-B finalization transaction.
    original_finalize = adapter_purge._finalize_operation
    monkeypatch.setattr(adapter_purge, "_finalize_operation", lambda *_args, **_kwargs: None)
    prepared = _purge(factory, authority, root, apply=True)
    assert prepared.applied_count == 2
    adapter_id, adapter_version, source_version, dependency_version = _versions(factory, authority)
    with factory() as session:
        operation = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id,
                AdapterPurgeOperation.status == "deleting",
            )
        )
        assert operation is not None
        operation_id = operation.id

    barrier = Barrier(2)

    def finalize_once():
        barrier.wait(timeout=10)
        try:
            return original_finalize(
                factory,
                data_dir=root,
                operation_id=operation_id,
                department_id=authority.department_id,
                actor_issuer=authority.issuer,
                actor_subject=authority.subject,
            )
        except BaseException as error:
            return error

    def release_once():
        barrier.wait(timeout=10)
        try:
            return _release(
                factory,
                authority,
                root,
                apply=True,
                versions=(adapter_id, adapter_version, source_version, dependency_version),
            )
        except BaseException as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        finalize_future = executor.submit(finalize_once)
        release_future = executor.submit(release_once)
        finalize_outcome, release_outcome = finalize_future.result(), release_future.result()
    _assert_no_deadlock(finalize_outcome)
    _assert_no_deadlock(release_outcome)
    assert not isinstance(finalize_outcome, BaseException)
    if isinstance(release_outcome, ServiceError):
        assert release_outcome.status_code == 409
    else:
        assert getattr(release_outcome, "applied", False)
    with factory() as session:
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert adapter is not None and source is not None
        assert adapter.status == "purged" and source.status == "purged"
    if isinstance(release_outcome, ServiceError):
        current = _versions(factory, authority)
        assert _release(factory, authority, root, apply=True, versions=current).applied
    assert _release_audit_count(factory, authority) == 1


def test_release_vs_phase12c_enqueue_shared_upstream_has_no_deadlock(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    shared_source_id, shared_adapter_id = _add_shared_active_dependency(
        factory, authority, enqueue=False
    )
    assert shared_adapter_id is None
    shared_authority = replace(authority, source_id=shared_source_id, source_version=2)
    barrier = Barrier(2)

    def release_once():
        barrier.wait(timeout=10)
        try:
            return _release(factory, authority, root, apply=True, versions=versions)
        except BaseException as error:
            return error

    def enqueue_once():
        barrier.wait(timeout=10)
        try:
            return _enqueue(factory, shared_authority, apply=True)
        except BaseException as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            release_future = executor.submit(release_once)
            enqueue_future = executor.submit(enqueue_once)
            release_outcome, enqueue_outcome = release_future.result(), enqueue_future.result()
        _assert_no_deadlock(release_outcome)
        _assert_no_deadlock(enqueue_outcome)
        assert not isinstance(release_outcome, BaseException)
        assert not isinstance(enqueue_outcome, BaseException)
        assert getattr(release_outcome, "applied", False)
        assert getattr(enqueue_outcome, "applied", False)
        with factory() as session:
            shared_adapter = session.get(Adapter, enqueue_outcome.adapter_id)
            assert shared_adapter is not None and shared_adapter.status == "queued"
            assert _has_active_adapter_dependency(
                session, authority.department_id, authority.training_job_id
            )
        assert _release_audit_count(factory, authority) == 1
    finally:
        _prepare_shared_source_for_fixture_cleanup(factory, shared_source_id)


@pytest.mark.parametrize("field", ("adapter", "source", "dependency"))
def test_expected_versions_fail_closed(factory, authority, tmp_path: Path, field: str) -> None:
    root = _storage(tmp_path)
    adapter_id, adapter_version, source_version, dependency_version = _complete_purge(
        factory, authority, root
    )
    versions = {
        "adapter": adapter_version,
        "source": source_version,
        "dependency": dependency_version,
    }
    versions[field] += 1

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(
            factory,
            authority,
            root,
            apply=True,
            versions=(adapter_id, versions["adapter"], versions["source"], versions["dependency"]),
        )

    assert _release_audit_count(factory, authority) == 0


def test_missing_eb_success_audit_fails_closed(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            delete(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == authority.department_id,
                PersistentAuditEvent.action == "adapter.purge",
            )
        )

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _release_audit_count(factory, authority) == 0


def test_completed_with_blocks_eb_operation_fails_closed(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            update(AdapterPurgeOperation)
            .where(AdapterPurgeOperation.department_id == authority.department_id)
            .values(status="completed_with_blocks", completed_item_count=1, blocked_item_count=1)
        )

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _release_audit_count(factory, authority) == 0


@pytest.mark.parametrize(
    "defect",
    (
        "missing_operation",
        "missing_reservation",
        "blocked_reservation",
        "incomplete_item",
        "missing_success_marker",
        "altered_tombstone_namespace",
    ),
)
def test_incomplete_eb_completion_evidence_fails_closed(
    factory, authority, tmp_path: Path, defect: str
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        operation = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id
            )
        )
        assert operation is not None
        if defect == "missing_operation":
            session.execute(
                delete(AdapterPurgeItem).where(AdapterPurgeItem.operation_id == operation.id)
            )
            session.execute(
                delete(AdapterPurgeReservation).where(
                    AdapterPurgeReservation.operation_id == operation.id
                )
            )
            session.delete(operation)
        elif defect == "missing_reservation":
            reservation = session.scalar(
                select(AdapterPurgeReservation).where(
                    AdapterPurgeReservation.operation_id == operation.id,
                    AdapterPurgeReservation.surface_type == "source_final",
                )
            )
            assert reservation is not None
            session.execute(
                delete(AdapterPurgeItem).where(AdapterPurgeItem.reservation_id == reservation.id)
            )
            session.delete(reservation)
        elif defect == "blocked_reservation":
            session.execute(
                update(AdapterPurgeReservation)
                .where(
                    AdapterPurgeReservation.operation_id == operation.id,
                    AdapterPurgeReservation.surface_type == "registry_final",
                )
                .values(
                    status="blocked",
                    completed_at=None,
                    blocked_at=datetime.now(UTC),
                    blocked_reason_code="purge_authority_changed",
                )
            )
        elif defect == "incomplete_item":
            session.execute(
                update(AdapterPurgeItem)
                .where(
                    AdapterPurgeItem.operation_id == operation.id,
                    AdapterPurgeItem.surface_type == "registry_final",
                )
                .values(status="registered", completed_at=None)
            )
        elif defect == "altered_tombstone_namespace":
            reservation = session.scalar(
                select(AdapterPurgeReservation).where(
                    AdapterPurgeReservation.operation_id == operation.id,
                    AdapterPurgeReservation.surface_type == "registry_final",
                )
            )
            assert reservation is not None
            item = session.scalar(
                select(AdapterPurgeItem).where(AdapterPurgeItem.reservation_id == reservation.id)
            )
            assert item is not None
            changed_namespace = {
                "surface_type": "registry_final",
                "department_id": str(authority.department_id),
                "resource_id": str(uuid4()),
                "item_id": str(item.id),
            }
            reservation.expected_tombstone_namespace = changed_namespace
            item.expected_tombstone_namespace = changed_namespace
        else:
            operation.success_audited_at = None

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _versions(factory, authority) == versions
    assert _release_audit_count(factory, authority) == 0


@pytest.mark.parametrize("row_name", ("adapter", "source"))
def test_adapter_and_source_must_both_remain_purged(
    factory, authority, tmp_path: Path, row_name: str
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        if row_name == "adapter":
            session.execute(
                update(Adapter)
                .where(Adapter.id == versions[0])
                .values(status="purge_pending", purged_at=None)
            )
        else:
            session.execute(
                update(AdapterImportSource)
                .where(AdapterImportSource.id == authority.source_id)
                .values(status="purge_pending", purged_at=None)
            )

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _release_audit_count(factory, authority) == 0


def test_altered_eb_snapshot_fails_closed(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            update(AdapterPurgeOperation)
            .where(AdapterPurgeOperation.department_id == authority.department_id)
            .values(authority_snapshot={})
        )

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _release_audit_count(factory, authority) == 0


def test_active_eb_operation_fails_closed(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            update(AdapterPurgeOperation)
            .where(AdapterPurgeOperation.department_id == authority.department_id)
            .values(status="deleting", completed_at=None)
        )

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _release_audit_count(factory, authority) == 0


def test_active_ea_final_item_fails_closed(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    adapter_id, adapter_version, _source_version, _dependency_version = versions
    with factory.begin() as session:
        registry_attempt = session.scalar(
            select(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id,
                AdapterRegistryAttempt.adapter_id == adapter_id,
            )
        )
        assert registry_attempt is not None
        operation = AdapterArtifactOperation(
            id=uuid4(),
            department_id=authority.department_id,
            requested_by_user_id=authority.admin_id,
            operation_type="reconcile",
            status="registered",
            limit_value=1,
            minimum_age_seconds=300,
            eligible_count=1,
            completed_count=0,
            blocked_count=0,
            version=1,
        )
        session.add(operation)
        session.flush()
        session.add(
            AdapterArtifactOperationItem(
                id=uuid4(),
                operation_id=operation.id,
                department_id=authority.department_id,
                surface_type="registry_final",
                adapter_id=adapter_id,
                registry_attempt_id=registry_attempt.id,
                publication_attempt_id=registry_attempt.publication_attempt_id,
                attempt_number=registry_attempt.attempt_number,
                expected_resource_version=adapter_version,
                expected_attempt_version=registry_attempt.version,
                status="registered",
                version=1,
            )
        )

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _release_audit_count(factory, authority) == 0


@pytest.mark.parametrize("surface_type", ("registry_final", "source_final"))
def test_reappeared_final_fails_closed(
    factory, authority, tmp_path: Path, surface_type: str
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    adapter_id, _adapter_version, _source_version, _dependency_version = versions
    resource_id = adapter_id if surface_type == "registry_final" else authority.source_id
    final = root / "adapters" / ("registry" if surface_type == "registry_final" else "imports")
    final = final / str(authority.department_id) / str(resource_id)
    final.mkdir(mode=0o700, parents=True)
    final.chmod(0o700)

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert final.is_dir()
    assert _release_audit_count(factory, authority) == 0


@pytest.mark.parametrize("surface_type", ("registry_final", "source_final"))
def test_residual_eb_tombstone_fails_closed(
    factory, authority, tmp_path: Path, surface_type: str
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    adapter_id, _adapter_version, _source_version, _dependency_version = versions
    resource_id = adapter_id if surface_type == "registry_final" else authority.source_id
    tombstone = (
        root
        / "adapters"
        / ".purge-deleting"
        / surface_type
        / str(authority.department_id)
        / str(resource_id)
        / str(uuid4())
    )
    tombstone.mkdir(mode=0o700, parents=True)
    tombstone.chmod(0o700)

    with pytest.raises(ServiceError, match="release authority changed"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert tombstone.is_dir()
    assert _release_audit_count(factory, authority) == 0


def test_release_storage_inspection_never_calls_artifact_mutation(
    factory, authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)

    def mutation_forbidden(*_args, **_kwargs):
        raise AssertionError("E-C must not mutate adapter artifacts")

    monkeypatch.setattr(
        AdapterPurgeArtifactStore,
        "move_verified_surface_to_tombstone",
        mutation_forbidden,
    )
    monkeypatch.setattr(
        AdapterPurgeArtifactStore,
        "unlink_committed_tombstone_entry",
        mutation_forbidden,
    )
    monkeypatch.setattr(
        AdapterPurgeArtifactStore,
        "remove_committed_tombstone_directory",
        mutation_forbidden,
    )
    result = _release(factory, authority, root, apply=True, versions=versions)

    assert result.applied
    assert _release_audit_count(factory, authority) == 1


def test_release_audit_failure_rolls_back_the_lifecycle_update(
    factory, authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)

    def audit_failure(*_args, **_kwargs):
        raise adapter_lifecycle_release.SQLAlchemyError("audit unavailable")

    monkeypatch.setattr(adapter_lifecycle_release, "append_mutation_audit", audit_failure)
    with pytest.raises(ServiceError, match="Database unavailable"):
        _release(factory, authority, root, apply=True, versions=versions)

    assert _versions(factory, authority) == versions
    assert _release_audit_count(factory, authority) == 0


def test_release_updates_the_closed_metadata_projection_and_lifts_own_fences(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    adapter_id, adapter_version, _source_version, _dependency_version = versions
    with factory() as session:
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        dependency = session.scalar(
            select(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.adapter_id == adapter_id
            )
        )
        assert adapter is not None and source is not None and dependency is not None
        before = _project(adapter, source, dependency)
        assert before.retention.upstream_dependency_status == "active"
        assert _has_active_adapter_dependency(
            session, authority.department_id, dependency.training_job_id
        )
        assert _has_active_dataset_dependency(
            session, authority.department_id, dependency.dataset_build_id
        )

    result = _release(factory, authority, root, apply=True, versions=versions)

    with factory() as session:
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        dependency = session.scalar(
            select(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.adapter_id == adapter_id
            )
        )
        assert adapter is not None and source is not None and dependency is not None
        after = _project(adapter, source, dependency)
        assert result.applied and after.version == adapter_version + 1
        assert after.retention.upstream_dependency_status == "released"
        assert after.retention.upstream_dependency_released_at is not None
        assert not _has_active_adapter_dependency(
            session, authority.department_id, dependency.training_job_id
        )
        assert not _has_active_dataset_dependency(
            session, authority.department_id, dependency.dataset_build_id
        )


def test_releasing_one_adapter_preserves_shared_job_and_dataset_fences(
    factory, authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    shared_source_id, shared_adapter_id = _add_shared_active_dependency(factory, authority)
    try:
        with factory() as session:
            original = session.scalar(
                select(AdapterUpstreamDependency).where(
                    AdapterUpstreamDependency.adapter_id == versions[0]
                )
            )
            shared = session.scalar(
                select(AdapterUpstreamDependency).where(
                    AdapterUpstreamDependency.adapter_id == shared_adapter_id
                )
            )
            assert original is not None and shared is not None
            assert _has_active_adapter_dependency(
                session, authority.department_id, original.training_job_id
            )
            assert _has_active_dataset_dependency(
                session, authority.department_id, original.dataset_build_id
            )

        result = _release(factory, authority, root, apply=True, versions=versions)

        with factory() as session:
            original = session.scalar(
                select(AdapterUpstreamDependency).where(
                    AdapterUpstreamDependency.adapter_id == versions[0]
                )
            )
            shared = session.scalar(
                select(AdapterUpstreamDependency).where(
                    AdapterUpstreamDependency.adapter_id == shared_adapter_id
                )
            )
            assert result.applied
            assert original is not None and original.status == "released"
            assert shared is not None and shared.status == "active"
            assert _has_active_adapter_dependency(
                session, authority.department_id, shared.training_job_id
            )
            assert _has_active_dataset_dependency(
                session, authority.department_id, shared.dataset_build_id
            )
    finally:
        _prepare_shared_source_for_fixture_cleanup(factory, shared_source_id)


@pytest.mark.parametrize("role", ("instructor", "student", "viewer"))
def test_non_admin_roles_are_denied(factory, authority, tmp_path: Path, role: str) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            Membership.__table__.update()
            .where(Membership.department_id == authority.department_id)
            .values(role=role)
        )

    with pytest.raises(ServiceError, match="Department access denied"):
        _release(factory, authority, root, apply=True, versions=versions)


@pytest.mark.parametrize("role", ("department_admin", "system_admin"))
def test_same_department_administrators_can_release(
    factory, authority, tmp_path: Path, role: str
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            Membership.__table__.update()
            .where(Membership.department_id == authority.department_id)
            .values(role=role)
        )

    assert _release(factory, authority, root, apply=True, versions=versions).applied


@pytest.mark.parametrize(
    ("field", "value"),
    (("status", "suspended"), ("expires_at", datetime(2000, 1, 1, tzinfo=UTC))),
)
def test_inactive_or_expired_membership_is_denied(
    factory, authority, tmp_path: Path, field: str, value
) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            Membership.__table__.update()
            .where(Membership.department_id == authority.department_id)
            .values({field: value})
        )

    with pytest.raises(ServiceError, match="Department access denied"):
        _release(factory, authority, root, apply=True, versions=versions)


def test_archived_department_is_denied(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            Department.__table__.update()
            .where(Department.id == authority.department_id)
            .values(status="archived")
        )

    with pytest.raises(ServiceError, match="Department access denied"):
        _release(factory, authority, root, apply=True, versions=versions)
    assert _release_audit_count(factory, authority) == 0


def test_system_admin_has_no_cross_department_bypass(factory, authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    versions = _complete_purge(factory, authority, root)
    with factory.begin() as session:
        session.execute(
            Membership.__table__.update()
            .where(Membership.department_id == authority.department_id)
            .values(role="system_admin")
        )
        foreign = Department(
            slug=f"phase12ec-foreign-{uuid4().hex[:12]}",
            display_name="Phase 12.1E-C foreign",
            status="active",
        )
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id

    try:
        with pytest.raises(ServiceError, match="Department access denied"):
            _release(
                factory,
                authority,
                root,
                apply=True,
                versions=versions,
                department_id=foreign_id,
            )
    finally:
        with factory.begin() as session:
            session.execute(delete(Department).where(Department.id == foreign_id))
