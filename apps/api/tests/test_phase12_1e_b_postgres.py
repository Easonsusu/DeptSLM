"""PostgreSQL 16 coverage for the Phase 12.1E-B adapter purge authority."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_phase12_1c_integration import (
    Authority,
    _enqueue,
    _seed_authority,
)
from test_phase12_1e_a_postgres import (
    _cleanup_test_rows,
    _registry_final,
    _registry_manifest_for_claim,
    _source_final,
)

from alembic import command
from app.adapter_maintenance_artifacts import AdapterPurgeArtifactStore
from app.adapter_purge import purge_adapter_artifacts
from app.adapter_registry_queue import claim_next_adapter
from app.adapter_source_artifacts import canonical_manifest_bytes
from app.database import create_database_engine
from app.models import (
    Adapter,
    AdapterArtifactOperation,
    AdapterArtifactReconciliationCursor,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterPurgeItem,
    AdapterPurgeOperation,
    AdapterPurgeReservation,
    AdapterRegistryAttempt,
    Department,
    Membership,
    PersistentAuditEvent,
    SftDatasetBuild,
    SftDatasetBuildAttempt,
    SftSourceBundle,
    TrainingJob,
    TrainingJobArtifactOperation,
    TrainingJobArtifactOperationItem,
    TrainingJobAttempt,
    TrainingJobPurgeReservation,
    UserIdentity,
)
from app.services import ServiceError

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped")
    pytest.skip("PostgreSQL integration database is unavailable")


@pytest.fixture(scope="module")
def engine():
    database_url = _database_url()
    value = create_database_engine(database_url)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    yield value
    value.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(engine)


@pytest.fixture
def authority(factory):
    with factory() as session:
        value = _seed_authority(session)
    # The shared Phase 12.1C seed uses a fixed test issuer.  Give this module
    # a unique issuer so stale rows from other isolated integration modules
    # can never be mistaken for this fixture's actor during cleanup.
    unique_issuer = f"https://phase12e-b-{uuid4().hex}.invalid"
    with factory.begin() as session:
        session.execute(
            UserIdentity.__table__.update()
            .where(UserIdentity.id == value.admin_id)
            .values(issuer=unique_issuer)
        )
    value = replace(value, issuer=unique_issuer)
    yield value
    with factory.begin() as session:
        session.execute(
            delete(AdapterPurgeItem).where(AdapterPurgeItem.department_id == value.department_id)
        )
        session.execute(
            delete(AdapterPurgeReservation).where(
                AdapterPurgeReservation.department_id == value.department_id
            )
        )
        session.execute(
            delete(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == value.department_id
            )
        )
        session.execute(
            delete(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == value.department_id,
                PersistentAuditEvent.action == "adapter.purge",
            )
        )
    # The apply test legitimately reaches the terminal purged state.  Clear
    # that lifecycle marker before reusing the Phase 12.1A restoration helper.
    with factory.begin() as session:
        session.execute(
            AdapterImportSource.__table__.update()
            .where(
                AdapterImportSource.department_id == value.department_id,
                AdapterImportSource.id == value.source_id,
            )
            .values(
                status="committed",
                purged_at=None,
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
            )
        )
    _cleanup_test_rows(factory, value)
    with factory.begin() as session:
        # The seed helpers intentionally create success/decision audit rows
        # for the actor.  Remove every isolated department audit row before
        # deleting that test identity; production audit retention is never
        # changed by the purge service.
        identity_ids = select(UserIdentity.id).where(UserIdentity.issuer == value.issuer)
        session.execute(
            delete(PersistentAuditEvent).where(
                (PersistentAuditEvent.department_id == value.department_id)
                | PersistentAuditEvent.actor_user_id.in_(identity_ids)
            )
        )
        session.execute(
            delete(Membership).where(
                Membership.user_id.in_(identity_ids)
                | Membership.created_by_user_id.in_(identity_ids)
            )
        )
        session.execute(
            delete(TrainingJobPurgeReservation).where(
                TrainingJobPurgeReservation.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobArtifactOperationItem).where(
                TrainingJobArtifactOperationItem.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobArtifactOperation).where(
                TrainingJobArtifactOperation.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobAttempt).where(
                TrainingJobAttempt.department_id == value.department_id
            )
        )
        session.execute(delete(TrainingJob).where(TrainingJob.department_id == value.department_id))
        session.execute(
            delete(SftDatasetBuildAttempt).where(
                SftDatasetBuildAttempt.department_id == value.department_id
            )
        )
        session.execute(
            delete(SftDatasetBuild).where(SftDatasetBuild.department_id == value.department_id)
        )
        session.execute(
            delete(SftSourceBundle).where(SftSourceBundle.department_id == value.department_id)
        )
        # Break the source/attempt composite foreign-key cycle before removing
        # the seeded source lineage and its importing identity.
        session.execute(
            AdapterImportSource.__table__.update()
            .where(
                AdapterImportSource.department_id == value.department_id,
                AdapterImportSource.id == value.source_id,
            )
            .values(
                status="staging",
                authoritative_attempt_id=None,
                committed_at=None,
                intake_manifest_sha256=None,
                intake_manifest_byte_size=None,
            )
        )
        session.execute(
            delete(AdapterImportAttempt).where(
                AdapterImportAttempt.department_id == value.department_id
            )
        )
        session.execute(
            delete(AdapterImportSource).where(
                AdapterImportSource.department_id == value.department_id
            )
        )
        session.execute(delete(UserIdentity).where(UserIdentity.issuer == value.issuer))
        session.execute(delete(Department).where(Department.id == value.department_id))


def _storage(root: Path) -> Path:
    for relative in (
        "adapters",
        "adapters/.staging",
        "adapters/.deleting",
        "adapters/imports",
        "adapters/registry",
        "adapters/.staging/imports",
        "adapters/.staging/registry",
        "adapters/.deleting/source_stage",
        "adapters/.deleting/source_final",
        "adapters/.deleting/registry_stage",
        "adapters/.deleting/registry_final",
        "adapters/.purge-deleting",
        "adapters/.purge-deleting/source_stage",
        "adapters/.purge-deleting/source_final",
        "adapters/.purge-deleting/registry_stage",
        "adapters/.purge-deleting/registry_final",
    ):
        path = root / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    return root


def _prepare_authority(factory: sessionmaker[Session], authority: Authority, root: Path) -> UUID:
    """Create exact source bytes before registry publication and purge."""

    config = b"{}"
    model = b"model"
    config_sha = hashlib.sha256(config).hexdigest()
    model_sha = hashlib.sha256(model).hexdigest()
    with factory.begin() as session:
        source = session.get(AdapterImportSource, authority.source_id)
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert source is not None and attempt is not None
        manifest = dict(attempt.ownership_manifest or {})
        manifest["files"] = {
            "adapter_config.json": {"sha256": config_sha, "byte_size": len(config)},
            "adapter_model.safetensors": {"sha256": model_sha, "byte_size": len(model)},
        }
        raw = canonical_manifest_bytes(manifest)
        attempt.ownership_manifest = manifest
        source.adapter_config_sha256 = config_sha
        source.adapter_config_byte_size = len(config)
        source.adapter_model_sha256 = model_sha
        source.adapter_model_byte_size = len(model)
        source.intake_manifest_sha256 = hashlib.sha256(raw).hexdigest()
        source.intake_manifest_byte_size = len(raw)
    _source_final(root, authority, manifest)
    enqueue = _enqueue(factory, authority, apply=True)
    claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None and enqueue.adapter_id == claim.id
    registry_manifest, registry_raw = _registry_manifest_for_claim(claim, config, model)
    _registry_final(root, authority, claim, registry_raw)
    now = datetime.now(UTC)
    with factory.begin() as session:
        attempt = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        adapter = session.get(Adapter, claim.id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert attempt is not None and adapter is not None and source is not None
        attempt.status = "succeeded"
        attempt.ownership_manifest = registry_manifest
        attempt.staged_at = now
        attempt.published_at = now
        attempt.finished_at = now
        attempt.worker_id = None
        attempt.claimed_at = None
        attempt.version += 1
        adapter.status = "validated"
        adapter.worker_id = None
        adapter.claim_token = None
        adapter.lease_expires_at = None
        adapter.validated_at = now
        adapter.finished_at = now
        adapter.registry_manifest_sha256 = hashlib.sha256(registry_raw).hexdigest()
        adapter.registry_adapter_config_sha256 = config_sha
        adapter.registry_adapter_config_byte_size = len(config)
        adapter.registry_adapter_model_sha256 = model_sha
        adapter.registry_adapter_model_byte_size = len(model)
        adapter.verified_governance_lineage = True
        adapter.verified_artifact_compatibility = True
        adapter.training_provenance_verified = False
        adapter.version += 1
        source.status = "consumed"
        source.consumed_at = now
        source.version += 1
    return claim.id


def _adapter_id(factory: sessionmaker[Session], authority: Authority) -> UUID:
    with factory() as session:
        value = session.scalar(
            select(Adapter.id).where(Adapter.department_id == authority.department_id)
        )
        assert value is not None
        return value


def _migration_config() -> Config:
    return Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))


def _migration_version(factory: sessionmaker[Session]) -> str:
    with factory() as session:
        value = session.scalar(text("SELECT version_num FROM alembic_version"))
        assert isinstance(value, str)
        return value


def test_phase12_1e_b_migration_chain_preserves_ea_cursor(factory, authority: Authority) -> None:
    """The E-B revision is layered on the merged E-A cursor revision."""

    config = _migration_config()
    cursor_id = uuid4()
    command.downgrade(config, "0012_phase12_adapter_reconciliation")
    try:
        assert _migration_version(factory) == "0012_phase12_adapter_reconciliation"
        command.upgrade(config, "0013_phase12_adapter_reconciliation_cursor")
        assert _migration_version(factory) == "0013_phase12_adapter_reconciliation_cursor"
        cursor_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        with factory.begin() as session:
            session.add(
                AdapterArtifactReconciliationCursor(
                    department_id=authority.department_id,
                    family="source",
                    status="failed",
                    cursor_created_at=cursor_created_at,
                    cursor_attempt_id=cursor_id,
                    version=4,
                )
            )
        command.upgrade(config, "0014_phase12_adapter_purge")
        assert _migration_version(factory) == "0014_phase12_adapter_purge"
        command.downgrade(config, "0013_phase12_adapter_reconciliation_cursor")
        assert _migration_version(factory) == "0013_phase12_adapter_reconciliation_cursor"
        with factory() as session:
            cursor = session.get(
                AdapterArtifactReconciliationCursor,
                (authority.department_id, "source", "failed"),
            )
            assert cursor is not None
            assert (cursor.cursor_created_at, cursor.cursor_attempt_id, cursor.version) == (
                cursor_created_at,
                cursor_id,
                4,
            )
        command.upgrade(config, "0014_phase12_adapter_purge")
        command.upgrade(config, "head")
        assert _migration_version(factory) == "0014_phase12_adapter_purge"
        with factory() as session:
            cursor = session.get(
                AdapterArtifactReconciliationCursor,
                (authority.department_id, "source", "failed"),
            )
            assert cursor is not None
            assert (cursor.cursor_created_at, cursor.cursor_attempt_id, cursor.version) == (
                cursor_created_at,
                cursor_id,
                4,
            )
    finally:
        command.upgrade(config, "head")
        with factory.begin() as session:
            session.execute(
                delete(AdapterArtifactReconciliationCursor).where(
                    AdapterArtifactReconciliationCursor.department_id == authority.department_id
                )
            )


def test_purge_preserves_ea_cursor_history_and_audit(
    factory, authority: Authority, tmp_path: Path
) -> None:
    """Purge owns only its exact surfaces, never E-A scheduler/history rows."""

    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    now = datetime.now(UTC)
    cursor_attempt_id = uuid4()
    operation_id = uuid4()
    audit_id = uuid4()
    with factory.begin() as session:
        session.add(
            AdapterArtifactReconciliationCursor(
                department_id=authority.department_id,
                family="registry",
                status="failed",
                cursor_created_at=now,
                cursor_attempt_id=cursor_attempt_id,
                version=7,
            )
        )
        session.add(
            AdapterArtifactOperation(
                id=operation_id,
                department_id=authority.department_id,
                requested_by_user_id=authority.admin_id,
                operation_type="reconcile",
                status="completed",
                limit_value=1,
                minimum_age_seconds=300,
                eligible_count=0,
                completed_count=0,
                blocked_count=0,
                completed_at=now,
                version=3,
            )
        )
        session.add(
            PersistentAuditEvent(
                id=audit_id,
                actor_subject=authority.subject,
                actor_user_id=authority.admin_id,
                department_id=authority.department_id,
                action="adapter.artifact.reconcile",
                resource_type="adapter_artifact_operation",
                resource_id=str(operation_id),
                result="allowed",
                reason_code="completed",
                created_at=now,
            )
        )
    result = _purge(factory, authority, root)
    assert result.applied_count == 2
    with factory() as session:
        cursor = session.get(
            AdapterArtifactReconciliationCursor,
            (authority.department_id, "registry", "failed"),
        )
        operation = session.get(AdapterArtifactOperation, operation_id)
        audit = session.get(PersistentAuditEvent, audit_id)
        assert cursor is not None and operation is not None and audit is not None
        assert (cursor.cursor_attempt_id, cursor.version) == (cursor_attempt_id, 7)
        assert (operation.status, operation.version, operation.completed_at) == (
            "completed",
            3,
            now,
        )
        assert (audit.action, audit.result, audit.reason_code) == (
            "adapter.artifact.reconcile",
            "allowed",
            "completed",
        )


def _purge(factory, authority: Authority, root: Path, *, apply: bool = True, department_id=None):
    return purge_adapter_artifacts(
        factory,
        data_dir=root,
        department_id=department_id or authority.department_id,
        adapter_id=_adapter_id(factory, authority),
        actor_issuer=authority.issuer,
        actor_subject=authority.subject,
        apply=apply,
    )


def test_dry_run_is_read_only_and_content_free(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    result = _purge(factory, authority, root, apply=False)
    assert result.eligible_count == 2
    assert result.applied_count == 0
    assert result.operation_id is None
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterPurgeOperation.id)).where(
                    AdapterPurgeOperation.department_id == authority.department_id
                )
            )
            == 0
        )
        adapter = session.scalar(
            select(Adapter).where(Adapter.department_id == authority.department_id)
        )
        source = session.get(AdapterImportSource, authority.source_id)
        assert adapter is not None and adapter.status == "validated"
        assert source is not None and source.status == "consumed"


def test_apply_purges_registry_then_source_and_replays_once(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    result = _purge(factory, authority, root)
    assert result.applied_count == 2
    with factory() as session:
        adapter = session.scalar(
            select(Adapter).where(Adapter.department_id == authority.department_id)
        )
        source = session.get(AdapterImportSource, authority.source_id)
        assert adapter is not None and adapter.status == "purged"
        assert source is not None and source.status == "purged"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.purge",
                )
            )
            == 1
        )
    replay = _purge(factory, authority, root)
    assert replay.eligible_count == 0
    assert replay.applied_count == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.purge",
                )
            )
            == 1
        )


def test_registry_failure_blocks_source_without_deleting_it(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    registry = root / "adapters" / "registry" / str(authority.department_id)
    for path in registry.iterdir():
        if path.is_dir():
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
    source = root / "adapters" / "imports" / str(authority.department_id) / str(authority.source_id)
    assert source.exists()
    result = _purge(factory, authority, root)
    assert result.blocked_count >= 1
    assert source.exists()
    with factory() as session:
        operation = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id
            )
        )
        assert operation is not None and operation.status == "completed_with_blocks"
        adapter = session.scalar(
            select(Adapter).where(Adapter.department_id == authority.department_id)
        )
        source_row = session.get(AdapterImportSource, authority.source_id)
        assert adapter is not None and adapter.status == "purge_pending"
        assert source_row is not None and source_row.status == "purge_pending"


def test_committed_move_intent_recovers_after_post_rename_crash(
    factory, authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after rename leaves durable move intent for exact recovery."""

    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    original = AdapterPurgeArtifactStore.move_verified_surface_to_tombstone
    raised = False

    def crash_after_move(self, inspected, *, expected_tombstone_namespace):
        nonlocal raised
        bound = original(
            self,
            inspected,
            expected_tombstone_namespace=expected_tombstone_namespace,
        )
        if not raised:
            raised = True
            raise RuntimeError("simulated process failure after rename")
        return bound

    monkeypatch.setattr(
        AdapterPurgeArtifactStore,
        "move_verified_surface_to_tombstone",
        crash_after_move,
    )
    with pytest.raises(RuntimeError, match="simulated process failure"):
        _purge(factory, authority, root)
    registry_tombstone = (
        root
        / "adapters"
        / ".purge-deleting"
        / "registry_final"
        / str(authority.department_id)
        / str(_adapter_id(factory, authority))
    )
    with factory() as session:
        registry_item = session.scalar(
            select(AdapterPurgeItem).where(
                AdapterPurgeItem.department_id == authority.department_id,
                AdapterPurgeItem.surface_type == "registry_final",
            )
        )
        assert registry_item is not None
        assert registry_item.status == "verified"
        reservation = session.scalar(
            select(AdapterPurgeReservation).where(
                AdapterPurgeReservation.id == registry_item.reservation_id
            )
        )
        assert reservation is not None and reservation.status == "deletion_authorized"
    assert registry_tombstone.is_dir()
    resumed = _purge(factory, authority, root)
    assert resumed.applied_count == 2
    with factory() as session:
        adapter = session.scalar(
            select(Adapter).where(Adapter.department_id == authority.department_id)
        )
        source = session.get(AdapterImportSource, authority.source_id)
        assert adapter is not None and adapter.status == "purged"
        assert source is not None and source.status == "purged"


def test_substituted_tombstone_after_crash_is_blocked_and_preserved(
    factory, authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery never adopts a substituted item-scoped tombstone."""

    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    original = AdapterPurgeArtifactStore.move_verified_surface_to_tombstone
    raised = False

    def crash_after_move(self, inspected, *, expected_tombstone_namespace):
        nonlocal raised
        bound = original(
            self,
            inspected,
            expected_tombstone_namespace=expected_tombstone_namespace,
        )
        if not raised:
            raised = True
            raise RuntimeError("simulated process failure after rename")
        return bound

    monkeypatch.setattr(
        AdapterPurgeArtifactStore,
        "move_verified_surface_to_tombstone",
        crash_after_move,
    )
    with pytest.raises(RuntimeError, match="simulated process failure"):
        _purge(factory, authority, root)
    adapter_id = _adapter_id(factory, authority)
    tombstone = (
        root
        / "adapters"
        / ".purge-deleting"
        / "registry_final"
        / str(authority.department_id)
        / str(adapter_id)
        / next(
            path.name
            for path in (
                root
                / "adapters"
                / ".purge-deleting"
                / "registry_final"
                / str(authority.department_id)
                / str(adapter_id)
            ).iterdir()
        )
    )
    # Keep the original outside the exact resource namespace. The retry must
    # therefore reject the replacement by the expected item's own identity,
    # not merely because a malformed sibling is present.
    parked = root / "parked-registry-tombstone"
    tombstone.rename(parked)
    tombstone.mkdir(mode=0o700)
    tombstone.chmod(0o700)
    result = _purge(factory, authority, root)
    assert result.blocked_count >= 1
    assert tombstone.is_dir() and parked.is_dir()
    with factory() as session:
        registry_item = session.scalar(
            select(AdapterPurgeItem).where(
                AdapterPurgeItem.department_id == authority.department_id,
                AdapterPurgeItem.surface_type == "registry_final",
            )
        )
        assert registry_item is not None
        registry_reservation = session.get(AdapterPurgeReservation, registry_item.reservation_id)
        operation = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id
            )
        )
        adapter = session.scalar(
            select(Adapter).where(Adapter.department_id == authority.department_id)
        )
        source = session.get(AdapterImportSource, authority.source_id)
        assert registry_item.status == "blocked"
        assert registry_reservation is not None and registry_reservation.status == "blocked"
        assert operation is not None and operation.status == "completed_with_blocks"
        assert adapter is not None and adapter.status == "purge_pending"
        assert source is not None and source.status == "purge_pending"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.purge",
                )
            )
            == 0
        )


def test_unknown_registry_sibling_after_post_rename_crash_is_resumable(
    factory, authority: Authority, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A canonical unknown sibling defers recovery without terminalizing it."""

    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    original = AdapterPurgeArtifactStore.move_verified_surface_to_tombstone
    raised = False

    def crash_after_registry_move(self, inspected, *, expected_tombstone_namespace):
        nonlocal raised
        bound = original(
            self,
            inspected,
            expected_tombstone_namespace=expected_tombstone_namespace,
        )
        if inspected.surface_type == "registry_final" and not raised:
            raised = True
            raise RuntimeError("simulated process failure after registry rename")
        return bound

    monkeypatch.setattr(
        AdapterPurgeArtifactStore,
        "move_verified_surface_to_tombstone",
        crash_after_registry_move,
    )
    with pytest.raises(RuntimeError, match="simulated process failure"):
        _purge(factory, authority, root)
    adapter_id = _adapter_id(factory, authority)
    with factory() as session:
        registry_item = session.scalar(
            select(AdapterPurgeItem).where(
                AdapterPurgeItem.department_id == authority.department_id,
                AdapterPurgeItem.surface_type == "registry_final",
            )
        )
        source_item = session.scalar(
            select(AdapterPurgeItem).where(
                AdapterPurgeItem.department_id == authority.department_id,
                AdapterPurgeItem.surface_type == "source_final",
            )
        )
        operation = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id
            )
        )
        assert registry_item is not None and source_item is not None and operation is not None
        registry_item_id = registry_item.id
        operation_id = operation.id
        durable_namespace = dict(registry_item.expected_tombstone_namespace or {})
        durable_identity = dict(registry_item.observed_identity or {})
        durable_plan = list(registry_item.deletion_plan or [])
        assert registry_item.status == "verified"
        assert source_item.status == "registered"
    expected = (
        root
        / "adapters"
        / ".purge-deleting"
        / "registry_final"
        / str(authority.department_id)
        / str(adapter_id)
        / str(registry_item_id)
    )
    unknown = expected.with_name(str(uuid4()))
    unknown.mkdir(mode=0o700)
    unknown.chmod(0o700)
    unknown_payload = unknown / "opaque.bin"
    unknown_payload.write_bytes(b"unowned")
    unknown_payload.chmod(0o600)
    with pytest.raises(ServiceError, match="external conflict resolution"):
        _purge(factory, authority, root)
    source_final = (
        root / "adapters" / "imports" / str(authority.department_id) / str(authority.source_id)
    )
    assert expected.is_dir()
    assert unknown.is_dir() and unknown_payload.read_bytes() == b"unowned"
    assert source_final.is_dir()
    with factory() as session:
        registry_item = session.get(AdapterPurgeItem, registry_item_id)
        source_item = session.scalar(
            select(AdapterPurgeItem).where(
                AdapterPurgeItem.operation_id == operation_id,
                AdapterPurgeItem.surface_type == "source_final",
            )
        )
        registry_reservation = session.scalar(
            select(AdapterPurgeReservation).where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.surface_type == "registry_final",
            )
        )
        source_reservation = session.scalar(
            select(AdapterPurgeReservation).where(
                AdapterPurgeReservation.operation_id == operation_id,
                AdapterPurgeReservation.surface_type == "source_final",
            )
        )
        operation = session.get(AdapterPurgeOperation, operation_id)
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert registry_item is not None and registry_reservation is not None
        assert source_item is not None and source_reservation is not None
        assert operation is not None and adapter is not None and source is not None
        assert registry_item.status == "verified"
        assert registry_reservation.status == "deletion_authorized"
        assert source_item.status == "registered"
        assert source_reservation.status == "registered"
        assert operation.status == "deleting"
        assert adapter.status == "purge_pending" and source.status == "purge_pending"
        assert registry_item.expected_tombstone_namespace == durable_namespace
        assert registry_item.observed_identity == durable_identity
        assert registry_item.deletion_plan == durable_plan
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.purge",
                )
            )
            == 0
        )
    # Model a reviewed out-of-band action that removes only the unowned
    # sibling. The exact same registered item resumes its durable move intent.
    unknown_payload.unlink()
    unknown.rmdir()
    resumed = _purge(factory, authority, root)
    assert resumed.applied_count == 2
    with factory() as session:
        operation = session.get(AdapterPurgeOperation, operation_id)
        registry_item = session.get(AdapterPurgeItem, registry_item_id)
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert operation is not None and operation.status == "completed"
        assert registry_item is not None and registry_item.status == "completed"
        assert adapter is not None and adapter.status == "purged"
        assert source is not None and source.status == "purged"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.purge",
                )
            )
            == 1
        )


@pytest.mark.parametrize("surface_type", ("registry_final", "source_final"))
def test_finalization_rejects_unknown_tombstone_for_each_exact_surface(
    factory,
    authority: Authority,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface_type: str,
) -> None:
    """Completed item rows cannot claim purge while a sibling remains."""

    root = _storage(tmp_path)
    _prepare_authority(factory, authority, root)
    original = AdapterPurgeArtifactStore.remove_committed_tombstone_directory
    injected: list[Path] = []

    def inject_after_expected_directory_removal(self, bound, *, allow_missing=False):
        result = original(self, bound, allow_missing=allow_missing)
        if bound.surface_type == surface_type and not injected:
            sibling = (
                root
                / "adapters"
                / ".purge-deleting"
                / surface_type
                / str(bound.department_id)
                / str(bound.resource_id)
                / str(uuid4())
            )
            sibling.mkdir(mode=0o700)
            sibling.chmod(0o700)
            payload = sibling / "opaque.bin"
            payload.write_bytes(b"unowned")
            payload.chmod(0o600)
            injected.extend((sibling, payload))
        return result

    monkeypatch.setattr(
        AdapterPurgeArtifactStore,
        "remove_committed_tombstone_directory",
        inject_after_expected_directory_removal,
    )
    with pytest.raises(ServiceError, match="external conflict resolution"):
        _purge(factory, authority, root)
    sibling, payload = injected
    assert sibling.is_dir() and payload.read_bytes() == b"unowned"
    adapter_id = _adapter_id(factory, authority)
    with factory() as session:
        operation = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id
            )
        )
        items = session.scalars(
            select(AdapterPurgeItem).where(
                AdapterPurgeItem.department_id == authority.department_id
            )
        ).all()
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert operation is not None and operation.status == "deleting"
        assert {item.status for item in items} == {"completed"}
        assert adapter is not None and adapter.status == "purge_pending"
        assert source is not None and source.status == "purge_pending"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.purge",
                )
            )
            == 0
        )
    payload.unlink()
    sibling.rmdir()
    resumed = _purge(factory, authority, root)
    assert resumed.applied_count == 0
    with factory() as session:
        operation = session.scalar(
            select(AdapterPurgeOperation).where(
                AdapterPurgeOperation.department_id == authority.department_id
            )
        )
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        assert operation is not None and operation.status == "completed"
        assert adapter is not None and adapter.status == "purged"
        assert source is not None and source.status == "purged"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.purge",
                )
            )
            == 1
        )
