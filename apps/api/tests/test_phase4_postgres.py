"""PostgreSQL and API coverage for Phase 4 document isolation."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import jwt
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from alembic import command
from app.audit import AuditEvent, AuditSink
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.document_services import (
    ServiceError,
    admit_document_upload,
    finalize_document_upload,
)
from app.document_storage import DocumentStorage
from app.document_upload import StreamResult, UploadMetadata
from app.main import app
from app.models import Department, Document, Membership, PersistentAuditEvent, UserIdentity

pytestmark = pytest.mark.postgres
SECRET = "phase-4-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
ISSUER = "https://phase4.issuer.invalid"
AUDIENCE = "phase4-tests"
ADMIN = "phase4-admin"


@dataclass
class CollectingAuditSink(AuditSink):
    events: list[AuditEvent] = field(default_factory=list)

    def emit(self, event: AuditEvent) -> None:
        self.events.append(event)


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
    config = Config("alembic.ini")
    command.upgrade(config, "head")
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
    department = Department(slug=f"docs-{uuid4().hex[:8]}", display_name="Documents")
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


def _client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    maximum: int = 100,
    quota: int = 1_000,
) -> TestClient:
    (tmp_path / "uploads").mkdir(exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", _database_url())
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DEPTSLM_DOCUMENT_MAX_BYTES", str(maximum))
    monkeypatch.setenv("DEPTSLM_DEPARTMENT_DOCUMENT_QUOTA_BYTES", str(quota))
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_AUTH_MODE", "hs256")
    monkeypatch.setenv("DEPTSLM_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("DEPTSLM_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", SECRET)
    return TestClient(app)


def _upload(client: TestClient, department_id, body: bytes = b"hello"):
    return client.post(
        f"/departments/{department_id}/documents",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Disposition": 'attachment; filename="notes.txt"',
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Length": str(len(body)),
        },
        content=body,
    )


def test_migration_head_contains_document_schema(engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0002_phase4_documents"
        )
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='documents'"
                )
            ).scalars()
        )
    assert {"department_id", "uploaded_by_user_id", "sha256", "deleted_at"}.issubset(columns)
    assert "storage_path" not in columns and "content" not in columns


def test_upload_list_read_and_safe_response(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity, department, _ = _seed(db)
    with _client(monkeypatch, tmp_path) as client:
        created = _upload(client, department.id)
        listed = client.get(
            f"/departments/{department.id}/documents",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        read = client.get(
            f"/departments/{department.id}/documents/{created.json()['id']}",
            headers={"Authorization": f"Bearer {_token()}"},
        )

    assert created.status_code == 201
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert read.status_code == 200
    assert created.json()["uploaded_by_user_id"] == str(identity.id)
    assert {"sha256", "storage_path", "path", "deleted_by_user_id"}.isdisjoint(created.json())
    source = tmp_path / "uploads" / str(department.id) / created.json()["id"] / "source"
    assert source.read_bytes() == b"hello"
    db.expire_all()
    row = db.get(Document, UUID(created.json()["id"]))
    assert (
        row is not None
        and row.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert db.query(PersistentAuditEvent).filter_by(action="document.create").count() == 1


def test_upload_process_audit_excludes_sensitive_metadata_and_content(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, _ = _seed(db)
    sink = CollectingAuditSink()
    filename = "sensitive-department-filename.txt"
    body = b"private document body"
    disposition = f'attachment; filename="{filename}"'
    with _client(monkeypatch, tmp_path) as client:
        client.app.state.audit_sink = sink
        response = client.post(
            f"/departments/{department.id}/documents",
            headers={
                "Authorization": f"Bearer {_token()}",
                "Content-Disposition": disposition,
                "Content-Type": "text/plain",
                "Content-Length": str(len(body)),
            },
            content=body,
        )
    assert response.status_code == 201
    serialized = repr(sink.events)
    row = db.query(Document).one()
    for forbidden in (filename, disposition, body.decode(), row.sha256, SECRET, str(tmp_path)):
        assert forbidden not in serialized
    assert {event.action for event in sink.events}.issuperset(
        {"authenticate", "document.upload.admission", "document.upload.finalization"}
    )


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("system_admin", 201),
        ("department_admin", 201),
        ("instructor", 201),
        ("student", 403),
        ("viewer", 403),
    ],
)
def test_upload_role_matrix(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
    expected: int,
) -> None:
    _, department, _ = _seed(db, role=role)
    with _client(monkeypatch, tmp_path) as client:
        response = _upload(client, department.id)
    assert response.status_code == expected
    if expected != 201:
        assert not list((tmp_path / "uploads").rglob("*.part"))
        assert db.query(Document).count() == 0


@pytest.mark.parametrize(
    "role", ["system_admin", "department_admin", "instructor", "student", "viewer"]
)
def test_every_same_department_role_can_list_and_read_metadata(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
) -> None:
    identity, department, _ = _seed(db, role=role)
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=identity.id,
        original_filename="notes.txt",
        media_type="text/plain",
        byte_size=5,
        sha256="0" * 64,
    )
    db.add(document)
    db.commit()
    headers = {"Authorization": f"Bearer {_token()}"}
    with _client(monkeypatch, tmp_path) as client:
        listed = client.get(f"/departments/{department.id}/documents", headers=headers)
        read = client.get(f"/departments/{department.id}/documents/{document.id}", headers=headers)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 1
    assert read.status_code == 200


def test_cross_department_and_header_mismatch_fail_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, own, _ = _seed(db)
    other_identity, other, _ = _seed(db, subject="other-admin")
    other_document = Document(
        department_id=other.id,
        uploaded_by_user_id=other_identity.id,
        original_filename="other.txt",
        media_type="text/plain",
        byte_size=5,
        sha256="0" * 64,
    )
    db.add(other_document)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        cross = _upload(client, other.id)
        mismatch = client.post(
            f"/departments/{own.id}/documents",
            headers={
                "Authorization": f"Bearer {_token()}",
                "X-Department-ID": str(other.id),
                "Content-Disposition": 'attachment; filename="notes.txt"',
                "Content-Type": "text/plain",
                "Content-Length": "5",
            },
            content=b"hello",
        )
        cross_read = client.get(
            f"/departments/{other.id}/documents/{other_document.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
        indirect = client.get(
            f"/departments/{own.id}/documents/{other_document.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert cross.status_code == 403 and mismatch.status_code == 403
    assert cross_read.status_code == 403 and indirect.status_code == 404
    assert db.query(Document).count() == 1


@pytest.mark.parametrize(
    ("filename", "media_type", "body", "extra", "expected"),
    [
        ("paper.pdf", "application/pdf", b"not-pdf", {}, 415),
        ("notes.txt", "text/plain", b"a\x00b", {}, 415),
        ("notes.txt", "text/plain", b"\xff", {}, 415),
        ("notes.txt", "text/plain", b"hello", {"Content-Encoding": "gzip"}, 415),
        ("notes.txt", "text/plain", b"x" * 11, {}, 413),
    ],
)
def test_invalid_uploads_leave_no_metadata_or_staging(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    filename: str,
    media_type: str,
    body: bytes,
    extra: dict[str, str],
    expected: int,
) -> None:
    _, department, _ = _seed(db)
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": media_type,
        "Content-Length": str(len(body)),
        **extra,
    }
    with _client(monkeypatch, tmp_path, maximum=10, quota=100) as client:
        response = client.post(
            f"/departments/{department.id}/documents", headers=headers, content=body
        )
    assert response.status_code == expected
    assert db.query(Document).count() == 0
    assert not list((tmp_path / "uploads").rglob("*.part"))


def test_final_authorization_rechecks_revoked_membership_and_cleans_staging(
    db: Session, engine, tmp_path: Path
) -> None:
    _, department, membership = _seed(db)
    factory = create_session_factory(engine)
    scope = DepartmentRequestScope(DepartmentScope(department.id), audit_sink=CollectingAuditSink())
    principal = AuthenticatedPrincipal(ADMIN, ISSUER)
    admit_document_upload(factory, principal, scope)
    storage = DocumentStorage(_prepared_root(tmp_path))
    staged = storage.create_staging(scope.department, uuid4())
    staged.write(b"hello")
    staged.finish()
    membership.status = "revoked"
    db.commit()

    with pytest.raises(ServiceError) as captured:
        finalize_document_upload(
            factory,
            principal,
            scope,
            UploadMetadata("notes.txt", "text/plain", 5),
            StreamResult(5, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"),
            staged,
            100,
        )
    assert captured.value.status_code == 403
    assert not staged.staging_path.exists()
    assert db.query(Document).count() == 0


def _prepared_root(path: Path) -> Path:
    (path / "uploads").mkdir(exist_ok=True)
    return path


def test_quota_is_serialized_and_includes_soft_deleted_bytes(
    db: Session, engine, tmp_path: Path
) -> None:
    identity, department, _ = _seed(db)
    existing = Document(
        department_id=department.id,
        uploaded_by_user_id=identity.id,
        original_filename="old.txt",
        media_type="text/plain",
        byte_size=4,
        sha256="0" * 64,
        status="deleted",
        deleted_at=datetime.now(UTC),
        deleted_by_user_id=identity.id,
    )
    db.add(existing)
    db.commit()
    factory = create_session_factory(engine)
    root = _prepared_root(tmp_path)
    scope = DepartmentRequestScope(DepartmentScope(department.id))
    principal = AuthenticatedPrincipal(ADMIN, ISSUER)
    barrier = Barrier(2)

    def finalize_one(marker: bytes) -> int:
        staged = DocumentStorage(root).create_staging(scope.department, uuid4())
        staged.write(marker * 4)
        staged.finish()
        barrier.wait(timeout=5)
        try:
            finalize_document_upload(
                factory,
                principal,
                scope,
                UploadMetadata("notes.txt", "text/plain", 4),
                StreamResult(4, marker.hex().ljust(64, "0")[:64]),
                staged,
                10,
            )
            return 201
        except ServiceError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(finalize_one, b"a")
        second = executor.submit(finalize_one, b"b")
        results = sorted((first.result(timeout=10), second.result(timeout=10)))
    assert results == [201, 413]
    db.expire_all()
    assert db.query(Document).count() == 2
    assert not list((tmp_path / "uploads").rglob("*.part"))


def test_database_failure_after_move_compensates_file(
    db: Session, engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, department, _ = _seed(db)
    factory = create_session_factory(engine)
    root = _prepared_root(tmp_path)
    staged = DocumentStorage(root).create_staging(DepartmentScope(department.id), uuid4())
    staged.write(b"hello")
    staged.finish()

    def fail_audit(*_args, **_kwargs):
        raise SQLAlchemyError("simulated")

    monkeypatch.setattr("app.document_services.append_mutation_audit", fail_audit)
    with pytest.raises(ServiceError) as captured:
        finalize_document_upload(
            factory,
            AuthenticatedPrincipal(ADMIN, ISSUER),
            DepartmentRequestScope(DepartmentScope(department.id)),
            UploadMetadata("notes.txt", "text/plain", 5),
            StreamResult(5, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"),
            staged,
            100,
        )
    assert captured.value.status_code == 503
    assert db.query(Document).count() == 0
    assert not list((root / "uploads" / str(department.id)).glob("*/source"))


def test_delete_is_soft_scoped_admin_only_and_idempotent(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, department, membership = _seed(db)
    headers = {"Authorization": f"Bearer {_token()}"}
    with _client(monkeypatch, tmp_path) as client:
        created = _upload(client, department.id)
        source = tmp_path / "uploads" / str(department.id) / created.json()["id"] / "source"
        membership.role = "instructor"
        db.commit()
        denied = client.delete(
            f"/departments/{department.id}/documents/{created.json()['id']}", headers=headers
        )
        membership.role = "department_admin"
        db.commit()
        removed = client.delete(
            f"/departments/{department.id}/documents/{created.json()['id']}", headers=headers
        )
        repeated = client.delete(
            f"/departments/{department.id}/documents/{created.json()['id']}", headers=headers
        )
        hidden = client.get(
            f"/departments/{department.id}/documents/{created.json()['id']}", headers=headers
        )

    assert denied.status_code == 403
    assert removed.status_code == 200 and removed.json()["status"] == "deleted"
    assert repeated.status_code == 404 and hidden.status_code == 404
    assert source.read_bytes() == b"hello"
    db.expire_all()
    row = db.get(Document, UUID(created.json()["id"]))
    assert row is not None and row.deleted_at is not None and row.deleted_by_user_id is not None
    assert db.query(PersistentAuditEvent).filter_by(action="document.delete").count() == 1


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("system_admin", 200),
        ("department_admin", 200),
        ("instructor", 403),
        ("student", 403),
        ("viewer", 403),
    ],
)
def test_delete_role_matrix(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
    expected: int,
) -> None:
    identity, department, _ = _seed(db, role=role)
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=identity.id,
        original_filename="notes.txt",
        media_type="text/plain",
        byte_size=5,
        sha256="0" * 64,
    )
    db.add(document)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.delete(
            f"/departments/{department.id}/documents/{document.id}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == expected
    db.refresh(document)
    assert document.status == ("deleted" if expected == 200 else "stored")


def test_document_constraints_reject_invalid_lifecycle_and_checksum(db: Session) -> None:
    identity, department, _ = _seed(db)
    db.add(
        Document(
            department_id=department.id,
            uploaded_by_user_id=identity.id,
            original_filename="bad.txt",
            media_type="text/plain",
            byte_size=1,
            sha256="NOT-A-CHECKSUM",
            status="stored",
            deleted_at=datetime.now(UTC),
            deleted_by_user_id=identity.id,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
