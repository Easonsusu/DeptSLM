"""PostgreSQL 16 and API integration coverage for Phase 8 feedback."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import jwt
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.feedback_purge import FeedbackPurgeSettings
from app.main import app
from app.models import (
    Base,
    Department,
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
    Membership,
    PersistentAuditEvent,
    RagAnswerCitation,
    RagAnswerFeedback,
    RagAnswerFeedbackReason,
    RagAnswerFeedbackSourceTarget,
    RagAnswerRun,
    UserIdentity,
)
from app.rag_domain import (
    ANSWER_CONTRACT_VERSION,
    GENERATION_MODEL_ID,
    GENERATION_MODEL_REVISION,
    PROMPT_VERSION,
)
from app.rag_feedback_domain import FeedbackSentiment, FeedbackStatus
from app.rag_feedback_services import (
    list_feedback_for_review,
    purge_feedback_batch,
    read_feedback_for_review,
    read_own_feedback,
    review_feedback,
    submit_feedback,
)
from app.services import ServiceError
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    QUERY_EMBEDDING_PIPELINE_VERSION,
    VECTOR_SCHEMA_VERSION,
)

pytestmark = pytest.mark.postgres
SECRET = "phase-8-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
ISSUER = "https://phase8.issuer.invalid"
AUDIENCE = "phase8-tests"


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
        session.execute(delete(RagAnswerFeedbackSourceTarget))
        session.execute(delete(RagAnswerFeedbackReason))
        session.execute(delete(RagAnswerFeedback))
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


def test_00_migration_paths_schema_and_orm_sync(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.downgrade(config, "0005_phase7_rag_answers")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT to_regclass('rag_answer_feedback')")).scalar() is None
        )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0006_phase8_rag_feedback"
        )
    inspector = inspect(engine)
    tables = {
        "rag_answer_feedback",
        "rag_answer_feedback_reasons",
        "rag_answer_feedback_source_targets",
    }
    assert tables <= set(inspector.get_table_names())
    for table_name in tables:
        database_columns = {item["name"] for item in inspector.get_columns(table_name)}
        orm_columns = {item.name for item in Base.metadata.tables[table_name].columns}
        assert database_columns == orm_columns
    all_columns = {item["name"] for table in tables for item in inspector.get_columns(table)}
    prohibited = {
        "question",
        "answer",
        "prompt",
        "comment",
        "note",
        "text",
        "evidence",
        "excerpt",
        "vector",
        "model_output",
        "filename",
        "path",
        "token",
        "runtime_url",
        "qdrant_url",
    }
    assert all_columns.isdisjoint(prohibited)
    feedback_indexes = {item["name"] for item in inspector.get_indexes("rag_answer_feedback")}
    assert {
        "ix_rag_feedback_owner_lookup",
        "ix_rag_feedback_review_queue",
        "ix_rag_feedback_expiry_purge",
    } <= feedback_indexes
    for table in tables:
        assert all(
            item["options"].get("ondelete") == "RESTRICT"
            for item in inspector.get_foreign_keys(table)
        )


def _identity(db: Session, department: Department, *, role: str, subject: str) -> UserIdentity:
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


def _run(db: Session, department: Department, identity: UserIdentity, status: str):
    now = datetime.now(UTC)
    terminal = status in {"answered", "insufficient_information", "failed"}
    run = RagAnswerRun(
        department_id=department.id,
        requested_by_user_id=identity.id,
        status=status,
        question_char_count=12,
        retrieval_candidate_count=0 if terminal and status != "failed" else None,
        retrieval_authorized_count=0 if terminal and status != "failed" else None,
        selected_source_count=0 if status == "insufficient_information" else None,
        query_embedding_pipeline_version=QUERY_EMBEDDING_PIPELINE_VERSION,
        query_embedding_model_id=EMBEDDING_MODEL_ID,
        query_embedding_model_revision=EMBEDDING_MODEL_REVISION,
        generation_model_id=GENERATION_MODEL_ID,
        generation_model_revision=GENERATION_MODEL_REVISION,
        prompt_version=PROMPT_VERSION,
        answer_contract_version=ANSWER_CONTRACT_VERSION,
        minimum_score=Decimal("0.450"),
        error_code="runtime_unavailable" if status == "failed" else None,
        finished_at=now if terminal else None,
    )
    db.add(run)
    db.flush()
    return run


def _answered_run_with_citations(
    db: Session, department: Department, identity: UserIdentity, *, count: int = 2
):
    now = datetime.now(UTC)
    documents = []
    citations = []
    run = RagAnswerRun(
        department_id=department.id,
        requested_by_user_id=identity.id,
        status="answered",
        question_char_count=12,
        retrieval_candidate_count=count,
        retrieval_authorized_count=count,
        selected_source_count=count,
        query_embedding_pipeline_version=QUERY_EMBEDDING_PIPELINE_VERSION,
        query_embedding_model_id=EMBEDDING_MODEL_ID,
        query_embedding_model_revision=EMBEDDING_MODEL_REVISION,
        generation_model_id=GENERATION_MODEL_ID,
        generation_model_revision=GENERATION_MODEL_REVISION,
        prompt_version=PROMPT_VERSION,
        answer_contract_version=ANSWER_CONTRACT_VERSION,
        minimum_score=Decimal("0.450"),
        finished_at=now,
    )
    db.add(run)
    db.flush()
    for rank in range(1, count + 1):
        source = f"Synthetic source {rank}.".encode()
        document = Document(
            department_id=department.id,
            uploaded_by_user_id=identity.id,
            original_filename=f"source-{rank}.txt",
            media_type="text/plain",
            byte_size=len(source),
            sha256=hashlib.sha256(source).hexdigest(),
        )
        db.add(document)
        db.flush()
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
            normalized_sha256=document.sha256,
            normalized_byte_size=document.byte_size,
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
            content_sha256=document.sha256,
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
            requested_by_user_id=identity.id,
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
        citation = RagAnswerCitation(
            run_id=run.id,
            department_id=department.id,
            document_id=document.id,
            extraction_id=extraction.id,
            indexing_id=indexing.id,
            chunk_id=chunk.id,
            source_label=f"S{rank}",
            rank=rank,
            ordinal=0,
            retrieval_score=Decimal("0.900000"),
            provenance_kind="line",
            line_start=1,
            line_end=1,
        )
        db.add(citation)
        documents.append(document)
        citations.append(citation)
    db.flush()
    return run, citations, documents


def _seed(db: Session, *, role: str = "viewer"):
    department = Department(slug=f"feedback-{uuid4().hex[:8]}", display_name="Feedback")
    foreign = Department(slug=f"foreign-{uuid4().hex[:8]}", display_name="Foreign")
    db.add_all([department, foreign])
    db.flush()
    owner = _identity(db, department, role=role, subject=f"owner-{uuid4().hex}")
    other = _identity(db, department, role="viewer", subject=f"other-{uuid4().hex}")
    admin = _identity(db, department, role="department_admin", subject=f"admin-{uuid4().hex}")
    instructor = _identity(db, department, role="instructor", subject=f"instructor-{uuid4().hex}")
    foreign_user = _identity(db, foreign, role="system_admin", subject=f"foreign-{uuid4().hex}")
    answered, citations, _documents = _answered_run_with_citations(db, department, owner)
    insufficient = _run(db, department, owner, "insufficient_information")
    db.commit()
    return (
        department,
        foreign,
        owner,
        other,
        admin,
        instructor,
        foreign_user,
        answered,
        insufficient,
        citations,
    )


def _feedback(
    db: Session,
    department: Department,
    run: RagAnswerRun,
    identity: UserIdentity,
    *,
    expires_at: datetime | None = None,
    created_at: datetime | None = None,
    sentiment: str = "helpful",
):
    now = created_at or datetime.now(UTC)
    value = RagAnswerFeedback(
        department_id=department.id,
        run_id=run.id,
        submitted_by_user_id=identity.id,
        sentiment=sentiment,
        status="open",
        expires_at=expires_at or now + timedelta(days=180),
        created_at=now,
        updated_at=now,
    )
    db.add(value)
    db.flush()
    return value


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
    for name in (
        "DEPTSLM_QDRANT_URL",
        "DEPTSLM_QDRANT_API_KEY",
        "DEPTSLM_RAG_RUNTIME_URL",
        "DEPTSLM_RAG_RUNTIME_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    values = {
        "DATABASE_URL": _database_url(),
        "DEPTSLM_DATA_DIR": str(tmp_path),
        "ENVIRONMENT": "test",
        "DEPTSLM_AUTH_MODE": "hs256",
        "DEPTSLM_AUTH_ISSUER": ISSUER,
        "DEPTSLM_AUTH_AUDIENCE": AUDIENCE,
        "DEPTSLM_AUTH_SECRET": SECRET,
        "DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS": "180",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return TestClient(app)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sentiment", "unknown"),
        ("status", "unknown"),
        ("version", 0),
        ("open_reviewer", uuid4()),
        ("resolved_code", "duplicate"),
        ("dismissed_code", "confirmed_quality_issue"),
    ],
)
def test_parent_lifecycle_constraints(db: Session, field: str, value) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _citations = _seed(db)
    now = datetime.now(UTC)
    feedback = RagAnswerFeedback(
        department_id=department.id,
        run_id=answered.id,
        submitted_by_user_id=owner.id,
        sentiment=value if field == "sentiment" else "helpful",
        status=value if field == "status" else "open",
        reviewed_by_user_id=value if field == "open_reviewer" else None,
        version=value if field == "version" else 1,
        expires_at=now + timedelta(days=1),
        created_at=now,
        updated_at=now,
    )
    if field == "resolved_code":
        feedback.status = "resolved"
        feedback.reviewed_by_user_id = owner.id
        feedback.reviewed_at = now
        feedback.resolution_code = value
    if field == "dismissed_code":
        feedback.status = "dismissed"
        feedback.reviewed_by_user_id = owner.id
        feedback.reviewed_at = now
        feedback.resolution_code = value
    db.add(feedback)
    with pytest.raises(IntegrityError):
        db.commit()


def test_expiry_unique_owner_and_tenant_run_constraints(db: Session) -> None:
    department, foreign, owner, *_rest, answered, _insufficient, _citations = _seed(db)
    now = datetime.now(UTC)
    invalid_expiry = RagAnswerFeedback(
        department_id=department.id,
        run_id=answered.id,
        submitted_by_user_id=owner.id,
        sentiment="helpful",
        status="open",
        expires_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(invalid_expiry)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    first = _feedback(db, department, answered, owner)
    db.commit()
    db.add(
        RagAnswerFeedback(
            department_id=department.id,
            run_id=answered.id,
            submitted_by_user_id=owner.id,
            sentiment="helpful",
            status="open",
            expires_at=now + timedelta(days=2),
            created_at=now,
            updated_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    assert db.get(RagAnswerFeedback, first.id) is not None
    db.add(
        RagAnswerFeedback(
            department_id=foreign.id,
            run_id=answered.id,
            submitted_by_user_id=owner.id,
            sentiment="helpful",
            status="open",
            expires_at=now + timedelta(days=2),
            created_at=now,
            updated_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_reason_and_source_exact_scope_constraints(db: Session) -> None:
    department, foreign, owner, *_rest, answered, _insufficient, citations = _seed(db)
    feedback = _feedback(db, department, answered, owner, sentiment="unhelpful")
    db.commit()
    db.add(
        RagAnswerFeedbackReason(
            feedback_id=feedback.id,
            department_id=foreign.id,
            run_id=answered.id,
            rank=1,
            reason_code="incorrect",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(
        RagAnswerFeedbackSourceTarget(
            feedback_id=feedback.id,
            department_id=foreign.id,
            run_id=answered.id,
            citation_id=citations[0].id,
            rank=1,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    "role", ["system_admin", "department_admin", "instructor", "student", "viewer"]
)
@pytest.mark.parametrize("answer_status", ["answered", "insufficient_information"])
def test_all_five_roles_submit_own_completed_feedback(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role: str,
    answer_status: str,
) -> None:
    department, _foreign, owner, *_rest, answered, insufficient, _citations = _seed(db, role=role)
    run = answered if answer_status == "answered" else insufficient
    with _client(monkeypatch, tmp_path) as client:
        response = client.put(
            f"/departments/{department.id}/rag/answers/{run.id}/feedback",
            headers=_headers(owner.subject),
            json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
        )
    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {
        "id",
        "run_id",
        "answer_status",
        "sentiment",
        "reason_codes",
        "source_ids",
        "status",
        "resolution_code",
        "created_at",
        "reviewed_at",
        "expires_at",
        "version",
    }
    assert payload["answer_status"] == answer_status


def test_submitter_owner_and_foreign_scope_fail_closed(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    department, foreign, owner, other, _admin, _instructor, foreign_user, answered, *_ = _seed(db)
    path = f"/departments/{department.id}/rag/answers/{answered.id}/feedback"
    with _client(monkeypatch, tmp_path) as client:
        assert (
            client.put(
                path,
                headers=_headers(other.subject),
                json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
            ).status_code
            == 404
        )
        denied = client.put(
            path,
            headers=_headers(foreign_user.subject),
            json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
        )
        assert denied.status_code == 403
        assert "WWW-Authenticate" not in denied.headers
        foreign_path = f"/departments/{foreign.id}/rag/answers/{answered.id}/feedback"
        assert (
            client.put(
                foreign_path,
                headers=_headers(foreign_user.subject),
                json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
            ).status_code
            == 404
        )
        unauthorized = client.put(
            path,
            json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
        )
        assert unauthorized.status_code == 401
        assert unauthorized.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("status", ["running", "failed"])
def test_noncompleted_run_rejects_feedback(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
) -> None:
    department, _foreign, owner, *_ = _seed(db)
    run = _run(db, department, owner, status)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.put(
            f"/departments/{department.id}/rag/answers/{run.id}/feedback",
            headers=_headers(owner.subject),
            json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
        )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"sentiment": "helpful", "reason_codes": ["incorrect"], "source_ids": []},
        {"sentiment": "unhelpful", "reason_codes": [], "source_ids": []},
        {
            "sentiment": "unhelpful",
            "reason_codes": ["wrong_citation"],
            "source_ids": [],
        },
        {
            "sentiment": "unhelpful",
            "reason_codes": ["incorrect"],
            "source_ids": ["S1"],
        },
        {
            "sentiment": "report",
            "reason_codes": ["wrong_citation"],
            "source_ids": ["S8"],
        },
        {
            "sentiment": "helpful",
            "reason_codes": [],
            "source_ids": [],
            "comment": "not allowed",
        },
        {
            "sentiment": "unhelpful",
            "reason_codes": ["incorrect", "incorrect"],
            "source_ids": [],
        },
        {
            "sentiment": "unhelpful",
            "reason_codes": [
                "incorrect",
                "unsupported_claim",
                "missing_information",
                "unsafe_content",
                "formatting_problem",
                "other_unspecified",
            ],
            "source_ids": [],
        },
        {"sentiment": "unhelpful", "reason_codes": ["invented"], "source_ids": []},
    ],
)
def test_submission_validation_fails_without_partial_rows(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _citations = _seed(db)
    with _client(monkeypatch, tmp_path) as client:
        response = client.put(
            f"/departments/{department.id}/rag/answers/{answered.id}/feedback",
            headers=_headers(owner.subject),
            json=payload,
        )
    assert response.status_code == 422
    assert db.scalar(select(func_count(RagAnswerFeedback))) == 0


def func_count(model):
    from sqlalchemy import func

    return func.count(model.id if hasattr(model, "id") else model.feedback_id)


def test_canonical_idempotent_replay_and_conflict(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _citations = _seed(db)
    path = f"/departments/{department.id}/rag/answers/{answered.id}/feedback"
    first_payload = {
        "sentiment": "unhelpful",
        "reason_codes": ["wrong_citation", "incorrect"],
        "source_ids": ["S2", "S1"],
    }
    reordered = {
        "sentiment": "unhelpful",
        "reason_codes": ["incorrect", "wrong_citation"],
        "source_ids": ["S1", "S2"],
    }
    with _client(monkeypatch, tmp_path) as client:
        first = client.put(path, headers=_headers(owner.subject), json=first_payload)
        replay = client.put(path, headers=_headers(owner.subject), json=reordered)
        conflict = client.put(
            path,
            headers=_headers(owner.subject),
            json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
        )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["reason_codes"] == ["incorrect", "wrong_citation"]
    assert replay.json()["source_ids"] == ["S1", "S2"]
    assert conflict.status_code == 409
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.submit"
            )
        )
        == 1
    )


def test_identical_concurrent_submission_creates_one_row_and_audit(db: Session, engine) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _citations = _seed(db)
    factory = create_session_factory(engine)
    barrier = Barrier(2)
    department_id = department.id
    owner_subject = owner.subject
    run_id = answered.id

    def worker():
        with factory.begin() as session:
            barrier.wait()
            return submit_feedback(
                session,
                AuthenticatedPrincipal(owner_subject, ISSUER),
                DepartmentRequestScope(DepartmentScope(department_id)),
                run_id,
                sentiment=FeedbackSentiment.HELPFUL,
                reason_codes=[],
                source_ids=[],
                retention_days=180,
            ).created

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: worker(), range(2)))
    assert sorted(results) == [False, True]
    assert db.scalar(select(func_count(RagAnswerFeedback))) == 1
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.submit"
            )
        )
        == 1
    )


def test_conflicting_concurrent_submission_creates_one_row_and_safe_conflict(
    db: Session, engine
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _citations = _seed(db)
    factory = create_session_factory(engine)
    barrier = Barrier(2)
    department_id = department.id
    owner_subject = owner.subject
    run_id = answered.id

    def worker(sentiment: FeedbackSentiment, reasons: list[str]):
        try:
            with factory.begin() as session:
                barrier.wait()
                result = submit_feedback(
                    session,
                    AuthenticatedPrincipal(owner_subject, ISSUER),
                    DepartmentRequestScope(DepartmentScope(department_id)),
                    run_id,
                    sentiment=sentiment,
                    reason_codes=reasons,
                    source_ids=[],
                    retention_days=180,
                )
            return result.created
        except ServiceError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, FeedbackSentiment.HELPFUL, [])
        second = pool.submit(worker, FeedbackSentiment.UNHELPFUL, ["incorrect"])
        results = [first.result(), second.result()]
    assert set(results) == {True, 409}
    assert db.scalar(select(func_count(RagAnswerFeedback))) == 1
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.submit"
            )
        )
        == 1
    )


@pytest.mark.parametrize("role", ["system_admin", "department_admin", "instructor"])
def test_reviewer_roles_can_list_read_and_transition(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str
) -> None:
    department, _foreign, owner, _other, admin, instructor, *_rest, answered, _insufficient, _ = (
        _seed(db)
    )
    reviewer = admin if role == "department_admin" else instructor
    if role == "system_admin":
        reviewer = _identity(db, department, role="system_admin", subject=f"system-{uuid4().hex}")
    feedback = _feedback(db, department, answered, owner)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        queue = client.get(
            f"/departments/{department.id}/rag/feedback",
            headers=_headers(reviewer.subject),
        )
        detail = client.get(
            f"/departments/{department.id}/rag/feedback/{feedback.id}",
            headers=_headers(reviewer.subject),
        )
        review = client.patch(
            f"/departments/{department.id}/rag/feedback/{feedback.id}",
            headers=_headers(reviewer.subject),
            json={"status": "triaged", "resolution_code": None, "expected_version": 1},
        )
    assert queue.status_code == detail.status_code == review.status_code == 200
    assert queue.json()["items"][0]["id"] == str(feedback.id)
    assert review.json()["status"] == "triaged"
    assert review.json()["version"] == 2
    assert "reviewed_by_user_id" not in review.json()


@pytest.mark.parametrize("role", ["student", "viewer"])
def test_nonreviewer_roles_cannot_list_or_review(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, role: str
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _ = _seed(db)
    user = _identity(db, department, role=role, subject=f"denied-{role}-{uuid4().hex}")
    feedback = _feedback(db, department, answered, owner)
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        queue = client.get(
            f"/departments/{department.id}/rag/feedback", headers=_headers(user.subject)
        )
        review = client.patch(
            f"/departments/{department.id}/rag/feedback/{feedback.id}",
            headers=_headers(user.subject),
            json={"status": "triaged", "resolution_code": None, "expected_version": 1},
        )
    assert queue.status_code == review.status_code == 403
    assert "WWW-Authenticate" not in queue.headers


@pytest.mark.parametrize(
    "authority_state", ["revoked_membership", "expired_membership", "inactive_department"]
)
def test_inactive_authority_denies_submission(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authority_state: str,
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _ = _seed(db)
    membership = db.scalar(
        select(Membership).where(
            Membership.department_id == department.id,
            Membership.user_id == owner.id,
        )
    )
    if authority_state == "revoked_membership":
        membership.status = "revoked"
    elif authority_state == "expired_membership":
        membership.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        department.status = "archived"
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.put(
            f"/departments/{department.id}/rag/answers/{answered.id}/feedback",
            headers=_headers(owner.subject),
            json={"sentiment": "helpful", "reason_codes": [], "source_ids": []},
        )
    assert response.status_code == 403
    assert db.scalar(select(func_count(RagAnswerFeedback))) == 0


@pytest.mark.parametrize(
    ("first_status", "first_code", "second_status", "second_code"),
    [
        ("triaged", None, "resolved", "confirmed_quality_issue"),
        ("triaged", None, "dismissed", "duplicate"),
    ],
)
def test_review_transition_and_optimistic_version(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    first_status,
    first_code,
    second_status,
    second_code,
) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, _ = _seed(db)
    feedback = _feedback(db, department, answered, owner)
    original_expiry = feedback.expires_at
    db.commit()
    path = f"/departments/{department.id}/rag/feedback/{feedback.id}"
    with _client(monkeypatch, tmp_path) as client:
        first = client.patch(
            path,
            headers=_headers(admin.subject),
            json={"status": first_status, "resolution_code": first_code, "expected_version": 1},
        )
        stale = client.patch(
            path,
            headers=_headers(admin.subject),
            json={"status": second_status, "resolution_code": second_code, "expected_version": 1},
        )
        second = client.patch(
            path,
            headers=_headers(admin.subject),
            json={"status": second_status, "resolution_code": second_code, "expected_version": 2},
        )
        terminal = client.patch(
            path,
            headers=_headers(admin.subject),
            json={"status": first_status, "resolution_code": first_code, "expected_version": 3},
        )
    assert first.status_code == 200
    assert stale.status_code == 409
    assert second.status_code == 200
    assert terminal.status_code == 409
    assert datetime.fromisoformat(second.json()["expires_at"]) == original_expiry
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.review"
            )
        )
        == 2
    )


def test_two_reviewers_with_same_version_apply_exactly_one_transition(db: Session, engine) -> None:
    department, _foreign, owner, _other, admin, instructor, *_rest, answered, _insufficient, _ = (
        _seed(db)
    )
    feedback = _feedback(db, department, answered, owner)
    db.commit()
    factory = create_session_factory(engine)
    barrier = Barrier(2)
    department_id = department.id
    feedback_id = feedback.id
    subjects = (admin.subject, instructor.subject)

    def worker(subject: str):
        try:
            with factory.begin() as session:
                barrier.wait()
                result = review_feedback(
                    session,
                    AuthenticatedPrincipal(subject, ISSUER),
                    DepartmentRequestScope(DepartmentScope(department_id)),
                    feedback_id,
                    new_status=FeedbackStatus.TRIAGED,
                    resolution_code=None,
                    expected_version=1,
                )
            return result.version
        except ServiceError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, subjects))
    assert sorted(results) == [2, 409]
    db.expire_all()
    assert db.get(RagAnswerFeedback, feedback_id).version == 2
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.review"
            )
        )
        == 1
    )


def test_review_queue_cursor_filters_and_expiry_visibility(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, insufficient, _ = _seed(db)
    first = _feedback(
        db,
        department,
        answered,
        owner,
        created_at=datetime.now(UTC) - timedelta(minutes=3),
    )
    second_owner = _identity(db, department, role="viewer", subject=f"second-{uuid4().hex}")
    second_run = _run(db, department, second_owner, "insufficient_information")
    second = _feedback(
        db,
        department,
        second_run,
        second_owner,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
        sentiment="report",
    )
    db.add(
        RagAnswerFeedbackReason(
            feedback_id=second.id,
            department_id=department.id,
            run_id=second_run.id,
            rank=1,
            reason_code="insufficient_when_expected",
        )
    )
    expired_owner = _identity(db, department, role="viewer", subject=f"expired-{uuid4().hex}")
    expired_run = _run(db, department, expired_owner, "insufficient_information")
    expired = _feedback(
        db,
        department,
        expired_run,
        expired_owner,
        created_at=datetime.now(UTC) - timedelta(days=2),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        page1 = client.get(
            f"/departments/{department.id}/rag/feedback?limit=1",
            headers=_headers(admin.subject),
        )
        cursor = page1.json()["next_cursor"]
        page2 = client.get(
            f"/departments/{department.id}/rag/feedback?limit=1&cursor={cursor}",
            headers=_headers(admin.subject),
        )
        wrong_filter = client.get(
            f"/departments/{department.id}/rag/feedback?limit=1&sentiment=report&cursor={cursor}",
            headers=_headers(admin.subject),
        )
        filtered = client.get(
            f"/departments/{department.id}/rag/feedback?sentiment=report",
            headers=_headers(admin.subject),
        )
        expired_read = client.get(
            f"/departments/{department.id}/rag/feedback/{expired.id}",
            headers=_headers(admin.subject),
        )
    assert [item["id"] for item in page1.json()["items"]] == [str(first.id)]
    assert [item["id"] for item in page2.json()["items"]] == [str(second.id)]
    assert wrong_filter.status_code == 422
    assert [item["id"] for item in filtered.json()["items"]] == [str(second.id)]
    assert expired_read.status_code == 404
    assert str(insufficient.id) != str(expired.id)


def test_purge_dry_run_apply_and_retained_run_citation_audit(db: Session) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, citations = _seed(
        db
    )
    created = datetime.now(UTC) - timedelta(days=3)
    feedback = _feedback(
        db,
        department,
        answered,
        owner,
        created_at=created,
        expires_at=created + timedelta(days=1),
        sentiment="unhelpful",
    )
    db.add(
        RagAnswerFeedbackReason(
            feedback_id=feedback.id,
            department_id=department.id,
            run_id=answered.id,
            rank=1,
            reason_code="wrong_citation",
        )
    )
    db.add(
        RagAnswerFeedbackSourceTarget(
            feedback_id=feedback.id,
            department_id=department.id,
            run_id=answered.id,
            citation_id=citations[0].id,
            rank=1,
        )
    )
    db.commit()
    principal = AuthenticatedPrincipal(admin.subject, ISSUER)
    scope = DepartmentRequestScope(DepartmentScope(department.id))
    dry = purge_feedback_batch(db, principal, scope, limit=500, apply=False)
    db.commit()
    assert dry.eligible_count == 1 and dry.purged_count == 0
    assert db.get(RagAnswerFeedback, feedback.id) is not None
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.purge"
            )
        )
        == 0
    )
    applied = purge_feedback_batch(db, principal, scope, limit=500, apply=True)
    db.commit()
    assert applied.purged_count == 1
    assert db.get(RagAnswerFeedback, feedback.id) is None
    assert db.get(RagAnswerRun, answered.id) is not None
    assert db.get(RagAnswerCitation, citations[0].id) is not None
    audits = list(
        db.scalars(
            select(PersistentAuditEvent).where(PersistentAuditEvent.action == "rag.feedback.purge")
        )
    )
    assert len(audits) == 1
    assert audits[0].resource_id == str(feedback.id)
    repeated = purge_feedback_batch(db, principal, scope, limit=500, apply=True)
    db.commit()
    assert repeated.purged_count == 0


def test_expired_owner_feedback_is_hidden_before_purge(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _ = _seed(db)
    created = datetime.now(UTC) - timedelta(days=2)
    _feedback(
        db,
        department,
        answered,
        owner,
        created_at=created,
        expires_at=created + timedelta(days=1),
    )
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.get(
            f"/departments/{department.id}/rag/answers/{answered.id}/feedback",
            headers=_headers(owner.subject),
        )
    assert response.status_code == 404


def test_purge_command_uses_only_database_settings(
    db: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, _ = _seed(db)
    created = datetime.now(UTC) - timedelta(days=2)
    feedback = _feedback(
        db,
        department,
        answered,
        owner,
        created_at=created,
        expires_at=created + timedelta(days=1),
    )
    feedback_id = feedback.id
    db.commit()
    monkeypatch.setenv("DATABASE_URL", _database_url())
    monkeypatch.delenv("DEPTSLM_DATA_DIR", raising=False)
    for name in (
        "DEPTSLM_QDRANT_URL",
        "DEPTSLM_RAG_RUNTIME_URL",
        "DEPTSLM_EMBEDDING_MODEL_REVISION",
        "DEPTSLM_GENERATION_MODEL_REVISION",
        "DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS",
    ):
        monkeypatch.setenv(name, "invalid-and-unused")
    monkeypatch.setattr(
        "app.admin.Settings.from_environment",
        lambda: pytest.fail("full settings must not load for purge"),
    )
    from app.admin import main

    assert (
        main(
            [
                "purge-rag-feedback",
                "--department-id",
                str(department.id),
                "--actor-issuer",
                ISSUER,
                "--actor-subject",
                admin.subject,
                "--apply",
            ]
        )
        == 0
    )
    assert "Purged feedback count: 1" in capsys.readouterr().out
    db.expire_all()
    assert db.get(RagAnswerFeedback, feedback_id) is None
    assert FeedbackPurgeSettings.from_environment().database_url == _database_url()


@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        (b"", 400),
        (b"\xff", 400),
        (b"{", 400),
        (b"[]", 400),
        (b'{} {"second":true}', 400),
        (
            json.dumps(
                {
                    "sentiment": "helpful",
                    "reason_codes": [],
                    "source_ids": [],
                    "unknown": "x" * 5000,
                }
            ).encode(),
            413,
        ),
    ],
)
def test_rejected_submit_bodies_create_no_feedback_or_audit(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    body: bytes,
    expected_status: int,
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _ = _seed(db)
    with _client(monkeypatch, tmp_path) as client:
        response = client.put(
            f"/departments/{department.id}/rag/answers/{answered.id}/feedback",
            headers={**_headers(owner.subject), "Content-Type": "application/json"},
            content=body,
        )
    assert response.status_code == expected_status
    assert response.json()["detail"] in {
        "Invalid feedback request body",
        "Feedback request body too large",
    }
    assert db.scalar(select(func.count()).select_from(RagAnswerFeedback)) == 0
    assert (
        db.scalar(
            select(func.count())
            .select_from(PersistentAuditEvent)
            .where(PersistentAuditEvent.action == "rag.feedback.submit")
        )
        == 0
    )


def test_chunked_multimegabyte_submit_is_rejected_before_model_or_service(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _ = _seed(db)
    model_calls: list[object] = []
    service_calls: list[object] = []

    def reject_model(cls, payload):
        model_calls.append(payload)
        pytest.fail("oversized body reached model validation")

    def reject_service(*args, **kwargs):
        service_calls.append((args, kwargs))
        pytest.fail("oversized body reached feedback service")

    monkeypatch.setattr(
        "app.routes.RagFeedbackSubmitRequest.model_validate",
        classmethod(reject_model),
    )
    monkeypatch.setattr("app.routes.submit_feedback", reject_service)

    def content():
        yield b'{"sentiment":"unhelpful","reason_codes":['
        for index in range(5):
            if index:
                yield b","
            yield b'"sensitive-marker-' + b"x" * (1024 * 1024) + b'"'
        yield b'],"source_ids":[]}'

    with _client(monkeypatch, tmp_path) as client:
        response = client.put(
            f"/departments/{department.id}/rag/answers/{answered.id}/feedback",
            headers={**_headers(owner.subject), "Content-Type": "application/json"},
            content=content(),
        )
    assert response.status_code == 413
    assert model_calls == service_calls == []
    assert "sensitive-marker" not in response.text
    assert "sensitive-marker" not in caplog.text
    assert db.scalar(select(func.count()).select_from(RagAnswerFeedback)) == 0


def test_feedback_submit_body_exact_limit_and_one_byte_over(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.feedback_request_body import FEEDBACK_SUBMIT_BODY_MAX_BYTES

    department, _foreign, owner, *_rest, answered, _insufficient, _ = _seed(db)
    raw = b'{"sentiment":"helpful","reason_codes":[],"source_ids":[]}'
    exact = raw + b" " * (FEEDBACK_SUBMIT_BODY_MAX_BYTES - len(raw))
    with _client(monkeypatch, tmp_path) as client:
        accepted = client.put(
            f"/departments/{department.id}/rag/answers/{answered.id}/feedback",
            headers={**_headers(owner.subject), "Content-Type": "application/json"},
            content=exact,
        )
        rejected = client.put(
            f"/departments/{department.id}/rag/answers/{answered.id}/feedback",
            headers={**_headers(owner.subject), "Content-Type": "application/json"},
            content=exact + b" ",
        )
    assert accepted.status_code == 201
    assert rejected.status_code == 413
    assert db.scalar(select(func.count()).select_from(RagAnswerFeedback)) == 1


def test_chunked_oversized_review_and_strict_version_do_not_mutate(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, _ = _seed(db)
    feedback = _feedback(db, department, answered, owner)
    db.commit()

    def oversized_content():
        yield b'{"status":"resolved","resolution_code":"'
        yield b"x" * 4096
        yield b'","expected_version":1}'

    path = f"/departments/{department.id}/rag/feedback/{feedback.id}"
    with _client(monkeypatch, tmp_path) as client:
        oversized = client.patch(
            path,
            headers={**_headers(admin.subject), "Content-Type": "application/json"},
            content=oversized_content(),
        )
        boolean_version = client.patch(
            path,
            headers=_headers(admin.subject),
            json={"status": "triaged", "resolution_code": None, "expected_version": True},
        )
    assert oversized.status_code == 413
    assert boolean_version.status_code == 422
    db.expire_all()
    assert db.get(RagAnswerFeedback, feedback.id).status == "open"
    assert (
        db.scalar(
            select(func.count())
            .select_from(PersistentAuditEvent)
            .where(PersistentAuditEvent.action == "rag.feedback.review")
        )
        == 0
    )


def _attach_negative_feedback_contract(
    db: Session,
    feedback: RagAnswerFeedback,
    citation: RagAnswerCitation,
) -> None:
    feedback.sentiment = "unhelpful"
    db.add(
        RagAnswerFeedbackReason(
            feedback_id=feedback.id,
            department_id=feedback.department_id,
            run_id=feedback.run_id,
            rank=1,
            reason_code="wrong_citation",
        )
    )
    db.add(
        RagAnswerFeedbackSourceTarget(
            feedback_id=feedback.id,
            department_id=feedback.department_id,
            run_id=feedback.run_id,
            citation_id=citation.id,
            rank=1,
        )
    )


def _wait_for_server_expiry(factory, expires_at: datetime) -> None:
    while True:
        with factory.begin() as session:
            if session.scalar(select(func.clock_timestamp())) >= expires_at:
                return
        time.sleep(0.02)


@pytest.mark.parametrize("read_kind", ["owner", "reviewer_detail", "reviewer_list"])
def test_feedback_read_snapshot_remains_complete_while_expiry_purge_commits(
    db: Session, engine, read_kind: str
) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, citations = _seed(
        db
    )
    created = datetime.now(UTC) - timedelta(days=1)
    expires_at = datetime.now(UTC) + timedelta(seconds=3)
    feedback = _feedback(
        db,
        department,
        answered,
        owner,
        created_at=created,
        expires_at=expires_at,
        sentiment="unhelpful",
    )
    _attach_negative_feedback_contract(db, feedback, citations[0])
    db.commit()
    factory = create_session_factory(engine)
    selected = Event()
    release = Event()
    department_id = department.id
    feedback_id = feedback.id
    run_id = answered.id

    def reader():
        with factory.begin() as session:
            connection = session.connection()

            def pause_after_snapshot(conn, cursor, statement, parameters, context, executemany):
                del conn, cursor, parameters, context, executemany
                if " AS reason_codes" in statement and not selected.is_set():
                    selected.set()
                    assert release.wait(10)

            sqlalchemy_event.listen(connection, "after_cursor_execute", pause_after_snapshot)
            if read_kind == "owner":
                return read_own_feedback(
                    session,
                    AuthenticatedPrincipal(owner.subject, ISSUER),
                    DepartmentRequestScope(DepartmentScope(department_id)),
                    run_id,
                )
            if read_kind == "reviewer_detail":
                return read_feedback_for_review(
                    session,
                    AuthenticatedPrincipal(admin.subject, ISSUER),
                    DepartmentRequestScope(DepartmentScope(department_id)),
                    feedback_id,
                )
            return list_feedback_for_review(
                session,
                AuthenticatedPrincipal(admin.subject, ISSUER),
                DepartmentRequestScope(DepartmentScope(department_id)),
                status=None,
                sentiment=None,
                limit=25,
                cursor=None,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(reader)
        assert selected.wait(10)
        _wait_for_server_expiry(factory, expires_at)
        with factory.begin() as session:
            result = purge_feedback_batch(
                session,
                AuthenticatedPrincipal(admin.subject, ISSUER),
                DepartmentRequestScope(DepartmentScope(department_id)),
                limit=1,
                apply=True,
            )
        assert result.purged_count == 1
        release.set()
        response = future.result(timeout=10)
    item = response.items[0] if read_kind == "reviewer_list" else response
    assert item.reason_codes == ["wrong_citation"]
    assert item.source_ids == ["S1"]
    db.expire_all()
    assert db.get(RagAnswerFeedback, feedback_id) is None
    assert db.get(RagAnswerFeedbackReason, (feedback_id, 1)) is None
    assert db.get(RagAnswerFeedbackSourceTarget, (feedback_id, 1)) is None
    assert (
        db.scalar(
            select(func.count())
            .select_from(PersistentAuditEvent)
            .where(PersistentAuditEvent.action == "rag.feedback.purge")
        )
        == 1
    )


def test_review_waiting_on_lock_rechecks_server_time_after_expiry(db: Session, engine) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, _ = _seed(db)
    created = datetime.now(UTC) - timedelta(days=1)
    expires_at = datetime.now(UTC) + timedelta(seconds=3)
    feedback = _feedback(
        db,
        department,
        answered,
        owner,
        created_at=created,
        expires_at=expires_at,
    )
    db.commit()
    factory = create_session_factory(engine)
    department_id = department.id
    feedback_id = feedback.id

    def reviewer():
        try:
            with factory.begin() as session:
                review_feedback(
                    session,
                    AuthenticatedPrincipal(admin.subject, ISSUER),
                    DepartmentRequestScope(DepartmentScope(department_id)),
                    feedback_id,
                    new_status=FeedbackStatus.TRIAGED,
                    resolution_code=None,
                    expected_version=1,
                )
        except ServiceError as error:
            return error.status_code
        return 200

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as blocker:
            blocker.execute(
                select(RagAnswerFeedback)
                .where(RagAnswerFeedback.id == feedback_id)
                .with_for_update()
            ).scalar_one()
            future = pool.submit(reviewer)
            _wait_for_server_expiry(factory, expires_at)
        result = future.result(timeout=10)
    assert result == 404
    db.expire_all()
    assert db.get(RagAnswerFeedback, feedback_id).status == "open"
    assert (
        db.scalar(
            select(func.count())
            .select_from(PersistentAuditEvent)
            .where(PersistentAuditEvent.action == "rag.feedback.review")
        )
        == 0
    )


def test_owner_read_snapshot_is_complete_during_review_transition(db: Session, engine) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, citations = _seed(
        db
    )
    feedback = _feedback(db, department, answered, owner, sentiment="unhelpful")
    _attach_negative_feedback_contract(db, feedback, citations[0])
    db.commit()
    factory = create_session_factory(engine)
    selected = Event()
    release = Event()
    department_id = department.id
    feedback_id = feedback.id
    run_id = answered.id

    def owner_reader():
        with factory.begin() as session:
            connection = session.connection()

            def pause_after_snapshot(conn, cursor, statement, parameters, context, executemany):
                del conn, cursor, parameters, context, executemany
                if " AS reason_codes" in statement and not selected.is_set():
                    selected.set()
                    assert release.wait(10)

            sqlalchemy_event.listen(connection, "after_cursor_execute", pause_after_snapshot)
            return read_own_feedback(
                session,
                AuthenticatedPrincipal(owner.subject, ISSUER),
                DepartmentRequestScope(DepartmentScope(department_id)),
                run_id,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(owner_reader)
        assert selected.wait(10)
        with factory.begin() as session:
            reviewed = review_feedback(
                session,
                AuthenticatedPrincipal(admin.subject, ISSUER),
                DepartmentRequestScope(DepartmentScope(department_id)),
                feedback_id,
                new_status=FeedbackStatus.TRIAGED,
                resolution_code=None,
                expected_version=1,
            )
        release.set()
        response = future.result(timeout=10)
    assert reviewed.status == FeedbackStatus.TRIAGED
    assert response.status == FeedbackStatus.OPEN
    assert response.version == 1
    assert response.reason_codes == ["wrong_citation"]
    assert response.source_ids == ["S1"]
    db.expire_all()
    assert db.get(RagAnswerFeedback, feedback_id).status == "triaged"


def test_unrelated_expired_purge_does_not_alter_active_feedback_read(db: Session, engine) -> None:
    department, _foreign, owner, other, admin, *_rest, answered, _insufficient, citations = _seed(
        db
    )
    active = _feedback(db, department, answered, owner, sentiment="unhelpful")
    _attach_negative_feedback_contract(db, active, citations[0])
    expired_run = _run(db, department, other, "insufficient_information")
    created = datetime.now(UTC) - timedelta(days=2)
    expired = _feedback(
        db,
        department,
        expired_run,
        other,
        created_at=created,
        expires_at=created + timedelta(days=1),
    )
    active_id = active.id
    expired_id = expired.id
    db.commit()
    factory = create_session_factory(engine)
    barrier = Barrier(2)
    department_id = department.id

    def read_active():
        with factory.begin() as session:
            barrier.wait()
            return read_feedback_for_review(
                session,
                AuthenticatedPrincipal(admin.subject, ISSUER),
                DepartmentRequestScope(DepartmentScope(department_id)),
                active.id,
            )

    def purge_expired():
        with factory.begin() as session:
            barrier.wait()
            return purge_feedback_batch(
                session,
                AuthenticatedPrincipal(admin.subject, ISSUER),
                DepartmentRequestScope(DepartmentScope(department_id)),
                limit=1,
                apply=True,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(read_active)
        purge_future = pool.submit(purge_expired)
        response = read_future.result(timeout=10)
        purge = purge_future.result(timeout=10)
    assert response.id == active.id
    assert response.reason_codes == ["wrong_citation"]
    assert response.source_ids == ["S1"]
    assert purge.purged_count == 1
    db.expire_all()
    assert db.get(RagAnswerFeedback, active_id) is not None
    assert db.get(RagAnswerFeedback, expired_id) is None


def test_membership_revocation_prevents_review_without_audit(
    db: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, _ = _seed(db)
    feedback = _feedback(db, department, answered, owner)
    membership = db.scalar(
        select(Membership).where(
            Membership.department_id == department.id,
            Membership.user_id == admin.id,
        )
    )
    membership.status = "revoked"
    db.commit()
    with _client(monkeypatch, tmp_path) as client:
        response = client.patch(
            f"/departments/{department.id}/rag/feedback/{feedback.id}",
            headers=_headers(admin.subject),
            json={"status": "triaged", "resolution_code": None, "expected_version": 1},
        )
    assert response.status_code == 403
    assert db.get(RagAnswerFeedback, feedback.id).status == "open"
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.review"
            )
        )
        == 0
    )


def test_database_failure_rolls_back_purge_children_parent_and_audit(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    department, _foreign, owner, _other, admin, *_rest, answered, _insufficient, _ = _seed(db)
    created = datetime.now(UTC) - timedelta(days=2)
    feedback = _feedback(
        db,
        department,
        answered,
        owner,
        created_at=created,
        expires_at=created + timedelta(days=1),
        sentiment="unhelpful",
    )
    reason = RagAnswerFeedbackReason(
        feedback_id=feedback.id,
        department_id=department.id,
        run_id=answered.id,
        rank=1,
        reason_code="incorrect",
    )
    db.add(reason)
    db.commit()
    admin_subject = admin.subject
    department_id = department.id
    original_flush = db.flush
    calls = 0

    def fail_after_audit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError("synthetic", {}, RuntimeError())
        return original_flush(*args, **kwargs)

    monkeypatch.setattr(db, "flush", fail_after_audit)
    with pytest.raises(ServiceError):
        purge_feedback_batch(
            db,
            AuthenticatedPrincipal(admin_subject, ISSUER),
            DepartmentRequestScope(DepartmentScope(department_id)),
            limit=1,
            apply=True,
        )
    db.rollback()
    assert db.get(RagAnswerFeedback, feedback.id) is not None
    assert db.get(RagAnswerFeedbackReason, (feedback.id, 1)) is not None
    assert (
        db.scalar(
            select(func_count(PersistentAuditEvent)).where(
                PersistentAuditEvent.action == "rag.feedback.purge"
            )
        )
        == 0
    )


def test_feedback_timestamps_are_timezone_aware(db: Session) -> None:
    department, _foreign, owner, *_rest, answered, _insufficient, _ = _seed(db)
    feedback = _feedback(db, department, answered, owner)
    db.commit()
    assert feedback.created_at.utcoffset() is not None
    assert feedback.updated_at.utcoffset() is not None
    assert feedback.expires_at.utcoffset() is not None
