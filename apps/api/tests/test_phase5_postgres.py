"""PostgreSQL 16 integration coverage for the Phase 5 extraction boundary."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import deptslm_worker.pipeline as pipeline_module
import jwt
import pytest
from alembic.config import Config
from deptslm_worker.chunking import Chunk
from deptslm_worker.pipeline import process_job
from deptslm_worker.queue import (
    Publication,
    QueueError,
    claim_next,
    fail_owned,
    finalize_success,
    heartbeat,
)
from deptslm_worker.settings import WorkerSettings
from deptslm_worker.storage import ExtractionStorage
from fastapi.testclient import TestClient
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.authorization import DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.main import app
from app.models import (
    Department,
    Document,
    DocumentChunk,
    DocumentExtraction,
    Membership,
    PersistentAuditEvent,
    UserIdentity,
)

pytestmark = pytest.mark.postgres
SECRET = "phase-5-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
ISSUER = "https://phase5.issuer.invalid"
AUDIENCE = "phase5-tests"
SUBJECT = "phase5-admin"


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
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


@pytest.fixture
def db(engine) -> Session:
    with Session(engine) as session:
        session.execute(delete(DocumentChunk))
        session.execute(delete(DocumentExtraction))
        session.execute(delete(PersistentAuditEvent))
        session.execute(delete(Document))
        session.execute(delete(Membership))
        session.execute(delete(Department))
        session.execute(delete(UserIdentity))
        session.commit()
        yield session
        session.rollback()


def _seed(db: Session, *, role: str = "department_admin", subject: str = SUBJECT):
    identity = UserIdentity(issuer=ISSUER, subject=subject, status="active")
    department = Department(slug=f"extract-{uuid4().hex[:8]}", display_name="Extraction")
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


def _document(db: Session, identity: UserIdentity, department: Department, payload=b"hello"):
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=identity.id,
        original_filename="notes.txt",
        media_type="text/plain",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    db.add(document)
    db.commit()
    return document


def _queued(db: Session, identity, department, document, *, attempt=1):
    extraction = DocumentExtraction(
        department_id=department.id,
        document_id=document.id,
        requested_by_user_id=identity.id,
        status="queued",
        pipeline_version="phase5-extraction-v1",
        normalization_version="phase5-normalization-v1",
        chunking_version="phase5-character-chunker-v1",
        source_sha256=document.sha256,
        source_byte_size=document.byte_size,
        attempt_number=attempt,
    )
    db.add(extraction)
    db.commit()
    return extraction


def _finish(extraction: DocumentExtraction, status: str) -> None:
    now = datetime.now(UTC)
    extraction.status = status
    if status == "succeeded":
        extraction.worker_id = uuid4()
        extraction.claim_token = uuid4()
        extraction.claimed_at = now
        extraction.started_at = now
        extraction.finished_at = now
        extraction.parser_name = "python-utf8"
        extraction.parser_version = "3.12"
        extraction.normalized_sha256 = "1" * 64
        extraction.normalized_byte_size = 5
        extraction.output_byte_size = 20
        extraction.chunk_count = 1
    elif status in {"failed", "cancelled"}:
        extraction.finished_at = now
        extraction.error_code = "parser_failed" if status == "failed" else "document_unavailable"


def _token(subject: str = SUBJECT) -> str:
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
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    (tmp_path / "extracted_text").mkdir(exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", _database_url())
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_AUTH_MODE", "hs256")
    monkeypatch.setenv("DEPTSLM_AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("DEPTSLM_AUTH_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("DEPTSLM_AUTH_SECRET", SECRET)
    return TestClient(app)


def _headers(subject: str = SUBJECT) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


def test_00_migration_upgrade_cycle_and_metadata_only_schema(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0002_phase4_documents")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT to_regclass('document_extractions')")).scalar() is None
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0003_phase5_extraction"
        )
        extraction_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='document_extractions'"
                )
            ).scalars()
        )
        chunk_columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='document_chunks'"
                )
            ).scalars()
        )
    assert {"department_id", "document_id", "claim_token", "source_sha256"}.issubset(
        extraction_columns
    )
    assert {"extraction_id", "char_start", "page_start", "line_start"}.issubset(chunk_columns)
    assert {"text", "content", "path", "original_filename"}.isdisjoint(
        extraction_columns | chunk_columns
    )
    assert "fk_extraction_document_scope" in {
        item["name"] for item in inspect(engine).get_foreign_keys("document_extractions")
    }
    assert all(
        item["options"].get("ondelete") == "RESTRICT"
        for table in ("document_extractions", "document_chunks")
        for item in inspect(engine).get_foreign_keys(table)
    )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda row: setattr(row, "status", "unknown"),
        lambda row: (
            setattr(row, "status", "running"),
            setattr(row, "started_at", datetime.now(UTC)),
        ),
        lambda row: (
            setattr(row, "status", "failed"),
            setattr(row, "finished_at", datetime.now(UTC)),
            setattr(row, "error_code", "raw exception"),
        ),
        lambda row: setattr(row, "source_sha256", "bad"),
    ],
)
def test_extraction_constraints_reject_invalid_lifecycle(db: Session, mutator) -> None:
    identity, department, _ = _seed(db)
    document = _document(db, identity, department)
    row = _queued(db, identity, department, document)
    mutator(row)
    with pytest.raises(IntegrityError):
        db.commit()


def test_composite_scope_constraints_reject_cross_department_rows(db: Session) -> None:
    identity, department, _ = _seed(db)
    document = _document(db, identity, department)
    other_identity, other, _ = _seed(db, subject="other")
    extraction = DocumentExtraction(
        department_id=other.id,
        document_id=document.id,
        requested_by_user_id=other_identity.id,
        status="queued",
        pipeline_version="phase5-extraction-v1",
        normalization_version="phase5-normalization-v1",
        chunking_version="phase5-character-chunker-v1",
        source_sha256=document.sha256,
        source_byte_size=document.byte_size,
        attempt_number=1,
    )
    db.add(extraction)
    with pytest.raises(IntegrityError):
        db.commit()


def test_chunk_constraints_reject_scope_duplicates_and_mixed_provenance(db: Session) -> None:
    identity, department, _ = _seed(db)
    document = _document(db, identity, department)
    extraction = _queued(db, identity, department, document)
    _finish(extraction, "succeeded")
    first = DocumentChunk(
        department_id=department.id,
        document_id=document.id,
        extraction_id=extraction.id,
        ordinal=0,
        char_start=0,
        char_end=5,
        byte_size=5,
        content_sha256="2" * 64,
        provenance_kind="line",
        line_start=1,
        line_end=1,
    )
    db.add(first)
    db.commit()
    duplicate = DocumentChunk(
        department_id=department.id,
        document_id=document.id,
        extraction_id=extraction.id,
        ordinal=0,
        char_start=0,
        char_end=5,
        byte_size=5,
        content_sha256="2" * 64,
        provenance_kind="page",
        page_start=1,
        page_end=1,
        line_start=1,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()


def test_partial_uniqueness_for_active_and_succeeded_results(db: Session) -> None:
    identity, department, _ = _seed(db)
    document = _document(db, identity, department)
    first = _queued(db, identity, department, document)
    first_id = first.id
    db.add(
        DocumentExtraction(
            department_id=department.id,
            document_id=document.id,
            requested_by_user_id=identity.id,
            status="queued",
            pipeline_version="phase5-extraction-v1",
            normalization_version="phase5-normalization-v1",
            chunking_version="phase5-character-chunker-v1",
            source_sha256=document.sha256,
            source_byte_size=document.byte_size,
            attempt_number=2,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    first = db.get(DocumentExtraction, first_id)
    _finish(first, "succeeded")
    db.commit()
    second = _queued(db, identity, department, document, attempt=2)
    _finish(second, "succeeded")
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("system_admin", 202),
        ("department_admin", 202),
        ("instructor", 202),
        ("student", 403),
        ("viewer", 403),
    ],
)
def test_enqueue_role_matrix_and_safe_schema(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
    expected: int,
) -> None:
    identity, department, _ = _seed(db, role=role)
    document = _document(db, identity, department)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            f"/departments/{department.id}/documents/{document.id}/extractions",
            headers=_headers(),
        )
    assert response.status_code == expected
    if expected == 202:
        payload = response.json()
        assert payload["status"] == "queued" and payload["attempt_number"] == 1
        assert {
            "requested_by_user_id",
            "worker_id",
            "claim_token",
            "lease_expires_at",
            "source_sha256",
            "normalized_sha256",
            "path",
            "text",
        }.isdisjoint(payload)
        assert (
            db.query(PersistentAuditEvent).filter_by(action="document.extraction.enqueue").count()
            == 1
        )
    else:
        assert db.query(DocumentExtraction).count() == 0


def test_cross_department_system_admin_and_auth_challenges_fail_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _identity, _own, _ = _seed(db, role="system_admin")
    other_identity, other, _ = _seed(db, subject="other-admin")
    document = _document(db, other_identity, other)
    with _client(monkeypatch, tmp_path) as client:
        unauthenticated = client.post(
            f"/departments/{other.id}/documents/{document.id}/extractions"
        )
        denied = client.post(
            f"/departments/{other.id}/documents/{document.id}/extractions",
            headers=_headers(),
        )
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    assert denied.status_code == 403 and "www-authenticate" not in denied.headers
    assert db.query(DocumentExtraction).count() == 0


def test_enqueue_conflicts_and_deleted_document_is_hidden(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity, department, _ = _seed(db)
    document = _document(db, identity, department)
    with _client(monkeypatch, tmp_path) as client:
        first = client.post(
            f"/departments/{department.id}/documents/{document.id}/extractions",
            headers=_headers(),
        )
        duplicate = client.post(
            f"/departments/{department.id}/documents/{document.id}/extractions",
            headers=_headers(),
        )
        deleted = client.delete(
            f"/departments/{department.id}/documents/{document.id}", headers=_headers()
        )
        hidden = client.post(
            f"/departments/{department.id}/documents/{document.id}/extractions",
            headers=_headers(),
        )
    assert first.status_code == 202 and duplicate.status_code == 409
    assert deleted.status_code == 200 and hidden.status_code == 404
    db.expire_all()
    extraction = db.query(DocumentExtraction).one()
    assert (extraction.status, extraction.error_code) == ("cancelled", "document_unavailable")


def test_retry_creates_history_and_safe_audit(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity, department, _ = _seed(db)
    document = _document(db, identity, department)
    failed = _queued(db, identity, department, document)
    _finish(failed, "failed")
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            f"/departments/{department.id}/documents/{document.id}/extractions/{failed.id}/retry",
            headers=_headers(),
        )
    assert response.status_code == 202 and response.json()["attempt_number"] == 2
    db.expire_all()
    retry = db.scalars(select(DocumentExtraction).where(DocumentExtraction.id != failed.id)).one()
    assert retry.retry_of_id == failed.id and retry.source_sha256 == failed.source_sha256
    assert db.query(PersistentAuditEvent).filter_by(action="document.extraction.retry").count() == 1


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "cancelled"])
def test_retry_rejects_nonfailed_status(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: str,
) -> None:
    identity, department, _ = _seed(db)
    document = _document(db, identity, department)
    extraction = _queued(db, identity, department, document)
    if status != "queued":
        if status == "running":
            now = datetime.now(UTC)
            extraction.status = "running"
            extraction.worker_id = uuid4()
            extraction.claim_token = uuid4()
            extraction.claimed_at = now
            extraction.started_at = now
            extraction.lease_expires_at = now + timedelta(minutes=5)
        else:
            _finish(extraction, status)
        db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            f"/departments/{department.id}/documents/{document.id}/extractions/{extraction.id}/retry",
            headers=_headers(),
        )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "role", ["system_admin", "department_admin", "instructor", "student", "viewer"]
)
def test_all_roles_read_metadata_but_never_chunk_text(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
) -> None:
    identity, department, _ = _seed(db, role=role)
    document = _document(db, identity, department)
    extraction = _queued(db, identity, department, document)
    _finish(extraction, "succeeded")
    db.add(
        DocumentChunk(
            department_id=department.id,
            document_id=document.id,
            extraction_id=extraction.id,
            ordinal=0,
            char_start=0,
            char_end=5,
            byte_size=5,
            content_sha256="3" * 64,
            provenance_kind="line",
            line_start=1,
            line_end=1,
        )
    )
    db.commit()
    base = f"/departments/{department.id}/documents/{document.id}/extractions"
    with _client(monkeypatch, tmp_path) as client:
        listed = client.get(base, headers=_headers())
        read = client.get(f"{base}/{extraction.id}", headers=_headers())
        chunks = client.get(f"{base}/{extraction.id}/chunks", headers=_headers())
    assert listed.status_code == read.status_code == chunks.status_code == 200
    item = chunks.json()["items"][0]
    assert item["ordinal"] == 0
    assert {"text", "content", "content_sha256", "department_id", "document_id"}.isdisjoint(item)


def test_two_workers_claim_distinct_jobs_and_expired_lease_is_reclaimable(
    db: Session, engine
) -> None:
    identity, department, _ = _seed(db)
    first_document = _document(db, identity, department, b"one")
    second_document = _document(db, identity, department, b"two")
    _queued(db, identity, department, first_document)
    _queued(db, identity, department, second_document)
    factory = create_session_factory(engine)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        return claim_next(factory, uuid4(), 300)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim) for _ in range(2)]
        claims = [future.result(timeout=10) for future in futures]
    assert None not in claims and len({item.id for item in claims if item}) == 2
    first = claims[0]
    assert first is not None and heartbeat(factory, first, 300)
    with Session(engine) as session:
        row = session.get(DocumentExtraction, first.id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    replacement = claim_next(factory, uuid4(), 300)
    assert replacement is not None and replacement.id == first.id
    assert replacement.claim_token != first.claim_token and not heartbeat(factory, first, 300)


def test_worker_end_to_end_publishes_metadata_and_external_content(
    db: Session,
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity, department, _ = _seed(db)
    payload = b"First line\r\nSecond line"
    document = _document(db, identity, department, payload)
    _queued(db, identity, department, document)
    source = tmp_path / "uploads" / str(department.id) / str(document.id) / "source"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    (tmp_path / "extracted_text").mkdir()
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", _database_url())
    settings = WorkerSettings.from_environment()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), settings.extraction_lease_seconds)
    assert job is not None and process_job(factory, settings, job, lambda: False)
    db.expire_all()
    extraction = db.get(DocumentExtraction, job.id)
    assert extraction is not None and extraction.status == "succeeded"
    assert extraction.chunk_count == db.query(DocumentChunk).count() > 0
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.extraction.complete").count() == 1
    )
    final = tmp_path / "extracted_text" / str(department.id) / str(document.id) / str(job.id)
    assert (final / "normalized.txt").read_text() == "First line\nSecond line"
    jsonl = [json.loads(line) for line in (final / "chunks.jsonl").read_text().splitlines()]
    assert jsonl[0]["text"] and "path" not in jsonl[0]
    manifest = json.loads((final / "manifest.json").read_text())
    assert manifest["pipeline_version"] == "phase5-extraction-v1"
    assert {"original_filename", "database_url", "path", "subject"}.isdisjoint(manifest)


def test_source_mutation_during_processing_blocks_publication(
    db: Session,
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity, department, _ = _seed(db)
    payload = b"source before mutation"
    document = _document(db, identity, department, payload)
    _queued(db, identity, department, document)
    source = tmp_path / "uploads" / str(department.id) / str(document.id) / "source"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    (tmp_path / "extracted_text").mkdir()
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", _database_url())
    original_chunk_document = pipeline_module.chunk_document

    def mutate_after_chunking(*args, **kwargs):
        chunks = original_chunk_document(*args, **kwargs)
        source.write_bytes(b"host-side mutation")
        return chunks

    monkeypatch.setattr(pipeline_module, "chunk_document", mutate_after_chunking)
    settings = WorkerSettings.from_environment()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), settings.extraction_lease_seconds)
    assert job is not None
    assert not process_job(factory, settings, job, lambda: False)
    db.expire_all()
    extraction = db.get(DocumentExtraction, job.id)
    assert extraction is not None
    assert (extraction.status, extraction.error_code) == (
        "failed",
        "source_integrity_mismatch",
    )
    assert db.query(DocumentChunk).count() == 0
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.extraction.complete").count() == 0
    )
    final = tmp_path / "extracted_text" / str(department.id) / str(document.id) / str(job.id)
    assert not final.exists()


def _prepared_publication(storage, department, document, job, marker: str):
    staging = storage.create_staging(
        DepartmentScope(department.id), document.id, job.id, job.claim_token
    )
    staging.write_file("normalized.txt", marker.encode())
    staging.write_file("chunks.jsonl", b'{"text":"x"}\n')
    staging.write_file("manifest.json", b"{}\n")
    chunk = Chunk(
        ordinal=0,
        text=marker,
        char_start=0,
        char_end=1,
        byte_size=1,
        content_sha256=hashlib.sha256(marker.encode()).hexdigest(),
        provenance_kind="line",
        line_start=1,
        line_end=1,
    )
    return staging, Publication(
        "python-utf8",
        "3.12",
        hashlib.sha256(marker.encode()).hexdigest(),
        1,
        staging.output_size(),
        (chunk,),
    )


def test_concurrent_finalization_serializes_extracted_quota(
    db: Session, engine, tmp_path: Path
) -> None:
    identity, department, _ = _seed(db)
    documents = [_document(db, identity, department, marker.encode()) for marker in ("a", "b")]
    for document in documents:
        _queued(db, identity, department, document)
    factory = create_session_factory(engine)
    jobs = [claim_next(factory, uuid4(), 300), claim_next(factory, uuid4(), 300)]
    assert all(jobs)
    (tmp_path / "uploads").mkdir()
    (tmp_path / "extracted_text").mkdir()
    storage = ExtractionStorage(tmp_path)
    prepared = [
        _prepared_publication(storage, department, document, job, marker)
        for document, job, marker in zip(documents, jobs, ("a", "b"), strict=True)
    ]
    quota = prepared[0][1].output_byte_size
    barrier = Barrier(2)

    def finalize(index: int) -> str:
        job = jobs[index]
        assert job is not None
        staging, publication = prepared[index]
        barrier.wait(timeout=5)
        try:
            finalize_success(factory, job, publication, staging, quota)
            return "succeeded"
        except QueueError as error:
            fail_owned(factory, job, error.code)
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(finalize, index) for index in range(2)]
        results = sorted(future.result(timeout=10) for future in futures)
    assert results == ["extraction_quota_exceeded", "succeeded"]
    db.expire_all()
    assert db.query(DocumentExtraction).filter_by(status="succeeded").count() == 1
    assert db.query(DocumentChunk).count() == 1
    assert (
        sum(
            item.output_byte_size or 0
            for item in db.query(DocumentExtraction).filter_by(status="succeeded")
        )
        <= quota
    )
