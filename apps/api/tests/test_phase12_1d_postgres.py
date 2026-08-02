"""PostgreSQL integration coverage for Phase 12.1D metadata reads."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker
from test_phase12_1c_integration import Authority, _enqueue, _principal, _scope, _seed_authority

from alembic import command
from app.adapter_registry_queue import claim_next_adapter, terminal_failure
from app.adapter_registry_read_services import list_adapters, read_adapter
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine
from app.models import (
    Adapter,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    Department,
    Membership,
    PersistentAuditEvent,
    SftArtifactReconciliationOperation,
    SftArtifactReconciliationOperationItem,
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
    value = create_database_engine(_database_url())
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


@pytest.fixture
def factory(engine):
    return sessionmaker(engine)


def _cleanup(factory, value: Authority) -> None:
    with factory.begin() as session:
        session.execute(
            update(AdapterImportSource)
            .where(
                AdapterImportSource.department_id == value.department_id,
                AdapterImportSource.id == value.source_id,
            )
            .values(
                status="committed",
                claimed_adapter_id=None,
                claimed_at=None,
                consumed_at=None,
                error_code=None,
            )
        )
        session.execute(
            delete(PersistentAuditEvent).where(
                PersistentAuditEvent.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobPurgeReservation).where(
                TrainingJobPurgeReservation.department_id == value.department_id
            )
        )
        session.execute(
            delete(SftArtifactReconciliationOperationItem).where(
                SftArtifactReconciliationOperationItem.department_id == value.department_id
            )
        )
        session.execute(
            delete(SftArtifactReconciliationOperation).where(
                SftArtifactReconciliationOperation.department_id == value.department_id
            )
        )
        session.execute(
            delete(TrainingJobArtifactOperationItem).where(
                TrainingJobArtifactOperationItem.department_id == value.department_id
            )
        )
        session.execute(
            delete(AdapterUpstreamDependency).where(
                AdapterUpstreamDependency.department_id == value.department_id
            )
        )
        session.execute(
            delete(AdapterRegistryAttempt).where(
                AdapterRegistryAttempt.department_id == value.department_id
            )
        )
        session.execute(delete(Adapter).where(Adapter.department_id == value.department_id))
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
            update(AdapterImportSource)
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
        session.execute(
            delete(SftSourceBundle).where(SftSourceBundle.department_id == value.department_id)
        )
        session.execute(delete(Membership).where(Membership.department_id == value.department_id))
        session.execute(delete(UserIdentity).where(UserIdentity.issuer == value.issuer))
        session.execute(delete(Department).where(Department.id == value.department_id))


@pytest.fixture
def authority(factory):
    with factory() as session:
        value = _seed_authority(session)
    yield value
    _cleanup(factory, value)


def _read(factory, authority: Authority):
    with factory() as session:
        return list_adapters(
            session,
            _principal(authority),
            _scope(authority),
            limit=25,
            offset=0,
        )


def _add_actor(
    factory,
    authority: Authority,
    *,
    role: str,
    identity_status: str = "active",
    membership_status: str = "active",
    expires_at: datetime | None = None,
) -> AuthenticatedPrincipal:
    subject = f"read-{role}-{uuid4().hex}"
    with factory.begin() as session:
        identity = UserIdentity(
            issuer=authority.issuer,
            subject=subject,
            status=identity_status,
        )
        session.add(identity)
        session.flush()
        session.add(
            Membership(
                user_id=identity.id,
                department_id=authority.department_id,
                role=role,
                status=membership_status,
                expires_at=expires_at,
                created_by_user_id=authority.admin_id,
            )
        )
    return AuthenticatedPrincipal(subject, authority.issuer)


@pytest.mark.parametrize("role", ["system_admin", "department_admin", "instructor"])
def test_allowed_read_roles_list_and_detail(factory, authority: Authority, role: str) -> None:
    _enqueue(factory, authority, apply=True)
    principal = _add_actor(factory, authority, role=role)
    scope = _scope(authority)
    with factory() as session:
        rows = list_adapters(session, principal, scope, limit=25, offset=0)
        detail = read_adapter(session, principal, scope, rows[0].id)
    assert len(rows) == 1
    assert detail.id == rows[0].id
    assert detail.department_id == authority.department_id


@pytest.mark.parametrize("role", ["student", "viewer"])
def test_student_and_viewer_are_denied(factory, authority: Authority, role: str) -> None:
    principal = _add_actor(factory, authority, role=role)
    with pytest.raises(ServiceError) as error:
        _read_as(factory, authority, principal)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    ("identity_status", "membership_status", "expired"),
    [
        ("suspended", "active", False),
        ("revoked", "active", False),
        ("active", "suspended", False),
        ("active", "revoked", False),
        ("active", "active", True),
    ],
)
def test_inactive_or_expired_membership_is_denied(
    factory, authority: Authority, identity_status: str, membership_status: str, expired: bool
) -> None:
    principal = _add_actor(
        factory,
        authority,
        role="instructor",
        identity_status=identity_status,
        membership_status=membership_status,
        expires_at=datetime.now(UTC) - timedelta(seconds=1) if expired else None,
    )
    with pytest.raises(ServiceError) as error:
        _read_as(factory, authority, principal)
    assert error.value.status_code == 403


def _read_as(factory, authority: Authority, principal: AuthenticatedPrincipal):
    with factory() as session:
        return list_adapters(session, principal, _scope(authority), limit=25, offset=0)


def test_system_admin_has_no_cross_department_bypass(factory, authority: Authority) -> None:
    principal = _add_actor(factory, authority, role="system_admin")
    with factory.begin() as session:
        foreign = Department(slug=f"foreign-read-{uuid4().hex[:8]}", display_name="Foreign")
        session.add(foreign)
        session.flush()
        foreign_scope = DepartmentRequestScope(DepartmentScope(foreign.id))
        with pytest.raises(ServiceError) as error:
            list_adapters(session, principal, foreign_scope, limit=25, offset=0)
        assert error.value.status_code == 403
        session.delete(foreign)


def test_archived_department_is_denied(factory, authority: Authority) -> None:
    with factory.begin() as session:
        session.execute(
            update(Department)
            .where(Department.id == authority.department_id)
            .values(status="archived")
        )
    with pytest.raises(ServiceError) as error:
        _read(factory, authority)
    assert error.value.status_code == 403


def test_authorized_missing_adapter_is_safe_404(factory, authority: Authority) -> None:
    with factory() as session:
        with pytest.raises(ServiceError) as error:
            read_adapter(session, _principal(authority), _scope(authority), uuid4())
    assert error.value.status_code == 404
    assert error.value.detail == "Adapter not found"


def test_stable_created_at_id_ordering_and_offset(factory, authority: Authority) -> None:
    result = _enqueue(factory, authority, apply=True)
    with factory.begin() as session:
        session.execute(
            update(Adapter)
            .where(Adapter.id == result.adapter_id)
            .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
    with factory() as session:
        page = list_adapters(session, _principal(authority), _scope(authority), limit=1, offset=0)
        next_page = list_adapters(
            session, _principal(authority), _scope(authority), limit=1, offset=1
        )
    assert len(page) == 1
    assert next_page == ()


def test_empty_page_and_limit_offset(factory, authority: Authority) -> None:
    with factory() as session:
        page = list_adapters(session, _principal(authority), _scope(authority), limit=1, offset=10)
    assert page == ()


@pytest.mark.parametrize("status", ["queued", "running", "failed", "validation_failed"])
def test_lifecycle_states_are_projected(factory, authority: Authority, status: str) -> None:
    result = _enqueue(factory, authority, apply=True)
    if status == "running":
        claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
        assert claim is not None
    elif status in {"failed", "validation_failed"}:
        claim = claim_next_adapter(factory, uuid4(), 30, authority.code_revision)
        assert claim is not None
        if status == "failed":
            terminal_failure(factory, claim, "adapter_registry_publication_failed")
        else:
            now = datetime.now(UTC)
            with factory.begin() as session:
                session.execute(
                    update(Adapter)
                    .where(Adapter.id == result.adapter_id)
                    .values(
                        status="validation_failed",
                        worker_id=None,
                        claim_token=None,
                        claimed_at=None,
                        lease_expires_at=None,
                        finished_at=now,
                        error_code="adapter_config_invalid",
                        version=Adapter.version + 1,
                    )
                )
                session.execute(
                    update(AdapterRegistryAttempt)
                    .where(AdapterRegistryAttempt.adapter_id == result.adapter_id)
                    .values(
                        status="validation_failed",
                        worker_id=None,
                        claimed_at=None,
                        finished_at=now,
                        error_code="adapter_config_invalid",
                        version=AdapterRegistryAttempt.version + 1,
                    )
                )
    with factory() as session:
        row = read_adapter(session, _principal(authority), _scope(authority), result.adapter_id)
    assert row.status == status
    assert row.retention.source_status == "claimed"
    assert row.retention.upstream_dependency_status == "active"


def test_source_and_dependency_status_are_current_metadata(factory, authority: Authority) -> None:
    result = _enqueue(factory, authority, apply=True)
    with factory.begin() as session:
        session.execute(
            update(AdapterImportSource)
            .where(AdapterImportSource.id == authority.source_id)
            .values(status="consumed", consumed_at=datetime.now(UTC))
        )
    with factory() as session:
        row = read_adapter(session, _principal(authority), _scope(authority), result.adapter_id)
    assert row.retention.source_status == "consumed"
    assert row.retention.source_consumed_at is not None


def test_released_dependency_on_non_purged_adapter_fails_closed(
    factory, authority: Authority
) -> None:
    result = _enqueue(factory, authority, apply=True)
    with factory.begin() as session:
        session.execute(
            update(AdapterUpstreamDependency)
            .where(AdapterUpstreamDependency.adapter_id == result.adapter_id)
            .values(status="released", released_at=datetime.now(UTC))
        )
    with factory() as session:
        with pytest.raises(ServiceError) as error:
            read_adapter(session, _principal(authority), _scope(authority), result.adapter_id)
    assert error.value.status_code == 503
    assert error.value.detail == "Adapter metadata unavailable"


def test_reads_do_not_change_versions_or_append_mutation_audit(
    factory, authority: Authority
) -> None:
    result = _enqueue(factory, authority, apply=True)
    with factory() as session:
        before = session.execute(
            select(Adapter.version, AdapterImportSource.version, AdapterUpstreamDependency.version)
            .join(AdapterImportSource, AdapterImportSource.id == Adapter.source_bundle_id)
            .join(AdapterUpstreamDependency, AdapterUpstreamDependency.adapter_id == Adapter.id)
            .where(Adapter.id == result.adapter_id)
        ).one()
        audits_before = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.department_id == authority.department_id
            )
        )
        read_adapter(session, _principal(authority), _scope(authority), result.adapter_id)
        after = session.execute(
            select(Adapter.version, AdapterImportSource.version, AdapterUpstreamDependency.version)
            .join(AdapterImportSource, AdapterImportSource.id == Adapter.source_bundle_id)
            .join(AdapterUpstreamDependency, AdapterUpstreamDependency.adapter_id == Adapter.id)
            .where(Adapter.id == result.adapter_id)
        ).one()
        audits_after = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.department_id == authority.department_id
            )
        )
    assert before == after
    assert audits_before == audits_after


def test_missing_authority_is_rejected_by_closed_projection(factory, authority: Authority) -> None:
    from test_adapter_registry_read_services import _authority_rows

    from app.adapter_registry_read_services import _project

    adapter, source, _dependency = _authority_rows()
    with pytest.raises(RuntimeError):
        _project(adapter, source, None)
