"""PostgreSQL 16 and API integration coverage for Phase 7 grounded answers."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from alembic.config import Config
from deptslm_worker.qdrant_adapter import VectorHit
from fastapi.testclient import TestClient
from sqlalchemy import delete, inspect, text
from sqlalchemy.orm import Session

from alembic import command
from app.database import create_database_engine
from app.main import app
from app.models import (
    Department,
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
    Membership,
    PersistentAuditEvent,
    RagAnswerCitation,
    RagAnswerRun,
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
SECRET = "phase-7-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
RUNTIME_TOKEN = "phase7-postgres-runtime-token-0123456789-abcdef"
ISSUER = "https://phase7.issuer.invalid"
AUDIENCE = "phase7-tests"


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
        session.execute(delete(RagAnswerCitation))
        session.execute(delete(RagAnswerRun))
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


def test_00_migration_cycle_and_content_free_schema(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0004_phase6_vector_indexing")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT to_regclass('rag_answer_runs')")).scalar() is None
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0007_phase9_evaluation_runner"
        )
    run_columns = {column["name"] for column in inspect(engine).get_columns("rag_answer_runs")}
    citation_columns = {
        column["name"] for column in inspect(engine).get_columns("rag_answer_citations")
    }
    forbidden = {
        "question",
        "answer",
        "prompt",
        "text",
        "content",
        "vector",
        "hash",
        "path",
        "token",
        "qdrant_url",
    }
    assert forbidden.isdisjoint(run_columns | citation_columns)
    assert {"department_id", "requested_by_user_id", "question_char_count"} <= run_columns
    assert {"department_id", "document_id", "extraction_id", "indexing_id", "chunk_id"} <= (
        citation_columns
    )


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


def _seed(db: Session, tmp_path: Path):
    department = Department(slug=f"rag-{uuid4().hex[:8]}", display_name="Grounded Answers")
    db.add(department)
    db.flush()
    identities = {
        role: _identity(db, department, role, f"phase7-{role}")
        for role in (
            "system_admin",
            "department_admin",
            "instructor",
            "student",
            "viewer",
        )
    }
    actor = identities["department_admin"]
    source = b"The synthetic policy is approved for testing."
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=actor.id,
        original_filename="policy.txt",
        media_type="text/plain",
        byte_size=len(source),
        sha256=hashlib.sha256(source).hexdigest(),
    )
    db.add(document)
    db.flush()
    now = datetime.now(UTC)
    extraction = DocumentExtraction(
        department_id=department.id,
        document_id=document.id,
        requested_by_user_id=actor.id,
        status="succeeded",
        pipeline_version="phase5-extraction-v1",
        parser_name="python-utf8",
        parser_version="3.12",
        normalization_version="phase5-normalization-v1",
        chunking_version="phase5-character-chunker-v1",
        source_sha256=document.sha256,
        source_byte_size=document.byte_size,
        normalized_sha256=hashlib.sha256(source).hexdigest(),
        normalized_byte_size=len(source),
        output_byte_size=1,
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
        char_end=len(source.decode()),
        byte_size=len(source),
        content_sha256=hashlib.sha256(source).hexdigest(),
        provenance_kind="line",
        line_start=1,
        line_end=1,
    )
    db.add(chunk)
    db.flush()
    attempt = uuid4()
    indexing = DocumentVectorIndexing(
        department_id=department.id,
        document_id=document.id,
        extraction_id=extraction.id,
        requested_by_user_id=actor.id,
        status="succeeded",
        embedding_pipeline_version=EMBEDDING_PIPELINE_VERSION,
        embedding_model_id=EMBEDDING_MODEL_ID,
        embedding_model_revision=EMBEDDING_MODEL_REVISION,
        embedding_dimension=EMBEDDING_DIMENSION,
        distance=EMBEDDING_DISTANCE,
        vector_schema_version=VECTOR_SCHEMA_VERSION,
        qdrant_collection=QDRANT_COLLECTION,
        expected_chunk_count=1,
        point_count=1,
        worker_id=uuid4(),
        claim_token=uuid4(),
        vector_attempt_id=attempt,
        claimed_at=now,
        started_at=now,
        finished_at=now,
    )
    db.add(indexing)
    db.flush()
    extraction.output_byte_size = _write_artifact(
        tmp_path, department, document, extraction, chunk, source
    )
    db.commit()
    return identities, department, document, extraction, chunk, indexing


def _write_artifact(root, department, document, extraction, chunk, source) -> int:
    final = root / "extracted_text" / str(department.id) / str(document.id) / str(extraction.id)
    final.mkdir(parents=True)
    payload = {
        "ordinal": 0,
        "text": source.decode(),
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "byte_size": chunk.byte_size,
        "content_sha256": chunk.content_sha256,
        "provenance_kind": "line",
        "page_start": None,
        "page_end": None,
        "line_start": 1,
        "line_end": 1,
    }
    chunks = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (final / "normalized.txt").write_bytes(source)
    (final / "chunks.jsonl").write_bytes(chunks)
    manifest = {
        "chunk_count": 1,
        "chunking_version": "phase5-character-chunker-v1",
        "chunks_byte_size": len(chunks),
        "chunks_sha256": hashlib.sha256(chunks).hexdigest(),
        "department_id": str(department.id),
        "document_id": str(document.id),
        "extraction_id": str(extraction.id),
        "normalization_version": "phase5-normalization-v1",
        "normalized_byte_size": len(source),
        "normalized_sha256": hashlib.sha256(source).hexdigest(),
        "parser_name": "python-utf8",
        "parser_version": "3.12",
        "pipeline_version": "phase5-extraction-v1",
        "source_byte_size": len(source),
        "source_sha256": hashlib.sha256(source).hexdigest(),
    }
    (final / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return sum(path.stat().st_size for path in final.iterdir())


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


def _headers(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    (tmp_path / "uploads").mkdir(exist_ok=True)
    (tmp_path / "extracted_text").mkdir(exist_ok=True)
    (tmp_path / "model_cache").mkdir(exist_ok=True)
    values = {
        "DATABASE_URL": _database_url(),
        "DEPTSLM_DATA_DIR": str(tmp_path),
        "ENVIRONMENT": "test",
        "DEPTSLM_AUTH_MODE": "hs256",
        "DEPTSLM_AUTH_ISSUER": ISSUER,
        "DEPTSLM_AUTH_AUDIENCE": AUDIENCE,
        "DEPTSLM_AUTH_SECRET": SECRET,
        "DEPTSLM_QDRANT_URL": "http://localhost:6333",
        "DEPTSLM_QDRANT_API_KEY": "phase7-postgres-qdrant-key-0123456789",
        "DEPTSLM_QDRANT_COLLECTION": QDRANT_COLLECTION,
        "DEPTSLM_RAG_RUNTIME_URL": "http://localhost:8010",
        "DEPTSLM_RAG_RUNTIME_TOKEN": RUNTIME_TOKEN,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return TestClient(app)


class _Runtime:
    generate_calls = 0

    def query_embedding(self, _question):
        vector = [0.0] * EMBEDDING_DIMENSION
        vector[0] = 1.0
        return vector

    def generate(self, _question, _evidence):
        self.generate_calls += 1
        return {"status": "answered", "answer": "Approved for testing [S1].", "citations": ["S1"]}


class _Qdrant:
    def __init__(self, hit=None, *, hits=None):
        self.hits = tuple(hits) if hits is not None else (() if hit is None else (hit,))

    def verify_collection(self):
        return None

    def search_published(self, _scope, _query, *, limit):
        assert limit == 20
        return self.hits


def _hit(document, extraction, chunk, indexing, *, score=0.9):
    return VectorHit(
        point_id=chunk.id,
        document_id=document.id,
        extraction_id=extraction.id,
        indexing_id=indexing.id,
        vector_attempt_id=indexing.vector_attempt_id,
        chunk_ordinal=chunk.ordinal,
        score=score,
    )


def _add_source(
    db: Session,
    tmp_path: Path,
    department: Department,
    actor: UserIdentity,
    *,
    filename: str,
    text_value: str,
):
    source = text_value.encode()
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=actor.id,
        original_filename=filename,
        media_type="text/plain",
        byte_size=len(source),
        sha256=hashlib.sha256(source).hexdigest(),
    )
    db.add(document)
    db.flush()
    now = datetime.now(UTC)
    extraction = DocumentExtraction(
        department_id=department.id,
        document_id=document.id,
        requested_by_user_id=actor.id,
        status="succeeded",
        pipeline_version="phase5-extraction-v1",
        parser_name="python-utf8",
        parser_version="3.12",
        normalization_version="phase5-normalization-v1",
        chunking_version="phase5-character-chunker-v1",
        source_sha256=document.sha256,
        source_byte_size=document.byte_size,
        normalized_sha256=hashlib.sha256(source).hexdigest(),
        normalized_byte_size=len(source),
        output_byte_size=1,
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
        char_end=len(text_value),
        byte_size=len(source),
        content_sha256=hashlib.sha256(source).hexdigest(),
        provenance_kind="line",
        line_start=1,
        line_end=1,
    )
    db.add(chunk)
    db.flush()
    indexing = DocumentVectorIndexing(
        department_id=department.id,
        document_id=document.id,
        extraction_id=extraction.id,
        requested_by_user_id=actor.id,
        status="succeeded",
        embedding_pipeline_version=EMBEDDING_PIPELINE_VERSION,
        embedding_model_id=EMBEDDING_MODEL_ID,
        embedding_model_revision=EMBEDDING_MODEL_REVISION,
        embedding_dimension=EMBEDDING_DIMENSION,
        distance=EMBEDDING_DISTANCE,
        vector_schema_version=VECTOR_SCHEMA_VERSION,
        qdrant_collection=QDRANT_COLLECTION,
        expected_chunk_count=1,
        point_count=1,
        worker_id=uuid4(),
        claim_token=uuid4(),
        vector_attempt_id=uuid4(),
        claimed_at=now,
        started_at=now,
        finished_at=now,
    )
    db.add(indexing)
    db.flush()
    extraction.output_byte_size = _write_artifact(
        tmp_path, department, document, extraction, chunk, source
    )
    db.commit()
    return document, extraction, chunk, indexing


def test_all_roles_receive_safe_answer_and_transactional_citations(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document, extraction, chunk, indexing = _seed(db, tmp_path)
    runtime = _Runtime()
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = runtime
        app.state.rag_qdrant = _Qdrant(_hit(document, extraction, chunk, indexing))
        for role, identity in identities.items():
            response = client.post(
                f"/departments/{department.id}/rag/answers",
                headers=_headers(identity.subject),
                json={"question": f"What is approved for {role}?"},
            )
            assert response.status_code == 200
            value = response.json()
            assert set(value) == {
                "id",
                "status",
                "answer",
                "citations",
                "generation_model",
                "created_at",
            }
            assert value["status"] == "answered"
            assert set(value["citations"][0]) == {
                "source_id",
                "document_id",
                "original_filename",
                "chunk_id",
                "ordinal",
                "provenance_kind",
                "page_start",
                "page_end",
                "line_start",
                "line_end",
            }
            assert "score" not in json.dumps(value).lower()
    with Session(engine) as session:
        assert session.query(RagAnswerRun).filter_by(status="answered").count() == 5
        assert session.query(RagAnswerCitation).count() == 5
        assert session.query(PersistentAuditEvent).filter_by(action="rag.answer.start").count() == 5
        assert (
            session.query(PersistentAuditEvent).filter_by(action="rag.answer.complete").count() == 5
        )


def test_insufficient_information_skips_generation(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, *_ = _seed(db, tmp_path)
    runtime = _Runtime()
    runtime.generate_calls = 0
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = runtime
        app.state.rag_qdrant = _Qdrant()
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["viewer"].subject),
            json={"question": "No matching source?"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_information"
    assert response.json()["citations"] == []
    assert runtime.generate_calls == 0
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert (run.status, run.selected_source_count) == ("insufficient_information", 0)
        assert session.query(RagAnswerCitation).count() == 0


@pytest.mark.parametrize(
    ("invalid_answer", "expected_code"),
    [
        ("Dangling [S1", "invalid_citation"),
        ("Dangling S1]", "invalid_citation"),
        ("Leading zero [S01], valid [S1].", "invalid_citation"),
        ("Unicode ［S1］, valid [S1].", "invalid_citation"),
        ("Long [S1" + "x" * 40 + "], valid [S1].", "invalid_citation"),
        ("Hidden [S\u00ad1], valid [S1].", "invalid_generation_response"),
        ("Combining [S\u034f1], valid [S1].", "invalid_generation_response"),
    ],
)
def test_invalid_generation_fails_without_answer_citations_or_completion_audit(
    db: Session,
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_answer: str,
    expected_code: str,
) -> None:
    identities, department, document, extraction, chunk, indexing = _seed(db, tmp_path)

    class InvalidRuntime(_Runtime):
        def generate(self, _question, _evidence):
            return {"status": "answered", "answer": invalid_answer, "citations": ["S1"]}

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = InvalidRuntime()
        app.state.rag_qdrant = _Qdrant(_hit(document, extraction, chunk, indexing))
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["student"].subject),
            json={"question": "Attempt malformed generation"},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Grounded answer unavailable"}
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert (run.status, run.error_code) == ("failed", expected_code)
        assert session.query(RagAnswerCitation).count() == 0
        assert (
            session.query(PersistentAuditEvent).filter_by(action="rag.answer.complete").count() == 0
        )


def test_authentication_and_cross_department_fail_without_resource_leakage(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, *_ = _seed(db, tmp_path)
    foreign = Department(slug=f"foreign-{uuid4().hex[:8]}", display_name="Foreign")
    db.add(foreign)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        missing = client.post(
            f"/departments/{department.id}/rag/answers", json={"question": "question"}
        )
        denied = client.post(
            f"/departments/{foreign.id}/rag/answers",
            headers=_headers(identities["system_admin"].subject),
            json={"question": "foreign question"},
        )
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert denied.status_code == 403
    assert "www-authenticate" not in denied.headers
    with Session(engine) as session:
        assert session.query(RagAnswerRun).count() == 0


def test_document_change_during_generation_prevents_answer_return(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document, extraction, chunk, indexing = _seed(db, tmp_path)

    class DeletingRuntime(_Runtime):
        def generate(self, _question, _evidence):
            with Session(engine) as session:
                row = session.get(Document, document.id)
                row.status = "deleted"
                row.deleted_at = datetime.now(UTC)
                row.deleted_by_user_id = identities["department_admin"].id
                row.version += 1
                session.commit()
            return super().generate(_question, _evidence)

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = DeletingRuntime()
        app.state.rag_qdrant = _Qdrant(_hit(document, extraction, chunk, indexing))
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["instructor"].subject),
            json={"question": "Race with deletion"},
        )
    assert response.status_code == 503
    with Session(engine) as session:
        assert session.query(RagAnswerRun).one().status == "failed"
        assert session.query(RagAnswerCitation).count() == 0
        assert (
            session.query(PersistentAuditEvent).filter_by(action="rag.answer.complete").count() == 0
        )


def test_artifact_change_during_generation_prevents_answer_return(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document, extraction, chunk, indexing = _seed(db, tmp_path)
    artifact = (
        tmp_path
        / "extracted_text"
        / str(department.id)
        / str(document.id)
        / str(extraction.id)
        / "chunks.jsonl"
    )

    class MutatingRuntime(_Runtime):
        def generate(self, _question, _evidence):
            artifact.write_text("{}\n")
            return super().generate(_question, _evidence)

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = MutatingRuntime()
        app.state.rag_qdrant = _Qdrant(_hit(document, extraction, chunk, indexing))
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["viewer"].subject),
            json={"question": "Race with artifact replacement"},
        )
    assert response.status_code == 503
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert (run.status, run.error_code) == ("failed", "source_artifact_mismatch")
        assert session.query(RagAnswerCitation).count() == 0
        assert (
            session.query(PersistentAuditEvent).filter_by(action="rag.answer.complete").count() == 0
        )


@pytest.mark.parametrize(
    "mutation",
    ["document_deleted", "extraction_unavailable", "indexing_attempt", "chunk_metadata"],
)
def test_uncited_supplied_source_database_change_invalidates_answer(
    db: Session,
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    identities, department, document1, extraction1, chunk1, indexing1 = _seed(db, tmp_path)
    document2, extraction2, chunk2, indexing2 = _add_source(
        db,
        tmp_path,
        department,
        identities["department_admin"],
        filename="uncited.txt",
        text_value="The uncited supplied source may influence generation.",
    )

    class MutatingRuntime(_Runtime):
        def generate(self, question, evidence):
            assert [item.label for item in evidence] == ["S1", "S2"]
            with Session(engine) as session:
                if mutation == "document_deleted":
                    row = session.get(Document, document2.id)
                    row.status = "deleted"
                    row.deleted_at = datetime.now(UTC)
                    row.deleted_by_user_id = identities["department_admin"].id
                elif mutation == "extraction_unavailable":
                    row = session.get(DocumentExtraction, extraction2.id)
                    row.status = "failed"
                    row.normalized_sha256 = None
                    row.normalized_byte_size = None
                    row.output_byte_size = None
                    row.chunk_count = None
                    row.error_code = "document_unavailable"
                elif mutation == "indexing_attempt":
                    session.get(DocumentVectorIndexing, indexing2.id).vector_attempt_id = uuid4()
                else:
                    session.get(DocumentChunk, chunk2.id).content_sha256 = "0" * 64
                session.commit()
            return super().generate(question, evidence)

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = MutatingRuntime()
        app.state.rag_qdrant = _Qdrant(
            hits=(
                _hit(document1, extraction1, chunk1, indexing1, score=0.9),
                _hit(document2, extraction2, chunk2, indexing2, score=0.8),
            )
        )
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["instructor"].subject),
            json={"question": "Use both supplied sources safely"},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Grounded answer unavailable"}
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert run.status == "failed"
        assert session.query(RagAnswerCitation).count() == 0
        assert (
            session.query(PersistentAuditEvent).filter_by(action="rag.answer.complete").count() == 0
        )


def test_uncited_supplied_source_artifact_change_invalidates_answer(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document1, extraction1, chunk1, indexing1 = _seed(db, tmp_path)
    document2, extraction2, chunk2, indexing2 = _add_source(
        db,
        tmp_path,
        department,
        identities["department_admin"],
        filename="uncited-artifact.txt",
        text_value="This second source is supplied but not cited.",
    )
    artifact = (
        tmp_path
        / "extracted_text"
        / str(department.id)
        / str(document2.id)
        / str(extraction2.id)
        / "chunks.jsonl"
    )

    class MutatingRuntime(_Runtime):
        def generate(self, question, evidence):
            assert len(evidence) == 2
            artifact.write_text("{}\n")
            return super().generate(question, evidence)

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = MutatingRuntime()
        app.state.rag_qdrant = _Qdrant(
            hits=(
                _hit(document1, extraction1, chunk1, indexing1, score=0.9),
                _hit(document2, extraction2, chunk2, indexing2, score=0.8),
            )
        )
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["viewer"].subject),
            json={"question": "Detect uncited artifact replacement"},
        )
    assert response.status_code == 503
    with Session(engine) as session:
        assert session.query(RagAnswerRun).one().error_code == "source_artifact_mismatch"
        assert session.query(RagAnswerCitation).count() == 0


def test_valid_cited_and_uncited_sources_persist_only_cited_subset(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document1, extraction1, chunk1, indexing1 = _seed(db, tmp_path)
    document2, extraction2, chunk2, indexing2 = _add_source(
        db,
        tmp_path,
        department,
        identities["department_admin"],
        filename="valid-uncited.txt",
        text_value="Valid supplementary evidence.",
    )
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = _Runtime()
        app.state.rag_qdrant = _Qdrant(
            hits=(
                _hit(document1, extraction1, chunk1, indexing1, score=0.9),
                _hit(document2, extraction2, chunk2, indexing2, score=0.8),
            )
        )
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["student"].subject),
            json={"question": "Use valid evidence"},
        )
    assert response.status_code == 200
    assert [item["source_id"] for item in response.json()["citations"]] == ["S1"]
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert (run.status, run.selected_source_count) == ("answered", 2)
        citation = session.query(RagAnswerCitation).one()
        assert citation.chunk_id == chunk1.id


def test_generated_insufficient_result_revalidates_and_counts_all_supplied_sources(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document1, extraction1, chunk1, indexing1 = _seed(db, tmp_path)
    document2, extraction2, chunk2, indexing2 = _add_source(
        db,
        tmp_path,
        department,
        identities["department_admin"],
        filename="insufficient-context.txt",
        text_value="Valid evidence that still may not answer the question.",
    )

    class InsufficientRuntime(_Runtime):
        def generate(self, _question, evidence):
            assert len(evidence) == 2
            return {"status": "insufficient_information", "answer": "", "citations": []}

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = InsufficientRuntime()
        app.state.rag_qdrant = _Qdrant(
            hits=(
                _hit(document1, extraction1, chunk1, indexing1, score=0.9),
                _hit(document2, extraction2, chunk2, indexing2, score=0.8),
            )
        )
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["student"].subject),
            json={"question": "Unsupported conclusion?"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "insufficient_information"
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert (run.status, run.selected_source_count) == ("insufficient_information", 2)
        assert session.query(RagAnswerCitation).count() == 0


def test_generated_insufficient_result_fails_if_supplied_source_changes(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document1, extraction1, chunk1, indexing1 = _seed(db, tmp_path)
    document2, extraction2, chunk2, indexing2 = _add_source(
        db,
        tmp_path,
        department,
        identities["department_admin"],
        filename="changed-insufficient.txt",
        text_value="This source will become unavailable after influencing generation.",
    )

    class MutatingInsufficientRuntime(_Runtime):
        def generate(self, _question, evidence):
            assert len(evidence) == 2
            with Session(engine) as session:
                row = session.get(Document, document2.id)
                row.status = "deleted"
                row.deleted_at = datetime.now(UTC)
                row.deleted_by_user_id = identities["department_admin"].id
                session.commit()
            return {"status": "insufficient_information", "answer": "", "citations": []}

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = MutatingInsufficientRuntime()
        app.state.rag_qdrant = _Qdrant(
            hits=(
                _hit(document1, extraction1, chunk1, indexing1, score=0.9),
                _hit(document2, extraction2, chunk2, indexing2, score=0.8),
            )
        )
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["viewer"].subject),
            json={"question": "Unsupported after source change?"},
        )
    assert response.status_code == 503
    with Session(engine) as session:
        assert session.query(RagAnswerRun).one().status == "failed"
        assert session.query(RagAnswerCitation).count() == 0


def test_unrelated_source_change_does_not_invalidate_supplied_evidence(
    db: Session, engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identities, department, document1, extraction1, chunk1, indexing1 = _seed(db, tmp_path)
    unrelated, *_ = _add_source(
        db,
        tmp_path,
        department,
        identities["department_admin"],
        filename="unrelated.txt",
        text_value="This source is never retrieved or supplied.",
    )

    class MutatingRuntime(_Runtime):
        def generate(self, question, evidence):
            with Session(engine) as session:
                row = session.get(Document, unrelated.id)
                row.status = "deleted"
                row.deleted_at = datetime.now(UTC)
                row.deleted_by_user_id = identities["department_admin"].id
                session.commit()
            return super().generate(question, evidence)

    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = MutatingRuntime()
        app.state.rag_qdrant = _Qdrant(_hit(document1, extraction1, chunk1, indexing1))
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["department_admin"].subject),
            json={"question": "Ignore unrelated changes"},
        )
    assert response.status_code == 200


@pytest.mark.parametrize("outcome", ["answered", "insufficient", "invalid_generation"])
def test_qdrant_close_failure_never_overrides_committed_or_original_outcome(
    db: Session,
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
) -> None:
    identities, department, document, extraction, chunk, indexing = _seed(db, tmp_path)

    class ClosingQdrant(_Qdrant):
        def close(self):
            raise RuntimeError("must remain content-free")

    qdrant = ClosingQdrant(
        hits=() if outcome == "insufficient" else (_hit(document, extraction, chunk, indexing),)
    )
    monkeypatch.setattr("app.rag_answer_services.DepartmentQdrant", lambda *_args: qdrant)

    class InvalidRuntime(_Runtime):
        def generate(self, _question, _evidence):
            return {"status": "answered", "answer": "invalid", "citations": ["S8"]}

    runtime = InvalidRuntime() if outcome == "invalid_generation" else _Runtime()
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = runtime
        app.state.rag_qdrant = None
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["viewer"].subject),
            json={"question": "Close consistency"},
        )
    if outcome == "invalid_generation":
        assert response.status_code == 503
        expected = ("failed", "invalid_citation")
    else:
        assert response.status_code == 200
        expected = (
            "insufficient_information" if outcome == "insufficient" else "answered",
            None,
        )
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert (run.status, run.error_code) == expected


@pytest.mark.parametrize(
    ("stage", "expected_code"),
    [
        ("query", "query_embedding_failed"),
        ("generation", "generation_failed"),
        ("artifact", "source_artifact_mismatch"),
    ],
)
def test_unexpected_stage_exception_marks_run_failed_without_success_audit(
    db: Session,
    engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stage: str,
    expected_code: str,
) -> None:
    identities, department, document, extraction, chunk, indexing = _seed(db, tmp_path)

    class UnexpectedRuntime(_Runtime):
        def query_embedding(self, question):
            if stage == "query":
                raise RuntimeError("secret dependency detail")
            return super().query_embedding(question)

        def generate(self, question, evidence):
            if stage == "generation":
                raise RuntimeError("secret model detail")
            return super().generate(question, evidence)

    if stage == "artifact":
        monkeypatch.setattr(
            "app.rag_answer_services.load_selected_chunks",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret path")),
        )
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = UnexpectedRuntime()
        app.state.rag_qdrant = _Qdrant(_hit(document, extraction, chunk, indexing))
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=_headers(identities["instructor"].subject),
            json={"question": "Unexpected failure"},
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "Grounded answer unavailable"}
    assert "secret" not in response.text
    with Session(engine) as session:
        run = session.query(RagAnswerRun).one()
        assert (run.status, run.error_code) == ("failed", expected_code)
        assert (
            session.query(PersistentAuditEvent).filter_by(action="rag.answer.complete").count() == 0
        )
