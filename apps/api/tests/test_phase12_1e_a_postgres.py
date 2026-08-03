"""PostgreSQL 16 integration coverage for Phase 12.1E-A reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from test_phase12_1c_integration import Authority, _enqueue, _seed_authority
from test_phase12_1d_postgres import _cleanup as cleanup_seed

from alembic import command
from app.adapter_artifact_maintenance import reconcile_adapter_artifacts
from app.adapter_registry_queue import claim_next_adapter, terminal_failure
from app.database import create_database_engine
from app.models import (
    Adapter,
    AdapterArtifactOperation,
    AdapterArtifactOperationItem,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    PersistentAuditEvent,
)

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
    value = create_database_engine(_database_url())
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(engine)


@pytest.fixture
def authority(factory):
    with factory() as session:
        value = _seed_authority(session)
    yield value
    _cleanup_test_rows(factory, value)
    cleanup_seed(factory, value)


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
    ):
        path = root / relative
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    return root


def _file(path: Path, value: bytes = b"partial") -> None:
    path.write_bytes(value)
    path.chmod(0o600)


def _cleanup_test_rows(factory: sessionmaker[Session], authority: Authority) -> None:
    """Remove reconciliation rows and restore the seed's closed source line."""

    with factory.begin() as session:
        now = datetime.now(UTC)
        source_attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert source_attempt is not None and isinstance(source_attempt.ownership_manifest, dict)
        manifest_bytes = (
            json.dumps(
                source_attempt.ownership_manifest,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        # Break the source-to-adapter claim before deleting the adapter row.
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == authority.department_id,
                AdapterImportSource.id == authority.source_id,
            )
            .values(
                status="committed",
                authoritative_attempt_id=authority.source_attempt_id,
                error_code=None,
                abandoned_at=None,
                committed_at=now,
                intake_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                intake_manifest_byte_size=len(manifest_bytes),
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
            )
        )
        session.execute(
            delete(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id
            )
        )
        session.execute(
            delete(AdapterArtifactOperation).where(
                AdapterArtifactOperation.department_id == authority.department_id
            )
        )
        session.execute(
            delete(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.department_id == authority.department_id
            )
        )
        session.execute(
            delete(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == authority.department_id
            )
        )
        session.execute(delete(Adapter).where(Adapter.department_id == authority.department_id))
        session.execute(
            update(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.department_id == authority.department_id,
                AdapterImportAttempt.id == authority.source_attempt_id,
            )
            .values(
                status="committed",
                error_code=None,
                committed_at=now,
                finished_at=now,
                cleanup_confirmed_at=None,
            )
        )
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == authority.department_id,
                AdapterImportSource.id == authority.source_id,
            )
            .values(
                status="committed",
                authoritative_attempt_id=authority.source_attempt_id,
                error_code=None,
                abandoned_at=None,
                committed_at=now,
                intake_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                intake_manifest_byte_size=len(manifest_bytes),
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
            )
        )
        session.execute(
            delete(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == authority.department_id,
                PersistentAuditEvent.action == "adapter.artifact.reconcile",
            )
        )


def _abandon_source(factory: sessionmaker[Session], authority: Authority) -> None:
    now = datetime.now(UTC)
    with factory.begin() as session:
        session.execute(
            update(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.department_id == authority.department_id,
                AdapterImportAttempt.id == authority.source_attempt_id,
            )
            .values(
                status="failed",
                error_code="adapter_source_publication_failed",
                committed_at=None,
                finished_at=now,
                cleanup_confirmed_at=None,
            )
        )
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == authority.department_id,
                AdapterImportSource.id == authority.source_id,
            )
            .values(
                status="abandoned",
                authoritative_attempt_id=None,
                error_code="adapter_source_publication_failed",
                abandoned_at=now,
                committed_at=None,
                intake_manifest_sha256=None,
                intake_manifest_byte_size=None,
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
            )
        )


def _source_stage(root: Path, authority: Authority) -> Path:
    stage = (
        root
        / "adapters"
        / ".staging"
        / "imports"
        / str(authority.department_id)
        / str(authority.source_id)
        / str(authority.source_attempt_id)
    )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    return stage


def _reconcile(factory, authority: Authority, root: Path, *, apply: bool, limit: int = 1):
    return reconcile_adapter_artifacts(
        factory,
        data_dir=root,
        department_id=authority.department_id,
        actor_issuer=authority.issuer,
        actor_subject=authority.subject,
        limit=limit,
        minimum_age_seconds=300,
        apply=apply,
    )


def test_real_dry_run_is_read_only(factory, authority: Authority, tmp_path: Path) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    result = _reconcile(factory, authority, root, apply=False)
    assert result.eligible_count == 1
    assert result.completed_count == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperation.id)).where(
                    AdapterArtifactOperation.department_id == authority.department_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(AdapterArtifactOperationItem.id)).where(
                    AdapterArtifactOperationItem.department_id == authority.department_id
                )
            )
            == 0
        )


def test_source_apply_commits_move_and_cleanup_once(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    stage = _source_stage(root, authority)
    result = _reconcile(factory, authority, root, apply=True)
    assert result.eligible_count == 1
    assert result.completed_count == 1
    assert not stage.exists()
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        assert attempt.status == "failed"
        operation = session.scalar(
            select(AdapterArtifactOperation).where(
                AdapterArtifactOperation.department_id == authority.department_id
            )
        )
        assert operation is not None and operation.status == "completed"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )
    second = _reconcile(factory, authority, root, apply=True)
    assert second.eligible_count == 0
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.artifact.reconcile",
                )
            )
            == 1
        )


def test_missing_after_verified_is_blocked_without_tombstone(
    factory, authority: Authority, tmp_path: Path, monkeypatch
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    from app import adapter_maintenance_artifacts as storage

    monkeypatch.setattr(
        storage.AdapterMaintenanceArtifactStore,
        "move_verified_surface_to_tombstone",
        lambda *_args, **_kwargs: None,
    )
    result = _reconcile(factory, authority, root, apply=True)
    assert result.blocked_count >= 1
    with factory() as session:
        rows = session.scalars(
            select(AdapterArtifactOperationItem).where(
                AdapterArtifactOperationItem.department_id == authority.department_id
            )
        ).all()
        assert any(row.status == "blocked" for row in rows)
        assert any(row.blocked_reason_code == "artifact_authority_changed" for row in rows)


def test_unknown_resource_tombstone_blocks_cleanup_confirmation(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    _abandon_source(factory, authority)
    _source_stage(root, authority)
    unknown = (
        root
        / "adapters"
        / ".deleting"
        / "source_stage"
        / str(authority.department_id)
        / str(uuid4())
        / str(uuid4())
    )
    unknown.mkdir(mode=0o700, parents=True)
    for path in (unknown.parent.parent, unknown.parent, unknown):
        path.chmod(0o700)
    result = _reconcile(factory, authority, root, apply=True)
    assert result.completed_count == 1
    assert result.blocked_count == 0
    with factory() as session:
        attempt = session.get(AdapterImportAttempt, authority.source_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is None


def test_registry_stage_uses_publication_attempt_path(
    factory, authority: Authority, tmp_path: Path
) -> None:
    root = _storage(tmp_path)
    enqueue = _enqueue(factory, authority, apply=True)
    claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None and enqueue.adapter_id is not None
    terminal_failure(factory, claim, "adapter_registry_publication_failed")
    stage = (
        root
        / "adapters"
        / ".staging"
        / "registry"
        / str(authority.department_id)
        / str(enqueue.adapter_id)
        / str(claim.publication_attempt_id)
    )
    stage.mkdir(mode=0o700, parents=True)
    for path in (stage.parent.parent, stage.parent, stage):
        path.chmod(0o700)
    _file(stage / "adapter_config.json")
    result = _reconcile(factory, authority, root, apply=True)
    assert result.completed_count == 1
    assert not stage.exists()
    with factory() as session:
        attempt = session.get(AdapterRegistryAttempt, claim.registry_attempt_id)
        assert attempt is not None and attempt.cleanup_confirmed_at is not None
        adapter = session.get(Adapter, enqueue.adapter_id)
        assert adapter is not None and adapter.status == "failed"


def test_physical_surface_indexes_reject_sibling_final_attempts(
    engine, factory, authority: Authority
) -> None:
    indexes = {
        row["name"]: row for row in inspect(engine).get_indexes("adapter_artifact_operation_items")
    }
    assert indexes["uq_adapter_artifact_item_active_source_stage"]["column_names"] == [
        "department_id",
        "surface_type",
        "source_bundle_id",
        "import_attempt_id",
    ]
    assert indexes["uq_adapter_artifact_item_active_registry_stage"]["column_names"] == [
        "department_id",
        "surface_type",
        "adapter_id",
        "publication_attempt_id",
    ]
    assert indexes["uq_adapter_artifact_item_active_source_final"]["column_names"] == [
        "department_id",
        "surface_type",
        "source_bundle_id",
    ]
    assert indexes["uq_adapter_artifact_item_active_registry_final"]["column_names"] == [
        "department_id",
        "surface_type",
        "adapter_id",
    ]

    now = datetime.now(UTC)
    sibling_attempt_id = uuid4()
    sibling_publication_id = uuid4()
    with factory.begin() as session:
        session.add(
            AdapterImportAttempt(
                id=sibling_attempt_id,
                department_id=authority.department_id,
                source_bundle_id=authority.source_id,
                attempt_number=2,
                publication_attempt_id=sibling_publication_id,
                status="failed",
                code_revision=authority.code_revision,
                error_code="adapter_source_publication_failed",
                finished_at=now,
                version=1,
            )
        )
        operation_one = AdapterArtifactOperation(
            id=uuid4(),
            department_id=authority.department_id,
            requested_by_user_id=authority.admin_id,
            operation_type="reconcile",
            status="registered",
            limit_value=1,
            minimum_age_seconds=300,
            eligible_count=1,
            version=1,
        )
        session.add(operation_one)
        session.flush()
        common = {
            "department_id": authority.department_id,
            "surface_type": "source_final",
            "source_bundle_id": authority.source_id,
            "adapter_id": None,
            "registry_attempt_id": None,
            "expected_resource_version": authority.source_version,
            "ownership_manifest": {},
            "status": "registered",
            "version": 1,
        }
        session.add(
            AdapterArtifactOperationItem(
                **common,
                id=uuid4(),
                operation_id=operation_one.id,
                import_attempt_id=authority.source_attempt_id,
                publication_attempt_id=authority.source_publication_attempt_id,
                attempt_number=1,
                expected_attempt_version=authority.source_attempt_version,
            )
        )
        session.flush()
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    AdapterArtifactOperationItem(
                        **common,
                        id=uuid4(),
                        operation_id=operation_one.id,
                        import_attempt_id=sibling_attempt_id,
                        publication_attempt_id=sibling_publication_id,
                        attempt_number=2,
                        expected_attempt_version=1,
                    )
                )
                session.flush()
