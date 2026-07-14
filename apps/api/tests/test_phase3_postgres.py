"""PostgreSQL integration coverage for Phase 3 boundaries."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.admin import BootstrapError, bootstrap_department
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentScope, MembershipResolutionUnavailable
from app.database import create_database_engine, create_session_factory
from app.main import app
from app.membership_resolver import SQLAlchemyMembershipResolver
from app.models import Department, Membership, PersistentAuditEvent, UserIdentity
from app.settings import Settings

pytestmark = pytest.mark.postgres
SECRET = "phase-3-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
ISSUER = "https://phase3.issuer.invalid"
AUDIENCE = "phase3-tests"
ADMIN = "opaque-admin"


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
        assert current == "0001_phase3"
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
    [("department_status", "invalid"), ("membership_role", "owner")],
)
def test_check_constraints(db: Session, field: str, value: str) -> None:
    identity = UserIdentity(issuer=ISSUER, subject=ADMIN, status="active")
    department = Department(
        slug="constraint-dept",
        display_name="Constraint",
        status=value if field == "department_status" else "active",
    )
    db.add_all([identity, department])
    with pytest.raises(IntegrityError):
        db.flush()
        if field == "membership_role":
            db.add(
                Membership(
                    user_id=identity.id,
                    department_id=department.id,
                    role=value,
                    status="active",
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


def test_bootstrap_is_atomic_and_conflict_safe(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
