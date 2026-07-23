"""PostgreSQL 16 coverage for Phase 6 indexing metadata and lease authority."""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from alembic.config import Config
from deptslm_worker.index_pipeline import (
    _cleanup_stale_attempt,
    _delete_owned_attempt,
    _run_owned_qdrant_mutation,
    process_index_job,
)
from deptslm_worker.index_queue import (
    IndexQueueError,
    claim_next,
    fail_owned,
    finalize_success,
    heartbeat,
    requeue_owned,
)
from deptslm_worker.qdrant_adapter import QdrantBoundaryError, VectorHit, VectorPoint
from deptslm_worker.vector_retrieval import RetrievalBoundaryError, search_authorized
from fastapi.testclient import TestClient
from sqlalchemy import delete, inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
    DocumentVectorIndexing,
    Membership,
    PersistentAuditEvent,
    UserIdentity,
)
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    VECTOR_SCHEMA_VERSION,
)

pytestmark = pytest.mark.postgres
SECRET = "phase-6-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
ISSUER = "https://phase6.issuer.invalid"
AUDIENCE = "phase6-tests"


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped in CI")
    pytest.skip("PostgreSQL integration database is unavailable")


@pytest.fixture(scope="module")
def engine():
    database_url = _database_url()
    os.environ["DATABASE_URL"] = database_url
    value = create_database_engine(database_url)
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


@pytest.fixture
def db(engine) -> Session:
    with Session(engine) as session:
        session.execute(delete(DocumentVectorIndexing))
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


def _identity(db: Session, department: Department, role: str, subject: str) -> UserIdentity:
    identity = UserIdentity(issuer=ISSUER, subject=subject, status="active")
    db.add(identity)
    db.flush()
    db.add(
        Membership(
            user_id=identity.id,
            department_id=department.id,
            role=role,
            status="active",
            created_by_user_id=identity.id,
        )
    )
    db.flush()
    return identity


def _seed(db: Session, *, role: str = "department_admin", subject: str = "phase6-admin"):
    department = Department(slug=f"index-{uuid4().hex[:8]}", display_name="Indexing")
    db.add(department)
    db.flush()
    identity = _identity(db, department, role, subject)
    payload = b"alpha beta"
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=identity.id,
        original_filename="notes.txt",
        media_type="text/plain",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    db.add(document)
    db.flush()
    now = datetime.now(UTC)
    extraction = DocumentExtraction(
        department_id=department.id,
        document_id=document.id,
        requested_by_user_id=identity.id,
        status="succeeded",
        pipeline_version="phase5-extraction-v1",
        parser_name="python-utf8",
        parser_version="3.12",
        normalization_version="phase5-normalization-v1",
        chunking_version="phase5-character-chunker-v1",
        source_sha256=document.sha256,
        source_byte_size=document.byte_size,
        normalized_sha256="1" * 64,
        normalized_byte_size=len(payload),
        output_byte_size=100,
        chunk_count=1,
        worker_id=uuid4(),
        claim_token=uuid4(),
        claimed_at=now,
        started_at=now,
        finished_at=now,
    )
    db.add(extraction)
    db.flush()
    chunk = DocumentChunk(
        department_id=department.id,
        document_id=document.id,
        extraction_id=extraction.id,
        ordinal=0,
        char_start=0,
        char_end=len(payload.decode()),
        byte_size=len(payload),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        provenance_kind="line",
        line_start=1,
        line_end=1,
    )
    db.add(chunk)
    db.commit()
    return identity, department, document, extraction, chunk


def _indexing(identity, department, document, extraction, *, status="queued"):
    row = DocumentVectorIndexing(
        department_id=department.id,
        document_id=document.id,
        extraction_id=extraction.id,
        requested_by_user_id=identity.id,
        status=status,
        embedding_pipeline_version=EMBEDDING_PIPELINE_VERSION,
        embedding_model_id=EMBEDDING_MODEL_ID,
        embedding_model_revision=EMBEDDING_MODEL_REVISION,
        embedding_dimension=EMBEDDING_DIMENSION,
        distance=EMBEDDING_DISTANCE,
        vector_schema_version=VECTOR_SCHEMA_VERSION,
        qdrant_collection=QDRANT_COLLECTION,
        expected_chunk_count=1,
    )
    if status == "failed":
        row.finished_at = datetime.now(UTC)
        row.error_code = "embedding_failed"
    return row


def _token(subject: str) -> str:
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


def _headers(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


def _base(department, document, extraction) -> str:
    return (
        f"/departments/{department.id}/documents/{document.id}"
        f"/extractions/{extraction.id}/indexings"
    )


def test_00_migration_cycle_and_metadata_only_schema(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0003_phase5_extraction")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT to_regclass('document_vector_indexings')")).scalar()
            is None
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0006_phase8_rag_feedback"
        )
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='document_vector_indexings'"
                )
            ).scalars()
        )
    assert {"department_id", "document_id", "extraction_id", "vector_attempt_id"} <= columns
    assert {"vector", "embedding", "text", "content", "model_path", "qdrant_url"}.isdisjoint(
        columns
    )
    foreign_keys = inspect(engine).get_foreign_keys("document_vector_indexings")
    assert {item["name"] for item in foreign_keys} >= {
        "fk_vector_indexing_document_scope",
        "fk_vector_indexing_extraction_scope",
        "fk_vector_indexing_retry_scope",
    }
    assert all(item["options"].get("ondelete") == "RESTRICT" for item in foreign_keys)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda row: setattr(row, "embedding_dimension", 768),
        lambda row: setattr(row, "distance", "dot"),
        lambda row: setattr(row, "embedding_model_revision", "main"),
        lambda row: setattr(row, "vector_schema_version", "other"),
        lambda row: setattr(row, "error_code", "raw exception text"),
        lambda row: setattr(row, "expected_chunk_count", 0),
        lambda row: setattr(row, "status", "unknown"),
        lambda row: setattr(row, "worker_id", uuid4()),
    ],
)
def test_indexing_constraints_reject_invalid_contract_or_lifecycle(db: Session, mutator) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    row = _indexing(identity, department, document, extraction)
    mutator(row)
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()


def test_composite_scope_and_unique_active_constraints(db: Session) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    other = Department(slug=f"other-{uuid4().hex[:8]}", display_name="Other")
    db.add(other)
    db.flush()
    foreign = _indexing(identity, department, document, extraction)
    foreign.department_id = other.id
    db.add(foreign)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    first = _indexing(identity, department, document, extraction)
    second = _indexing(identity, department, document, extraction)
    db.add_all([first, second])
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
def test_enqueue_authorization_and_safe_schema(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
    expected: int,
) -> None:
    subject = f"phase6-{role}"
    _identity_row, department, document, extraction, _chunk = _seed(db, role=role, subject=subject)
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(_base(department, document, extraction), headers=_headers(subject))
    assert response.status_code == expected
    if expected == 202:
        body = response.json()
        assert body["embedding_model_id"] == EMBEDDING_MODEL_ID
        forbidden = {
            "requested_by_user_id",
            "worker_id",
            "claim_token",
            "vector_attempt_id",
            "lease_expires_at",
            "qdrant_collection",
            "embedding_model_revision",
            "vector",
            "hash",
        }
        assert forbidden.isdisjoint(body)
        assert (
            db.query(PersistentAuditEvent).filter_by(action="document.vector_index.enqueue").count()
            == 1
        )
    else:
        assert "WWW-Authenticate" not in response.headers


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
def test_retry_authorization(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
    expected: int,
) -> None:
    subject = f"phase6-retry-{role}"
    identity, department, document, extraction, _chunk = _seed(db, role=role, subject=subject)
    failed = _indexing(identity, department, document, extraction, status="failed")
    db.add(failed)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            f"{_base(department, document, extraction)}/{failed.id}/retry",
            headers=_headers(subject),
        )
    assert response.status_code == expected
    if expected == 202:
        assert response.json()["attempt_number"] == 2
    else:
        assert "WWW-Authenticate" not in response.headers


def test_only_failed_indexing_is_retryable(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    queued = _indexing(identity, department, document, extraction)
    db.add(queued)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            f"{_base(department, document, extraction)}/{queued.id}/retry",
            headers=_headers("phase6-admin"),
        )
    assert response.status_code == 409
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.vector_index.retry").count() == 0
    )


def test_duplicate_retry_and_all_role_reads(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _identity_row, department, document, extraction, _chunk = _seed(db)
    base = _base(department, document, extraction)
    with _client(monkeypatch, tmp_path) as client:
        created = client.post(base, headers=_headers("phase6-admin"))
        assert created.status_code == 202
        indexing_id = created.json()["id"]
        assert client.post(base, headers=_headers("phase6-admin")).status_code == 409
        row = db.get(DocumentVectorIndexing, indexing_id)
        row.status = "failed"
        row.finished_at = datetime.now(UTC)
        row.error_code = "embedding_failed"
        db.commit()
        retried = client.post(f"{base}/{indexing_id}/retry", headers=_headers("phase6-admin"))
        assert retried.status_code == 202
        assert retried.json()["attempt_number"] == 2
        retried_id = retried.json()["id"]
        for role in ("system_admin", "instructor", "student", "viewer"):
            _identity(db, department, role, f"phase6-reader-{role}")
        db.commit()
        for subject in (
            "phase6-admin",
            "phase6-reader-system_admin",
            "phase6-reader-instructor",
            "phase6-reader-student",
            "phase6-reader-viewer",
        ):
            assert client.get(base, headers=_headers(subject)).status_code == 200
            assert client.get(f"{base}/{retried_id}", headers=_headers(subject)).status_code == 200
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.vector_index.retry").count() == 1
    )


def test_indexing_endpoint_preserves_bearer_challenge(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _identity_row, department, document, extraction, _chunk = _seed(db)
    with _client(monkeypatch, tmp_path) as client:
        response = client.get(_base(department, document, extraction))
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_non_succeeded_extraction_is_not_indexable(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _identity_row, department, document, extraction, _chunk = _seed(db)
    extraction.status = "failed"
    extraction.error_code = "parser_failed"
    extraction.normalized_sha256 = None
    extraction.normalized_byte_size = None
    extraction.output_byte_size = None
    extraction.chunk_count = None
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            _base(department, document, extraction), headers=_headers("phase6-admin")
        )
    assert response.status_code == 404


def test_foreign_scope_and_deleted_document_are_non_enumerating(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _identity_row, department, document, extraction, _chunk = _seed(db)
    identity_id = _identity_row.id
    other = Department(slug=f"foreign-{uuid4().hex[:8]}", display_name="Foreign")
    db.add(other)
    _identity(db, other, "system_admin", "foreign-admin")
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            _base(department, document, extraction), headers=_headers("foreign-admin")
        )
        assert response.status_code == 403
        next_version = document.version + 1
        document.status = "deleted"
        document.deleted_at = datetime.now(UTC)
        document.deleted_by_user_id = identity_id
        document.version = next_version
        db.commit()
        response = client.post(
            _base(department, document, extraction), headers=_headers("phase6-admin")
        )
        assert response.status_code == 404


def test_soft_delete_cancels_only_queued_indexings(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    queued = _indexing(identity, department, document, extraction)
    db.add(queued)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.delete(
            f"/departments/{department.id}/documents/{document.id}",
            headers=_headers("phase6-admin"),
        )
    assert response.status_code == 200
    db.refresh(queued)
    assert (queued.status, queued.error_code) == ("cancelled", "document_unavailable")


def test_claims_are_distinct_and_expired_authority_is_non_revivable(db: Session, engine) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    first = _indexing(identity, department, document, extraction)
    db.add(first)
    db.commit()
    factory = create_session_factory(engine)
    claimed = claim_next(factory, uuid4(), 60)
    assert claimed is not None
    assert claim_next(factory, uuid4(), 60) is None
    db.execute(
        text(
            "UPDATE document_vector_indexings SET lease_expires_at = clock_timestamp() "
            "- interval '1 second' WHERE id = :id"
        ),
        {"id": claimed.id},
    )
    db.commit()
    replacement = claim_next(factory, uuid4(), 60)
    assert replacement is not None
    assert replacement.claim_token != claimed.claim_token
    assert replacement.vector_attempt_id != claimed.vector_attempt_id
    assert replacement.stale_vector_attempt_id == claimed.vector_attempt_id
    assert heartbeat(factory, claimed, 60) is False
    assert requeue_owned(factory, claimed) is False
    assert fail_owned(factory, claimed, "embedding_failed") is False


def test_two_workers_claim_distinct_jobs(db: Session, engine) -> None:
    first_identity, first_department, first_document, first_extraction, _chunk = _seed(db)
    second_identity, second_department, second_document, second_extraction, _chunk = _seed(
        db, subject="phase6-second-admin"
    )
    db.add_all(
        [
            _indexing(first_identity, first_department, first_document, first_extraction),
            _indexing(second_identity, second_department, second_document, second_extraction),
        ]
    )
    db.commit()
    factory = create_session_factory(engine)
    first = claim_next(factory, uuid4(), 60)
    second = claim_next(factory, uuid4(), 60)
    assert first is not None and second is not None
    assert first.id != second.id


class _MutationSpy:
    def __init__(self) -> None:
        self.deleted = []
        self.upserts = 0
        self.activations = 0

    def delete_attempt(self, scope, indexing_id, vector_attempt_id) -> None:
        self.deleted.append((scope.value, indexing_id, vector_attempt_id))

    def upsert_staging(self, _scope, _points) -> None:
        self.upserts += 1

    def activate_attempt(self, _scope, _indexing_id, _vector_attempt_id) -> None:
        self.activations += 1


@pytest.mark.parametrize("mutation", ["delete", "upsert", "activate"])
def test_expired_worker_cannot_mutate_qdrant(db: Session, engine, mutation: str) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    db.execute(
        text(
            "UPDATE document_vector_indexings SET lease_expires_at = clock_timestamp() "
            "- interval '1 second' WHERE id = :id"
        ),
        {"id": job.id},
    )
    db.commit()
    settings = SimpleNamespace(lease_seconds=60)
    qdrant = _MutationSpy()
    with pytest.raises(IndexQueueError, match="claim_lost"):
        if mutation == "delete":
            _delete_owned_attempt(
                factory,
                settings,
                qdrant,
                DepartmentScope(job.department_id),
                job,
                job.vector_attempt_id,
            )
        elif mutation == "upsert":
            _run_owned_qdrant_mutation(
                factory,
                settings,
                job,
                lambda: qdrant.upsert_staging(DepartmentScope(job.department_id), (object(),)),
            )
        else:
            _run_owned_qdrant_mutation(
                factory,
                settings,
                job,
                lambda: qdrant.activate_attempt(
                    DepartmentScope(job.department_id), job.id, job.vector_attempt_id
                ),
            )
    assert qdrant.deleted == [] and qdrant.upserts == 0 and qdrant.activations == 0


def test_reclaimed_worker_cannot_delete_after_token_replacement(db: Session, engine) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    stale = claim_next(factory, uuid4(), 60)
    assert stale is not None
    db.execute(
        text(
            "UPDATE document_vector_indexings SET lease_expires_at = clock_timestamp() "
            "- interval '1 second' WHERE id = :id"
        ),
        {"id": stale.id},
    )
    db.commit()
    replacement = claim_next(factory, uuid4(), 60)
    assert replacement is not None
    qdrant = _MutationSpy()
    with pytest.raises(IndexQueueError, match="claim_lost"):
        _delete_owned_attempt(
            factory,
            SimpleNamespace(lease_seconds=60),
            qdrant,
            DepartmentScope(stale.department_id),
            stale,
            stale.vector_attempt_id,
        )
    assert qdrant.deleted == []


class _UnavailableFactory:
    def __call__(self):
        raise SQLAlchemyError("database unavailable")


def test_database_failure_prevents_qdrant_mutation() -> None:
    job = SimpleNamespace()
    qdrant = _MutationSpy()
    with pytest.raises(IndexQueueError, match="database_unavailable"):
        _run_owned_qdrant_mutation(
            _UnavailableFactory(),
            SimpleNamespace(lease_seconds=60),
            job,
            lambda: qdrant.upsert_staging(DepartmentScope(uuid4()), (object(),)),
        )
    assert qdrant.upserts == 0


class _SchemaMismatchQdrant(_MutationSpy):
    def verify_collection(self) -> None:
        raise QdrantBoundaryError("qdrant_schema_mismatch")


def test_collection_mismatch_fails_job_without_any_qdrant_mutation(db: Session, engine) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    qdrant = _SchemaMismatchQdrant()
    assert (
        process_index_job(
            factory,
            SimpleNamespace(lease_seconds=60),
            qdrant,
            job,
            lambda: False,
        )
        is False
    )
    row = db.get(DocumentVectorIndexing, job.id)
    db.refresh(row)
    assert (row.status, row.error_code) == ("failed", "qdrant_schema_mismatch")
    assert qdrant.deleted == [] and qdrant.upserts == 0 and qdrant.activations == 0
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.vector_index.complete").count()
        == 0
    )


class _DelayedAttemptQdrant:
    def __init__(self, stale_attempt_id) -> None:
        self.stale_attempt_id = stale_attempt_id
        self.stale_upsert_started = threading.Event()
        self.release_stale_upsert = threading.Event()
        self.lock = threading.Lock()
        self.points: dict[tuple[object, object, object], dict[object, bool]] = {}
        self.cleanup_attempts = []

    @staticmethod
    def _key(scope, indexing_id, vector_attempt_id):
        return scope.value, indexing_id, vector_attempt_id

    def seed(self, scope, indexing_id, vector_attempt_id, point_id, *, published=False) -> None:
        with self.lock:
            self.points.setdefault(self._key(scope, indexing_id, vector_attempt_id), {})[
                point_id
            ] = published

    def upsert_staging(self, scope, points) -> None:
        points = tuple(points)
        if points[0].vector_attempt_id == self.stale_attempt_id:
            self.stale_upsert_started.set()
            assert self.release_stale_upsert.wait(timeout=5)
        with self.lock:
            for point in points:
                self.points.setdefault(
                    self._key(scope, point.indexing_id, point.vector_attempt_id), {}
                )[point.chunk_id] = False

    def delete_attempt(self, scope, indexing_id, vector_attempt_id) -> None:
        key = self._key(scope, indexing_id, vector_attempt_id)
        with self.lock:
            self.cleanup_attempts.append(key)
            self.points.pop(key, None)

    def count_attempt(self, scope, indexing_id, vector_attempt_id, *, published):
        with self.lock:
            return sum(
                value is published
                for value in self.points.get(
                    self._key(scope, indexing_id, vector_attempt_id), {}
                ).values()
            )

    def inspect_attempt(self, scope, indexing_id, vector_attempt_id, *, published, maximum):
        with self.lock:
            values = tuple(
                point_id
                for point_id, value in self.points.get(
                    self._key(scope, indexing_id, vector_attempt_id), {}
                ).items()
                if value is published
            )
        return values[: maximum + 1]

    def activate_attempt(self, scope, indexing_id, vector_attempt_id) -> None:
        key = self._key(scope, indexing_id, vector_attempt_id)
        with self.lock:
            self.points[key] = {point_id: True for point_id in self.points.get(key, {})}


def _vector_point(job, chunk_id) -> VectorPoint:
    return VectorPoint(
        chunk_id=chunk_id,
        document_id=job.document_id,
        extraction_id=job.extraction_id,
        indexing_id=job.id,
        vector_attempt_id=job.vector_attempt_id,
        chunk_ordinal=0,
        provenance_kind="line",
        page_start=None,
        page_end=None,
        line_start=1,
        line_end=1,
        vector=tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)),
    )


def test_reclaim_second_cleanup_removes_late_stale_upsert_before_activation(
    db: Session, engine
) -> None:
    identity, department, document, extraction, chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    settings = SimpleNamespace(lease_seconds=60)
    stale = claim_next(factory, uuid4(), 60)
    assert stale is not None
    scope = DepartmentScope(stale.department_id)
    qdrant = _DelayedAttemptQdrant(stale.vector_attempt_id)
    stale_errors = []

    def delayed_stale_write() -> None:
        try:
            _run_owned_qdrant_mutation(
                factory,
                settings,
                stale,
                lambda: qdrant.upsert_staging(scope, (_vector_point(stale, uuid4()),)),
            )
        except IndexQueueError as error:
            stale_errors.append(error.code)

    thread = threading.Thread(target=delayed_stale_write)
    thread.start()
    assert qdrant.stale_upsert_started.wait(timeout=5)
    db.execute(
        text(
            "UPDATE document_vector_indexings SET lease_expires_at = clock_timestamp() "
            "- interval '1 second' WHERE id = :id"
        ),
        {"id": stale.id},
    )
    db.commit()
    replacement = claim_next(factory, uuid4(), 60)
    assert replacement is not None
    assert replacement.stale_vector_attempt_id == stale.vector_attempt_id
    replacement_scope = DepartmentScope(replacement.department_id)
    unrelated_scope = DepartmentScope(uuid4())
    unrelated_key = (unrelated_scope.value, uuid4(), uuid4())
    qdrant.seed(
        unrelated_scope,
        unrelated_key[1],
        unrelated_key[2],
        uuid4(),
    )
    _cleanup_stale_attempt(factory, settings, qdrant, replacement_scope, replacement)
    qdrant.release_stale_upsert.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert stale_errors == ["claim_lost"]
    current_point = _vector_point(replacement, chunk.id)
    _run_owned_qdrant_mutation(
        factory,
        settings,
        replacement,
        lambda: qdrant.upsert_staging(replacement_scope, (current_point,)),
    )
    _cleanup_stale_attempt(factory, settings, qdrant, replacement_scope, replacement)
    finalize_success(factory, replacement, qdrant)
    stale_key = (replacement.department_id, replacement.id, stale.vector_attempt_id)
    current_key = (
        replacement.department_id,
        replacement.id,
        replacement.vector_attempt_id,
    )
    assert qdrant.points.get(stale_key) is None
    assert qdrant.points[current_key] == {chunk.id: True}
    assert unrelated_key in qdrant.points
    assert qdrant.cleanup_attempts.count(stale_key) == 2
    row = db.get(DocumentVectorIndexing, replacement.id)
    db.refresh(row)
    assert row.status == "succeeded"


class _SuccessfulQdrant:
    def __init__(self) -> None:
        self.activated = False
        self.point_id = uuid4()

    def count_attempt(self, _scope, _indexing, _attempt, *, published):
        return 1 if published is self.activated else 0

    def activate_attempt(self, _scope, _indexing, _attempt):
        self.activated = True

    def inspect_attempt(self, _scope, _indexing, _attempt, *, published, maximum):
        if published is self.activated and maximum == 1:
            return (self.point_id,)
        return ()


class _RetrievalQdrant:
    def __init__(self, hit: VectorHit) -> None:
        self.hit = hit

    def search_published(self, _scope, _query, *, limit):
        assert limit == 5
        return (self.hit,)


class _FailingFinalizeQdrant(_SuccessfulQdrant):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def count_attempt(self, _scope, _indexing, _attempt, *, published):
        if self.mode == "count":
            return 0
        return super().count_attempt(_scope, _indexing, _attempt, published=published)

    def activate_attempt(self, _scope, _indexing, _attempt):
        if self.mode == "activation":
            raise QdrantBoundaryError("qdrant_write_failed")
        super().activate_attempt(_scope, _indexing, _attempt)


def test_finalize_revalidates_authority_and_writes_completion_audit(db: Session, engine) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    qdrant = _SuccessfulQdrant()
    finalize_success(factory, job, qdrant)
    row = db.get(DocumentVectorIndexing, job.id)
    db.refresh(row)
    assert (row.status, row.point_count) == ("succeeded", 1)
    assert qdrant.activated is True
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.vector_index.complete").count()
        == 1
    )


def test_current_succeeded_indexing_blocks_duplicate_enqueue(
    db: Session,
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    finalize_success(factory, job, _SuccessfulQdrant())
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            _base(department, document, extraction), headers=_headers("phase6-admin")
        )
    assert response.status_code == 409


@pytest.mark.parametrize("mode", ["count", "activation"])
def test_finalize_failure_never_records_success(db: Session, engine, mode: str) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    with pytest.raises(IndexQueueError):
        finalize_success(factory, job, _FailingFinalizeQdrant(mode))
    row = db.get(DocumentVectorIndexing, job.id)
    db.refresh(row)
    assert row.status == "running"
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.vector_index.complete").count()
        == 0
    )


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_internal_retrieval_rejects_non_succeeded_indexing(
    db: Session, engine, status: str
) -> None:
    identity, department, document, extraction, chunk = _seed(db)
    row = _indexing(identity, department, document, extraction)
    db.add(row)
    db.commit()
    factory = create_session_factory(engine)
    attempt_id = uuid4()
    if status != "queued":
        job = claim_next(factory, uuid4(), 60)
        assert job is not None
        attempt_id = job.vector_attempt_id
        if status == "failed":
            assert fail_owned(factory, job, "embedding_failed") is True
        elif status == "cancelled":
            db.execute(
                text(
                    "UPDATE document_vector_indexings SET status='cancelled', "
                    "lease_expires_at=NULL, finished_at=clock_timestamp(), "
                    "error_code='document_unavailable' WHERE id=:id"
                ),
                {"id": job.id},
            )
            db.commit()
    hit = VectorHit(
        point_id=chunk.id,
        document_id=document.id,
        extraction_id=extraction.id,
        indexing_id=row.id,
        vector_attempt_id=attempt_id,
        chunk_ordinal=chunk.ordinal,
        score=0.75,
    )
    query = tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1))
    with pytest.raises(RetrievalBoundaryError):
        search_authorized(
            factory,
            _RetrievalQdrant(hit),
            DepartmentScope(department.id),
            query,
            limit=5,
        )


def test_cleanup_failure_published_point_is_rejected_without_postgres_success(
    db: Session, engine
) -> None:
    identity, department, document, extraction, chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    assert fail_owned(factory, job, "qdrant_cleanup_failed") is True
    published_hit = VectorHit(
        point_id=chunk.id,
        document_id=document.id,
        extraction_id=extraction.id,
        indexing_id=job.id,
        vector_attempt_id=job.vector_attempt_id,
        chunk_ordinal=chunk.ordinal,
        score=0.75,
    )
    query = tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1))
    with pytest.raises(RetrievalBoundaryError):
        search_authorized(
            factory,
            _RetrievalQdrant(published_hit),
            DepartmentScope(department.id),
            query,
            limit=5,
        )


def test_internal_retrieval_cross_checks_postgres_authority(db: Session, engine) -> None:
    identity, department, document, extraction, chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    finalize_success(factory, job, _SuccessfulQdrant())
    hit = VectorHit(
        point_id=chunk.id,
        document_id=document.id,
        extraction_id=extraction.id,
        indexing_id=job.id,
        vector_attempt_id=job.vector_attempt_id,
        chunk_ordinal=chunk.ordinal,
        score=0.75,
    )
    query = tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1))
    result = search_authorized(
        factory,
        _RetrievalQdrant(hit),
        DepartmentScope(department.id),
        query,
        limit=5,
    )
    assert len(result) == 1 and result[0].indexing_id == job.id
    stale_attempt = VectorHit(
        point_id=chunk.id,
        document_id=document.id,
        extraction_id=extraction.id,
        indexing_id=job.id,
        vector_attempt_id=uuid4(),
        chunk_ordinal=chunk.ordinal,
        score=0.75,
    )
    with pytest.raises(RetrievalBoundaryError):
        search_authorized(
            factory,
            _RetrievalQdrant(stale_attempt),
            DepartmentScope(department.id),
            query,
            limit=5,
        )
    identity_id = identity.id
    document.status = "deleted"
    document.deleted_at = datetime.now(UTC)
    document.deleted_by_user_id = identity_id
    document.version += 1
    db.commit()
    with pytest.raises(RetrievalBoundaryError):
        search_authorized(
            factory,
            _RetrievalQdrant(hit),
            DepartmentScope(department.id),
            query,
            limit=5,
        )


def test_finalize_rejects_deleted_document_before_activation(db: Session, engine) -> None:
    identity, department, document, extraction, _chunk = _seed(db)
    db.add(_indexing(identity, department, document, extraction))
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 60)
    assert job is not None
    identity_id = identity.id
    next_version = document.version + 1
    document.status = "deleted"
    document.deleted_at = datetime.now(UTC)
    document.deleted_by_user_id = identity_id
    document.version = next_version
    db.commit()
    qdrant = _SuccessfulQdrant()
    with pytest.raises(IndexQueueError, match="document_unavailable"):
        finalize_success(factory, job, qdrant)
    assert qdrant.activated is False
    assert (
        db.query(PersistentAuditEvent).filter_by(action="document.vector_index.complete").count()
        == 0
    )
