"""PostgreSQL 16 integration coverage for Phase 9 metadata and leases."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from alembic.config import Config
from deptslm_worker.evaluation_queue import (
    EvaluationQueueError,
    cancel_owned,
    claim_next,
    finalize_success,
    require_live_claim,
)
from fastapi.testclient import TestClient
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.evaluation_artifacts import EvaluationArtifactStore
from app.evaluation_domain import (
    ANSWER_NORMALIZATION_VERSION,
    ARTIFACT_CONTRACT_VERSION,
    GATE_POLICY_VERSION,
    METRIC_CONTRACT_VERSION,
    RUNNER_CONTRACT_VERSION,
    AggregateMetrics,
    EvaluationCaseScore,
    GateEvaluation,
    production_contract,
)
from app.evaluation_services import (
    cancel_evaluation_run,
    enqueue_evaluation_run,
    list_evaluation_runs,
    list_evaluation_suites,
)
from app.main import app
from app.models import (
    Base,
    Department,
    EvaluationCaseResult,
    EvaluationRun,
    EvaluationSuite,
    Membership,
    PersistentAuditEvent,
    UserIdentity,
)
from app.services import ServiceError

pytestmark = pytest.mark.postgres
ISSUER = "https://phase9.issuer.invalid"
AUDIENCE = "phase9-tests"
SECRET = "phase-9-test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
CODE_REVISION = "9" * 40


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
        session.execute(delete(EvaluationCaseResult))
        session.execute(delete(EvaluationRun))
        session.execute(delete(EvaluationSuite))
        session.execute(delete(PersistentAuditEvent))
        session.execute(delete(Membership))
        session.execute(delete(Department))
        session.execute(delete(UserIdentity))
        session.commit()
        yield session
        session.rollback()


def test_00_phase9_migration_paths_and_orm_sync(engine) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0006_phase8_rag_feedback")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT to_regclass('evaluation_suites')")) is None
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007_phase9_evaluation_runner"
        )
    inspector = inspect(engine)
    tables = {"evaluation_suites", "evaluation_runs", "evaluation_case_results"}
    assert tables <= set(inspector.get_table_names())
    for table in tables:
        assert {column["name"] for column in inspector.get_columns(table)} == {
            column.name for column in Base.metadata.tables[table].columns
        }
        assert all(
            key["options"].get("ondelete") == "RESTRICT"
            for key in inspector.get_foreign_keys(table)
        )
    columns = {column["name"] for table in tables for column in inspector.get_columns(table)}
    assert columns.isdisjoint(
        {
            "question",
            "accepted_answer",
            "generated_answer",
            "answer",
            "prompt",
            "evidence",
            "text",
            "vector",
            "chunk_id",
            "document_id",
            "extraction_id",
            "indexing_id",
            "path",
            "runtime_response",
        }
    )


def _department(db: Session, slug: str) -> Department:
    department = Department(slug=slug, display_name=slug, status="active")
    db.add(department)
    db.flush()
    return department


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


def _suite(db: Session, department: Department, identity: UserIdentity) -> EvaluationSuite:
    suite = EvaluationSuite(
        department_id=department.id,
        imported_by_user_id=identity.id,
        status="active",
        suite_contract_version="phase9-evaluation-suite-v1",
        artifact_contract_version=ARTIFACT_CONTRACT_VERSION,
        metric_contract_version=METRIC_CONTRACT_VERSION,
        answer_normalization_version=ANSWER_NORMALIZATION_VERSION,
        gate_policy_version=GATE_POLICY_VERSION,
        case_count=1,
        answered_case_count=1,
        insufficient_case_count=0,
        artifact_manifest_sha256="a" * 64,
        canonical_cases_sha256="b" * 64,
        canonical_cases_byte_size=10,
        retrieval_recall_at_5_min=Decimal("0.8"),
        retrieval_mrr_at_20_min=Decimal("0.7"),
        answer_status_accuracy_min=Decimal("0.9"),
        citation_precision_min=Decimal("0.9"),
        citation_recall_min=Decimal("0.8"),
        normalized_exact_match_min=Decimal("0.6"),
        character_f1_min=Decimal("0.75"),
        invalid_contract_rate_max=Decimal("0"),
    )
    db.add(suite)
    db.flush()
    return suite


def _scope(department: Department) -> DepartmentRequestScope:
    return DepartmentRequestScope(DepartmentScope(department.id))


def _principal(identity: UserIdentity) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(identity.subject, identity.issuer)


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
    (tmp_path / "eval_results").mkdir(exist_ok=True)
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
        "DEPTSLM_EVALUATION_CODE_REVISION": CODE_REVISION,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return TestClient(app)


def test_suite_constraints_and_cross_department_run_fk(db: Session) -> None:
    first = _department(db, "phase9-first")
    second = _department(db, "phase9-second")
    actor = _identity(db, first, "department_admin", "schema-admin")
    suite = _suite(db, first, actor)
    foreign_actor = _identity(db, second, "department_admin", "foreign-admin")
    run = EvaluationRun(
        department_id=second.id,
        suite_id=suite.id,
        requested_by_user_id=foreign_actor.id,
        status="queued",
        gate_status="pending",
        runner_contract_version=RUNNER_CONTRACT_VERSION,
        code_revision=CODE_REVISION,
        base_seed=1,
        case_count=1,
        completed_case_count=0,
        answered_case_count=0,
        insufficient_case_count=0,
        **production_contract(),
    )
    db.add(run)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("system_admin", True),
        ("department_admin", True),
        ("instructor", True),
        ("student", False),
        ("viewer", False),
    ],
)
def test_evaluator_role_matrix(db: Session, role: str, allowed: bool) -> None:
    department = _department(db, f"phase9-{role.replace('_', '-')}")
    actor = _identity(db, department, role, f"{role}-actor")
    importer = actor
    if not allowed:
        importer = _identity(db, department, "department_admin", f"{role}-suite-importer")
    suite = _suite(db, department, importer)
    if allowed:
        run = enqueue_evaluation_run(
            db,
            _principal(actor),
            _scope(department),
            suite.id,
            code_revision=CODE_REVISION,
        )
        assert run.status == "queued"
    else:
        with pytest.raises(ServiceError) as caught:
            enqueue_evaluation_run(
                db,
                _principal(actor),
                _scope(department),
                suite.id,
                code_revision=CODE_REVISION,
            )
        assert caught.value.status_code == 403
    db.rollback()


def test_system_admin_has_no_cross_department_bypass(db: Session) -> None:
    first = _department(db, "phase9-system-first")
    second = _department(db, "phase9-system-second")
    actor = _identity(db, first, "system_admin", "same-only-system")
    importer = _identity(db, second, "department_admin", "second-importer")
    suite = _suite(db, second, importer)
    with pytest.raises(ServiceError) as caught:
        enqueue_evaluation_run(
            db,
            _principal(actor),
            _scope(second),
            suite.id,
            code_revision=CODE_REVISION,
        )
    assert caught.value.status_code == 403


def test_list_and_read_surfaces_are_content_free(db: Session) -> None:
    department = _department(db, "phase9-list")
    actor = _identity(db, department, "instructor", "list-instructor")
    suite = _suite(db, department, actor)
    run = enqueue_evaluation_run(
        db,
        _principal(actor),
        _scope(department),
        suite.id,
        code_revision=CODE_REVISION,
    )
    suite_page = list_evaluation_suites(
        db, _principal(actor), _scope(department), limit=25, cursor=None
    )
    run_page = list_evaluation_runs(
        db, _principal(actor), _scope(department), limit=25, cursor=None
    )
    assert suite_page.items == (suite,)
    assert suite_page.next_cursor is None
    assert run_page.items == (run,)
    assert run_page.next_cursor is None


def test_cancel_requires_version_and_running_worker_observes_request(db: Session, engine) -> None:
    department = _department(db, "phase9-cancel")
    actor = _identity(db, department, "instructor", "cancel-instructor")
    suite = _suite(db, department, actor)
    run = enqueue_evaluation_run(
        db,
        _principal(actor),
        _scope(department),
        suite.id,
        code_revision=CODE_REVISION,
    )
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 300, CODE_REVISION)
    assert job is not None and job.id == run.id
    with Session(engine) as session:
        current = session.get(EvaluationRun, run.id)
        with pytest.raises(ServiceError):
            cancel_evaluation_run(
                session,
                _principal(actor),
                _scope(department),
                run.id,
                expected_version=current.version + 1,
            )
        session.rollback()
        current = session.get(EvaluationRun, run.id)
        cancelled = cancel_evaluation_run(
            session,
            _principal(actor),
            _scope(department),
            run.id,
            expected_version=current.version,
        )
        session.commit()
        assert cancelled.status == "running"
    with pytest.raises(EvaluationQueueError) as caught:
        require_live_claim(factory, job)
    assert caught.value.code == "cancelled"
    assert cancel_owned(factory, job)
    with Session(engine) as session:
        terminal = session.get(EvaluationRun, run.id)
        assert terminal.status == "cancelled"
        assert terminal.error_code == "cancelled"


def test_expired_claim_is_reclaimed_with_fresh_token_and_attempt(db: Session, engine) -> None:
    department = _department(db, "phase9-reclaim")
    actor = _identity(db, department, "department_admin", "reclaim-admin")
    suite = _suite(db, department, actor)
    run = enqueue_evaluation_run(
        db,
        _principal(actor),
        _scope(department),
        suite.id,
        code_revision=CODE_REVISION,
    )
    db.commit()
    factory = create_session_factory(engine)
    first = claim_next(factory, uuid4(), 300, CODE_REVISION)
    assert first is not None
    with factory.begin() as session:
        current = session.get(EvaluationRun, run.id)
        current.lease_expires_at = session.scalar(select(text("clock_timestamp()"))) - timedelta(
            seconds=1
        )
    replacement = claim_next(factory, uuid4(), 300, CODE_REVISION)
    assert replacement is not None
    assert replacement.claim_token != first.claim_token
    assert replacement.stale_claim_token == first.claim_token
    with pytest.raises(EvaluationQueueError):
        require_live_claim(factory, first)
    with Session(engine) as session:
        assert session.get(EvaluationRun, run.id).attempt_number == 2


def test_two_workers_claim_distinct_runs(db: Session, engine) -> None:
    department = _department(db, "phase9-distinct-claims")
    actor = _identity(db, department, "instructor", "distinct-claim-instructor")
    suite = _suite(db, department, actor)
    runs = {
        enqueue_evaluation_run(
            db,
            _principal(actor),
            _scope(department),
            suite.id,
            code_revision=CODE_REVISION,
        ).id
        for _ in range(2)
    }
    db.commit()
    factory = create_session_factory(engine)
    claimed = {claim_next(factory, uuid4(), 300, CODE_REVISION).id for _ in range(2)}
    assert claimed == runs


def test_worker_claim_is_bound_to_exact_code_revision(db: Session, engine) -> None:
    department = _department(db, "phase9-code-revision")
    actor = _identity(db, department, "instructor", "code-revision-instructor")
    suite = _suite(db, department, actor)
    run = enqueue_evaluation_run(
        db,
        _principal(actor),
        _scope(department),
        suite.id,
        code_revision=CODE_REVISION,
    )
    db.commit()
    factory = create_session_factory(engine)
    assert claim_next(factory, uuid4(), 300, "8" * 40) is None
    with Session(engine) as session:
        assert session.get(EvaluationRun, run.id).status == "queued"
    assert claim_next(factory, uuid4(), 300, CODE_REVISION).id == run.id


def test_requester_revocation_blocks_publication_and_completion_audit(
    db: Session,
    engine,
    tmp_path: Path,
) -> None:
    department = _department(db, "phase9-revoked-finalization")
    actor = _identity(db, department, "instructor", "revoked-instructor")
    suite = _suite(db, department, actor)
    run = enqueue_evaluation_run(
        db,
        _principal(actor),
        _scope(department),
        suite.id,
        code_revision=CODE_REVISION,
    )
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 300, CODE_REVISION)
    assert job is not None
    with factory.begin() as session:
        membership = session.scalar(
            select(Membership).where(
                Membership.department_id == department.id,
                Membership.user_id == actor.id,
            )
        )
        membership.status = "revoked"
    root = tmp_path / "runtime"
    (root / "eval_results").mkdir(parents=True)
    store = EvaluationArtifactStore(root)
    score = _score_for_finalization()
    metrics = _metrics_for_finalization()
    gate = GateEvaluation(True, 0, {"retrieval_recall_at_5_min": True})
    staged, summary = store.stage_run(
        DepartmentScope(department.id),
        suite.id,
        run.id,
        job.claim_token,
        manifest_value={"case_count": 1},
        summary_value={"failed_gate_count": 0},
        scores=(score,),
    )
    with pytest.raises(EvaluationQueueError) as caught:
        finalize_success(factory, store, job, staged, summary, (score,), metrics, gate)
    assert caught.value.code == "requester_unauthorized"
    assert not staged.final_path.exists()
    with Session(engine) as session:
        assert (
            session.scalar(
                select(PersistentAuditEvent).where(
                    PersistentAuditEvent.action == "evaluation.run.complete",
                    PersistentAuditEvent.resource_id == str(run.id),
                )
            )
            is None
        )


def _score_for_finalization() -> EvaluationCaseScore:
    return EvaluationCaseScore(
        case_id=uuid4(),
        expected_status="answered",
        actual_status="answered",
        relevant_chunk_count=1,
        retrieved_relevant_at_5=1,
        retrieved_relevant_at_10=1,
        retrieved_relevant_at_20=1,
        reciprocal_rank_at_20=Decimal(1),
        status_correct=True,
        cited_count=1,
        cited_relevant_count=1,
        citation_precision=Decimal(1),
        citation_recall=Decimal(1),
        normalized_exact_match=Decimal(1),
        character_f1=Decimal(1),
        answer_contract_valid=True,
        case_gate_passed=True,
        error_code=None,
    )


def _metrics_for_finalization() -> AggregateMetrics:
    return AggregateMetrics(
        retrieval_recall_at_5=Decimal(1),
        retrieval_recall_at_10=Decimal(1),
        retrieval_recall_at_20=Decimal(1),
        retrieval_mrr_at_20=Decimal(1),
        answer_status_accuracy=Decimal(1),
        citation_precision=Decimal(1),
        citation_recall=Decimal(1),
        normalized_exact_match=Decimal(1),
        character_f1=Decimal(1),
        invalid_contract_rate=Decimal(0),
    )


def test_quality_gate_failure_finishes_succeeded_and_audited(
    db: Session, engine, tmp_path: Path
) -> None:
    department = _department(db, "phase9-gate")
    actor = _identity(db, department, "department_admin", "gate-admin")
    suite = _suite(db, department, actor)
    run = enqueue_evaluation_run(
        db,
        _principal(actor),
        _scope(department),
        suite.id,
        code_revision=CODE_REVISION,
    )
    db.commit()
    factory = create_session_factory(engine)
    job = claim_next(factory, uuid4(), 300, CODE_REVISION)
    assert job is not None
    root = tmp_path / "runtime"
    (root / "eval_results").mkdir(parents=True)
    store = EvaluationArtifactStore(root)
    score = EvaluationCaseScore(
        case_id=uuid4(),
        expected_status="answered",
        actual_status="answered",
        relevant_chunk_count=1,
        retrieved_relevant_at_5=0,
        retrieved_relevant_at_10=0,
        retrieved_relevant_at_20=0,
        reciprocal_rank_at_20=Decimal(0),
        status_correct=True,
        cited_count=1,
        cited_relevant_count=0,
        citation_precision=Decimal(0),
        citation_recall=Decimal(0),
        normalized_exact_match=Decimal(0),
        character_f1=Decimal("0.5"),
        answer_contract_valid=True,
        case_gate_passed=True,
        error_code=None,
    )
    metrics = AggregateMetrics(
        retrieval_recall_at_5=Decimal(0),
        retrieval_recall_at_10=Decimal(0),
        retrieval_recall_at_20=Decimal(0),
        retrieval_mrr_at_20=Decimal(0),
        answer_status_accuracy=Decimal(1),
        citation_precision=Decimal(0),
        citation_recall=Decimal(0),
        normalized_exact_match=Decimal(0),
        character_f1=Decimal("0.5"),
        invalid_contract_rate=Decimal(0),
    )
    gate = GateEvaluation(False, 6, {"retrieval_recall_at_5_min": False})
    staged, summary = store.stage_run(
        DepartmentScope(department.id),
        suite.id,
        run.id,
        job.claim_token,
        manifest_value={"case_count": 1},
        summary_value={"failed_gate_count": 6},
        scores=(score,),
    )
    finalize_success(factory, store, job, staged, summary, (score,), metrics, gate)
    with Session(engine) as session:
        current = session.get(EvaluationRun, run.id)
        assert current.status == "succeeded"
        assert current.gate_status == "failed"
        assert current.failed_gate_count == 6
        assert session.scalar(
            select(PersistentAuditEvent).where(
                PersistentAuditEvent.action == "evaluation.run.complete",
                PersistentAuditEvent.resource_id == str(run.id),
            )
        )


def test_evaluation_api_authorization_bodies_cursors_and_public_safety(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    department = _department(db, "phase9-api")
    foreign = _department(db, "phase9-api-foreign")
    evaluator = _identity(db, department, "instructor", "api-evaluator")
    student = _identity(db, department, "student", "api-student")
    foreign_importer = _identity(db, foreign, "department_admin", "api-foreign")
    first = _suite(db, department, evaluator)
    _suite(db, department, evaluator)
    hidden = _suite(db, foreign, foreign_importer)
    db.commit()
    base = f"/departments/{department.id}/evaluation-suites"
    with _client(monkeypatch, tmp_path) as client:
        unauthenticated = client.get(base)
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["WWW-Authenticate"] == "Bearer"
        denied = client.get(base, headers=_headers(student.subject))
        assert denied.status_code == 403
        assert "WWW-Authenticate" not in denied.headers
        page = client.get(f"{base}?limit=1", headers=_headers(evaluator.subject))
        assert page.status_code == 200
        assert len(page.json()["items"]) == 1
        cursor = page.json()["next_cursor"]
        assert isinstance(cursor, str)
        next_page = client.get(
            f"{base}?limit=1&cursor={cursor}", headers=_headers(evaluator.subject)
        )
        assert next_page.status_code == 200
        malformed = client.get(f"{base}?cursor=not-a-cursor", headers=_headers(evaluator.subject))
        assert malformed.status_code == 422
        hidden_read = client.get(f"{base}/{hidden.id}", headers=_headers(evaluator.subject))
        assert hidden_read.status_code == 404
        extra = client.post(
            f"{base}/{first.id}/runs",
            headers=_headers(evaluator.subject),
            json={"seed": 1},
        )
        assert extra.status_code == 422
        oversized = client.post(
            f"{base}/{first.id}/runs",
            headers={
                **_headers(evaluator.subject),
                "Content-Type": "application/json",
            },
            content=b'{"padding":"' + b"x" * 256 + b'"}',
        )
        assert oversized.status_code == 413
        created = client.post(
            f"{base}/{first.id}/runs",
            headers=_headers(evaluator.subject),
            json={},
        )
        assert created.status_code == 202
        value = created.json()
        prohibited = {
            "requested_by_user_id",
            "worker_id",
            "claim_token",
            "question",
            "answer",
            "prompt",
            "evidence",
            "path",
            "result_manifest_sha256",
        }
        assert set(value).isdisjoint(prohibited)
        assert (
            client.get(f"{base}/{first.id}/cases", headers=_headers(evaluator.subject)).status_code
            == 404
        )
        assert (
            client.get(
                f"/departments/{department.id}/evaluation-runs/{value['id']}/artifact",
                headers=_headers(evaluator.subject),
            ).status_code
            == 404
        )
