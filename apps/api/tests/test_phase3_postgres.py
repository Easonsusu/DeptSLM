"""PostgreSQL integration coverage for Phase 3 boundaries."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import jwt
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from alembic import command
from app.admin import BootstrapError, bootstrap_department
from app.audit import AuditEvent, AuditSink, LoggingAuditSink
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import (
    DepartmentRequestScope,
    DepartmentScope,
    MembershipResolutionUnavailable,
)
from app.database import create_database_engine, create_session_factory
from app.main import app
from app.membership_resolver import SQLAlchemyMembershipResolver
from app.models import Department, Document, Membership, PersistentAuditEvent, UserIdentity
from app.services import (
    ADMIN_ROLES,
    ServiceError,
    authorize_transaction,
    update_department,
    update_membership,
)
from app.settings import Settings

pytestmark = pytest.mark.postgres
SECRET = "phase-3-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
ISSUER = "https://phase3.issuer.invalid"
AUDIENCE = "phase3-tests"
ADMIN = "opaque-admin"


@dataclass
class CollectingAuditSink(AuditSink):
    events: list[AuditEvent] = field(default_factory=list)

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _authorization_events(sink: CollectingAuditSink) -> list[AuditEvent]:
    return [event for event in sink.events if event.action == "authorize_department"]


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped in CI")
    pytest.skip("PostgreSQL integration database is unavailable")


@pytest.fixture(scope="module")
def engine():
    value = create_database_engine(_database_url())
    yield value
    value.dispose()


@pytest.fixture
def db(engine) -> Session:
    with Session(engine) as session:
        session.execute(delete(PersistentAuditEvent))
        session.execute(delete(Document))
        session.execute(delete(Membership))
        session.execute(delete(Department))
        session.execute(delete(UserIdentity))
        session.commit()
        yield session
        session.rollback()


def _seed(db: Session, *, subject: str = ADMIN, role: str = "department_admin"):
    identity = UserIdentity(issuer=ISSUER, subject=subject, status="active")
    department = Department(slug=f"dept-{uuid4().hex[:8]}", display_name="Department")
    db.add_all([identity, department])
    db.flush()
    membership = Membership(
        user_id=identity.id,
        department_id=department.id,
        role=role,
        status="active",
        created_by_user_id=identity.id,
    )
    db.add(membership)
    db.commit()
    return identity, department, membership


def _token(subject: str = ADMIN) -> str:
    return jwt.encode(
        {
            "sub": subject,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        SECRET,
        algorithm="HS256",
    )


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    (tmp_path / "uploads").mkdir(exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", _database_url())
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_AUTH_MODE", "hs256")
    monkeypatch.setenv("DEPTSLM_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("DEPTSLM_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", SECRET)
    return TestClient(app)


def test_00_migration_cycle_reaches_head(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert current == "0004_phase6_vector_indexing"
        assert {"user_identities", "departments", "memberships", "audit_events"}.issubset(
            set(
                connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname='public'")
                ).scalars()
            )
        )


@pytest.mark.parametrize("kind", ["identity", "department", "membership"])
def test_unique_constraints(db: Session, kind: str) -> None:
    identity, department, _ = _seed(db)
    if kind == "identity":
        db.add(UserIdentity(issuer=ISSUER, subject=ADMIN, status="active"))
    elif kind == "department":
        db.add(Department(slug=department.slug, display_name="Duplicate"))
    else:
        db.add(
            Membership(
                user_id=identity.id, department_id=department.id, role="viewer", status="active"
            )
        )
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_status", "invalid"),
        ("issuer", "   "),
        ("subject", "\t"),
        ("department_status", "invalid"),
        ("membership_status", "invalid"),
        ("membership_role", "owner"),
    ],
)
def test_check_constraints(db: Session, field: str, value: str) -> None:
    identity = UserIdentity(
        issuer=value if field == "issuer" else ISSUER,
        subject=value if field == "subject" else ADMIN,
        status=value if field == "user_status" else "active",
    )
    department = Department(
        slug="constraint-dept",
        display_name="Constraint",
        status=value if field == "department_status" else "active",
    )
    db.add_all([identity, department])
    with pytest.raises(IntegrityError):
        db.flush()
        if field in {"membership_role", "membership_status"}:
            second_identity = UserIdentity(issuer=ISSUER, subject="constraint-user")
            db.add(second_identity)
            db.flush()
            db.add(
                Membership(
                    user_id=second_identity.id,
                    department_id=department.id,
                    role=value if field == "membership_role" else "viewer",
                    status=value if field == "membership_status" else "active",
                )
            )
        db.commit()


def test_foreign_keys_and_timezone_timestamps(db: Session) -> None:
    _identity, department, membership = _seed(db)
    assert membership.created_at.utcoffset() is not None
    db.add(
        Membership(
            user_id=uuid4(),
            department_id=department.id,
            role="viewer",
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    ("user_status", "department_status", "membership_status", "expired", "allowed"),
    [
        ("active", "active", "active", False, True),
        ("suspended", "active", "active", False, False),
        ("revoked", "active", "active", False, False),
        ("active", "archived", "active", False, False),
        ("active", "active", "suspended", False, False),
        ("active", "active", "revoked", False, False),
        ("active", "active", "active", True, False),
    ],
)
def test_membership_resolver_state_boundaries(
    db: Session,
    engine,
    user_status: str,
    department_status: str,
    membership_status: str,
    expired: bool,
    allowed: bool,
) -> None:
    identity, department, membership = _seed(db)
    identity.status = user_status
    department.status = department_status
    membership.status = membership_status
    if expired:
        membership.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    resolver = SQLAlchemyMembershipResolver(create_session_factory(engine))
    result = resolver.resolve(AuthenticatedPrincipal(ADMIN, ISSUER), DepartmentScope(department.id))
    assert (result is not None) is allowed


def test_membership_resolver_rejects_wrong_identity_and_department(db: Session, engine) -> None:
    _, department, _ = _seed(db)
    resolver = SQLAlchemyMembershipResolver(create_session_factory(engine))
    wrong_subject = resolver.resolve(
        AuthenticatedPrincipal("wrong", ISSUER), DepartmentScope(department.id)
    )
    wrong_issuer = resolver.resolve(
        AuthenticatedPrincipal(ADMIN, "wrong"), DepartmentScope(department.id)
    )
    assert wrong_subject is None
    assert wrong_issuer is None
    assert resolver.resolve(AuthenticatedPrincipal(ADMIN, ISSUER), DepartmentScope(uuid4())) is None


def test_membership_resolver_database_failure() -> None:
    engine = create_database_engine("postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid")
    resolver = SQLAlchemyMembershipResolver(create_session_factory(engine))
    with pytest.raises(MembershipResolutionUnavailable):
        resolver.resolve(AuthenticatedPrincipal(ADMIN, ISSUER), DepartmentScope(uuid4()))
    engine.dispose()


def test_department_list_is_identity_scoped(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, own, _ = _seed(db)
    other_identity = UserIdentity(issuer=ISSUER, subject="other", status="active")
    other = Department(slug="other-dept", display_name="Other")
    db.add_all([other_identity, other])
    db.flush()
    db.add(
        Membership(
            user_id=other_identity.id,
            department_id=other.id,
            role="viewer",
            status="active",
        )
    )
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/departments", headers={"Authorization": f"Bearer {_token()}"})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(own.id)]


def test_allowed_department_read_emits_authorization_event(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, _ = _seed(db)
    sink = CollectingAuditSink()
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.get(
            f"/departments/{department.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 200
    decisions = _authorization_events(sink)
    assert [(event.result, event.reason_code) for event in decisions] == [
        ("allowed", "active_membership")
    ]


def test_allowed_mutation_emits_authorization_and_persistent_success(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, _ = _seed(db)
    sink = CollectingAuditSink()
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.patch(
            f"/departments/{department.id}",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"display_name": "Audited update"},
        )
    assert response.status_code == 200
    decisions = _authorization_events(sink)
    assert [(event.result, event.reason_code) for event in decisions] == [
        ("allowed", "active_membership")
    ]
    db.expire_all()
    assert db.query(PersistentAuditEvent).filter_by(action="department.update").count() == 1


def test_role_denial_emits_authorization_without_persistent_success(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, _ = _seed(db, role="viewer")
    sink = CollectingAuditSink()
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.patch(
            f"/departments/{department.id}",
            headers={"Authorization": f"Bearer {_token()}"},
            json={"display_name": "Denied update"},
        )
    assert response.status_code == 403
    decisions = _authorization_events(sink)
    assert [(event.result, event.reason_code) for event in decisions] == [("denied", "role_denied")]
    assert db.query(PersistentAuditEvent).count() == 0


@pytest.mark.parametrize(
    "stale_state",
    [
        "identity_suspended",
        "identity_revoked",
        "membership_suspended",
        "membership_revoked",
        "membership_expired",
        "department_archived",
    ],
)
def test_stale_route_authorization_emits_denial_without_persistent_success(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stale_state: str,
) -> None:
    identity, department, membership = _seed(db)
    if stale_state == "identity_suspended":
        identity.status = "suspended"
    elif stale_state == "identity_revoked":
        identity.status = "revoked"
    elif stale_state == "membership_suspended":
        membership.status = "suspended"
    elif stale_state == "membership_revoked":
        membership.status = "revoked"
    elif stale_state == "membership_expired":
        membership.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        department.status = "archived"
    db.commit()
    sink = CollectingAuditSink()

    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.get(
            f"/departments/{department.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 403
    decisions = _authorization_events(sink)
    assert [(event.result, event.reason_code) for event in decisions] == [
        ("denied", "membership_denied")
    ]
    assert db.query(PersistentAuditEvent).count() == 0


def test_cross_department_route_denial_emits_authorization_event(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed(db)
    _, other_department, _ = _seed(db, subject="other-admin")
    sink = CollectingAuditSink()
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.get(
            f"/departments/{other_department.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 403
    decisions = _authorization_events(sink)
    assert [(event.result, event.reason_code) for event in decisions] == [
        ("denied", "membership_denied")
    ]
    assert db.query(PersistentAuditEvent).count() == 0


def test_authorization_database_failure_emits_safe_unavailable_event(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, _ = _seed(db)
    sink = CollectingAuditSink()

    def fail_resolution(*_args, **_kwargs):
        raise SQLAlchemyError("SELECT secret FROM memberships AT database.internal")

    monkeypatch.setattr("app.services.resolve_transaction_membership", fail_resolution)
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.get(
            f"/departments/{department.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    decisions = _authorization_events(sink)
    assert [(event.result, event.reason_code) for event in decisions] == [
        ("denied", "membership_store_unavailable")
    ]
    assert "SELECT secret" not in repr(sink.events)
    assert "database.internal" not in repr(sink.events)
    assert db.query(PersistentAuditEvent).count() == 0


def test_production_authorization_audit_excludes_sensitive_data(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, department, _ = _seed(db)
    exact_token = _token()
    raw_body = "raw request body document content source content training content"
    sql_text = "SELECT * FROM memberships WHERE bearer_token = 'secret'"
    database_url = _database_url()
    sink = CollectingAuditSink()
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.patch(
            f"/departments/{department.id}",
            headers={"Authorization": f"Bearer {exact_token}"},
            json={"display_name": raw_body},
        )
    assert response.status_code == 200

    serialized_events = repr(sink.events)
    forbidden_values = (
        exact_token,
        SECRET,
        database_url,
        sql_text,
        "raw request body",
        "source content",
        "document content",
        "training content",
        "database.internal",
    )
    for forbidden in forbidden_values:
        assert forbidden not in serialized_events

    caplog.set_level("INFO", logger="deptslm.audit")
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = LoggingAuditSink()
        logged = client.get(
            f"/departments/{department.id}",
            headers={"Authorization": f"Bearer {exact_token}"},
        )
    assert logged.status_code == 200
    for forbidden in forbidden_values:
        assert forbidden not in caplog.text


def test_department_update_archive_and_audit(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, _ = _seed(db)
    headers = {"Authorization": f"Bearer {_token()}"}
    with _client(monkeypatch, tmp_path) as client:
        updated = client.patch(
            f"/departments/{department.id}", headers=headers, json={"display_name": "Updated"}
        )
        wrong = client.request(
            "DELETE",
            f"/departments/{department.id}",
            headers=headers,
            json={"confirm_slug": "wrong"},
        )
        archived = client.request(
            "DELETE",
            f"/departments/{department.id}",
            headers=headers,
            json={"confirm_slug": department.slug},
        )
        denied = client.get(f"/departments/{department.id}", headers=headers)
    assert updated.status_code == 200 and updated.json()["display_name"] == "Updated"
    assert wrong.status_code == 409
    assert archived.status_code == 200 and archived.json()["status"] == "archived"
    assert denied.status_code == 403
    db.expire_all()
    assert db.query(PersistentAuditEvent).count() == 2


def test_membership_crud_scope_and_last_admin(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, admin = _seed(db)
    _, other_department, other_membership = _seed(db, subject="other-admin")
    headers = {"Authorization": f"Bearer {_token()}"}
    with _client(monkeypatch, tmp_path) as client:
        created = client.post(
            f"/departments/{department.id}/memberships",
            headers=headers,
            json={"subject": "student-opaque", "role": "student"},
        )
        member_id = created.json()["id"]
        listed = client.get(f"/departments/{department.id}/memberships", headers=headers)
        hidden = client.get(
            f"/departments/{department.id}/memberships/{other_membership.id}", headers=headers
        )
        forbidden = client.post(
            f"/departments/{department.id}/memberships",
            headers=headers,
            json={"subject": "global", "role": "system_admin"},
        )
        changed = client.patch(
            f"/departments/{department.id}/memberships/{member_id}",
            headers=headers,
            json={"role": "viewer"},
        )
        revoked = client.delete(
            f"/departments/{department.id}/memberships/{member_id}", headers=headers
        )
        last_admin = client.delete(
            f"/departments/{department.id}/memberships/{admin.id}", headers=headers
        )
        cross = client.get(f"/departments/{other_department.id}", headers=headers)
    assert created.status_code == 201
    assert len(listed.json()["items"]) == 2
    assert hidden.status_code == 404 and forbidden.status_code == 403
    assert changed.json()["role"] == "viewer" and revoked.json()["status"] == "revoked"
    assert last_admin.status_code == 409 and cross.status_code == 403


@pytest.mark.parametrize(
    "stale_state",
    [
        "membership_revoked",
        "membership_demoted",
        "membership_expired",
        "identity_suspended",
        "department_archived",
    ],
)
def test_mutation_revalidates_stale_authorization(db: Session, engine, stale_state: str) -> None:
    identity, department, membership = _seed(db)
    principal = AuthenticatedPrincipal(ADMIN, ISSUER)
    scope = DepartmentScope(department.id)
    request_scope = DepartmentRequestScope(scope)
    with Session(engine) as authorization_session:
        stale = authorize_transaction(
            authorization_session, principal, request_scope, ADMIN_ROLES, lock=False
        )
        assert stale.membership.id == membership.id

    if stale_state == "membership_revoked":
        membership.status = "revoked"
    elif stale_state == "membership_demoted":
        membership.role = "viewer"
    elif stale_state == "membership_expired":
        membership.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif stale_state == "identity_suspended":
        identity.status = "suspended"
    else:
        department.status = "archived"
    db.commit()

    with Session(engine) as mutation_session:
        with pytest.raises(ServiceError) as captured:
            update_department(mutation_session, principal, request_scope, "Unauthorized change")
        mutation_session.rollback()
    assert captured.value.status_code == 403
    db.expire_all()
    assert db.query(PersistentAuditEvent).count() == 0


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
def test_only_effective_administrators_count_for_last_admin(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    identity_status: str,
    membership_status: str,
    expired: bool,
) -> None:
    _, department, admin = _seed(db)
    second_identity = UserIdentity(
        issuer=ISSUER, subject="ineffective-admin", status=identity_status
    )
    db.add(second_identity)
    db.flush()
    db.add(
        Membership(
            user_id=second_identity.id,
            department_id=department.id,
            role="department_admin",
            status=membership_status,
            expires_at=(datetime.now(UTC) - timedelta(minutes=1) if expired else None),
        )
    )
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.delete(
            f"/departments/{department.id}/memberships/{admin.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 409


def test_membership_noop_does_not_increment_or_audit(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, admin = _seed(db)
    original_version = admin.version
    headers = {"Authorization": f"Bearer {_token()}"}
    sink = CollectingAuditSink()
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        same = client.patch(
            f"/departments/{department.id}/memberships/{admin.id}",
            headers=headers,
            json={"role": "department_admin"},
        )
        empty = client.patch(
            f"/departments/{department.id}/memberships/{admin.id}",
            headers=headers,
            json={},
        )
        ambiguous = client.patch(
            f"/departments/{department.id}/memberships/{admin.id}",
            headers=headers,
            json={"expires_at": "2030-01-01T00:00:00Z", "clear_expiry": True},
        )
    assert same.status_code == 200 and same.json()["version"] == original_version
    assert empty.status_code == 422 and ambiguous.status_code == 422
    decisions = _authorization_events(sink)
    assert [(event.result, event.reason_code) for event in decisions] == [
        ("allowed", "active_membership")
    ]
    db.expire_all()
    assert db.query(PersistentAuditEvent).count() == 0


def test_same_department_system_admin_has_no_global_bypass(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, own_department, _ = _seed(db, role="system_admin")
    _, other_department, _ = _seed(db, subject="other-admin")
    headers = {"Authorization": f"Bearer {_token()}"}
    with _client(monkeypatch, tmp_path) as client:
        own = client.get(f"/departments/{own_department.id}", headers=headers)
        cross = client.get(f"/departments/{other_department.id}", headers=headers)
    assert own.status_code == 200
    assert cross.status_code == 403


def test_transaction_authorization_database_failure_is_generic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "uploads").mkdir()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid")
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_AUTH_MODE", "hs256")
    monkeypatch.setenv("DEPTSLM_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("DEPTSLM_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", SECRET)
    with TestClient(app) as client:
        response = client.get(
            f"/departments/{uuid4()}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    assert "invalid@" not in response.text


def test_concurrent_admin_demotions_retain_effective_administrator(db: Session, engine) -> None:
    first_identity, department, first_membership = _seed(db, subject="first-admin")
    second_identity = UserIdentity(issuer=ISSUER, subject="second-admin", status="active")
    db.add(second_identity)
    db.flush()
    second_membership = Membership(
        user_id=second_identity.id,
        department_id=department.id,
        role="department_admin",
        status="active",
        created_by_user_id=first_identity.id,
    )
    db.add(second_membership)
    db.commit()

    barrier = Barrier(2)
    scope = DepartmentRequestScope(DepartmentScope(department.id))

    def demote(actor_subject: str, target_id) -> int:
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                update_membership(
                    session,
                    AuthenticatedPrincipal(actor_subject, ISSUER),
                    scope,
                    target_id,
                    role=DepartmentRole.VIEWER,
                    status=None,
                    expires_at=None,
                    expiry_supplied=False,
                )
                session.commit()
                return 200
            except ServiceError as error:
                session.rollback()
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(demote, "first-admin", second_membership.id)
        second = executor.submit(demote, "second-admin", first_membership.id)
        results = sorted((first.result(timeout=10), second.result(timeout=10)))

    assert results == [200, 403]
    db.expire_all()
    effective = (
        db.query(Membership)
        .join(UserIdentity, Membership.user_id == UserIdentity.id)
        .filter(
            Membership.department_id == department.id,
            Membership.role.in_(("department_admin", "system_admin")),
            Membership.status == "active",
            UserIdentity.status == "active",
        )
        .count()
    )
    assert effective == 1
    assert db.query(PersistentAuditEvent).filter_by(action="membership.update").count() == 1


def test_bootstrap_is_atomic_and_conflict_safe(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "uploads").mkdir()
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", _database_url())
    monkeypatch.setenv("ENVIRONMENT", "test")
    settings = Settings.from_environment()
    department, membership = bootstrap_department(
        settings,
        slug="bootstrap-dept",
        display_name="Bootstrap",
        admin_issuer=ISSUER,
        admin_subject=ADMIN,
    )
    assert membership.role == "department_admin"
    with pytest.raises(BootstrapError):
        bootstrap_department(
            settings,
            slug="bootstrap-dept",
            display_name="Again",
            admin_issuer=ISSUER,
            admin_subject="another",
        )
    db.expire_all()
    assert db.get(Department, department.id) is not None
    assert db.query(Department).filter_by(slug="bootstrap-dept").count() == 1
    assert db.query(PersistentAuditEvent).filter_by(action="department.bootstrap").count() == 1
