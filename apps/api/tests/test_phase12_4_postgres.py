"""PostgreSQL admission and migration coverage for Phase 12.4 routing."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_phase7_postgres as phase7_tests
import test_phase12_3_postgres as phase12_3_tests
from alembic.config import Config
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from test_phase7_postgres import _client, _headers, _hit, _seed

from alembic import command
from app.adapter_purge import _finalize_operation, _mark_operation_deleting, _register_or_resume
from app.database import create_database_engine
from app.main import app
from app.models import (
    Adapter,
    AdapterDeploymentEvent,
    AdapterDeploymentOperation,
    AdapterEvaluationEvidence,
    AdapterEvaluationRun,
    AdapterImportAttempt,
    AdapterImportSource,
    AdapterPurgeItem,
    AdapterPurgeOperation,
    AdapterPurgeReservation,
    AdapterRegistryAttempt,
    AdapterReview,
    AdapterRollbackRetention,
    AdapterUpstreamDependency,
    Department,
    DepartmentAdapterDeployment,
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
    EvaluationSuite,
    PersistentAuditEvent,
    RagAnswerRun,
    RagAnswerRuntimeSnapshot,
    UserIdentity,
)
from app.rag_answer_services import _start_run
from app.rag_runtime_router import RoutedRagRuntime
from app.services import ServiceError
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
def clean_db(engine):
    return engine


@pytest.fixture
def factory(engine):
    return sessionmaker(engine)


def _seed_unique(session, tmp_path: Path):
    original = phase7_tests._identity

    def identity(db, department, role, subject):
        return original(db, department, role, f"{subject}-{uuid4().hex}")

    phase7_tests._identity = identity
    try:
        return _seed(session, tmp_path)
    finally:
        phase7_tests._identity = original


def _seed_adapter_rag_source(session: Session, authority, tmp_path: Path):
    """Add one real Phase 7 source to the Phase 12.3 authority department."""

    department = session.get(Department, authority.department_id)
    actor = session.get(UserIdentity, authority.admin_id)
    assert department is not None and actor is not None
    source = b"The deployed adapter policy is approved for testing."
    document = Document(
        department_id=department.id,
        uploaded_by_user_id=actor.id,
        original_filename="adapter-policy.txt",
        media_type="text/plain",
        byte_size=len(source),
        sha256=hashlib.sha256(source).hexdigest(),
    )
    session.add(document)
    session.flush()
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
    session.add(extraction)
    session.flush()
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
    session.add(chunk)
    session.flush()
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
    session.add(indexing)
    session.flush()
    extraction.output_byte_size = phase7_tests._write_artifact(
        tmp_path, department, document, extraction, chunk, source
    )
    session.commit()
    return department, document, extraction, chunk, indexing


def test_phase12_4_migration_cycle_has_one_head_and_content_free_snapshot(clean_db) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0016_phase12_adapter_governance")
    command.upgrade(config, "0017_phase12_adapter_runtime_routing")
    command.upgrade(config, "0017_phase12_adapter_runtime_routing")
    with clean_db.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0017_phase12_adapter_runtime_routing"
        )
    columns = {
        column["name"] for column in inspect(clean_db).get_columns("rag_answer_runtime_snapshots")
    }
    assert {
        "run_id",
        "department_id",
        "target_kind",
        "target_fingerprint",
        "runtime_contract_version",
        "adapter_config_sha256",
        "adapter_model_sha256",
    } <= columns
    assert not columns & {
        "question",
        "answer",
        "prompt",
        "evidence",
        "source_text",
        "path",
        "token",
        "vector",
        "adapter_bytes",
    }


def test_phase12_4_downgrade_maps_populated_runtime_error_codes(clean_db) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0016_phase12_adapter_governance")
    command.upgrade(config, "0017_phase12_adapter_runtime_routing")
    codes = (
        "adapter_runtime_timeout",
        "adapter_runtime_unavailable",
        "adapter_load_failed",
        "adapter_runtime_target_mismatch",
        "deployment_authority_changed",
    )
    with Session(clean_db) as session:
        department = Department(slug=f"migration-{uuid4().hex[:8]}", display_name="Migration proof")
        identity = UserIdentity(
            issuer="https://phase12-4.invalid",
            subject=f"migration-{uuid4().hex}",
            status="active",
        )
        session.add_all([department, identity])
        session.flush()
        session.add(
            DepartmentAdapterDeployment(
                department_id=department.id,
                target_kind="base",
                base_model_id="Qwen/Qwen3-0.6B",
                base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
                deployment_version=1,
                version=1,
            )
        )
        runs = [
            RagAnswerRun(
                department_id=department.id,
                requested_by_user_id=identity.id,
                status="failed",
                question_char_count=1,
                query_embedding_pipeline_version="phase7-qwen3-query-embedding-v1",
                query_embedding_model_id="Qwen/Qwen3-Embedding-0.6B",
                query_embedding_model_revision="d23109d65ca9fdf61eef614209744716f337f50f",
                generation_model_id="Qwen/Qwen3-0.6B",
                generation_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
                prompt_version="phase7-grounded-answer-prompt-v1",
                answer_contract_version="phase7-grounded-answer-v1",
                minimum_score=Decimal("0.100"),
                error_code=code,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                version=1,
            )
            for code in (*codes, "runtime_timeout")
        ]
        session.add_all(runs)
        session.flush()
        session.add(
            RagAnswerRuntimeSnapshot(
                run_id=runs[0].id,
                department_id=department.id,
                target_kind="base",
                deployment_id=None,
                deployment_version=0,
                deployment_row_version=None,
                base_model_id="Qwen/Qwen3-0.6B",
                base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
                runtime_contract_version="phase12-adapter-runtime-routing-v1",
                target_fingerprint="a" * 64,
            )
        )
        session.commit()
        run_ids = tuple(run.id for run in runs)
        department_id = department.id

    command.downgrade(config, "0016_phase12_adapter_governance")
    expected = {
        "adapter_runtime_timeout": "runtime_timeout",
        "adapter_runtime_unavailable": "runtime_unavailable",
        "adapter_load_failed": "runtime_unavailable",
        "adapter_runtime_target_mismatch": "runtime_unavailable",
        "deployment_authority_changed": "runtime_unavailable",
        "runtime_timeout": "runtime_timeout",
    }
    with Session(clean_db) as session:
        values = {
            run.id: run.error_code
            for run in session.query(RagAnswerRun).filter(RagAnswerRun.id.in_(run_ids))
        }
        assert [values[run_id] for run_id in run_ids] == [
            expected[code] for code in (*codes, "runtime_timeout")
        ]
        assert (
            session.query(DepartmentAdapterDeployment)
            .filter_by(department_id=department_id)
            .count()
            == 1
        )

    command.upgrade(config, "0017_phase12_adapter_runtime_routing")
    with Session(clean_db) as session:
        restored = RagAnswerRuntimeSnapshot(
            run_id=run_ids[0],
            department_id=department_id,
            target_kind="base",
            deployment_id=None,
            deployment_version=0,
            deployment_row_version=None,
            base_model_id="Qwen/Qwen3-0.6B",
            base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
            runtime_contract_version="phase12-adapter-runtime-routing-v1",
            target_fingerprint="b" * 64,
        )
        session.add(restored)
        session.commit()
        assert session.get(RagAnswerRuntimeSnapshot, restored.id) is not None
    with clean_db.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0017_phase12_adapter_runtime_routing"
        )


def test_implicit_base_admission_creates_base_snapshot(
    clean_db, monkeypatch, tmp_path: Path
) -> None:
    with Session(clean_db) as session:
        identities, department, document, extraction, chunk, indexing = _seed_unique(
            session, tmp_path
        )
        department_id = department.id
        actor_subject = identities["student"].subject
        hit = _hit(document, extraction, chunk, indexing)
    monkeypatch.delenv("DEPTSLM_ADAPTER_RUNTIME_URL", raising=False)
    monkeypatch.delenv("DEPTSLM_ADAPTER_RUNTIME_TOKEN", raising=False)
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = _Runtime()
        app.state.rag_qdrant = _Qdrant(hit)
        response = client.post(
            f"/departments/{department_id}/rag/answers",
            headers=_headers(actor_subject),
            json={"question": "What is approved?"},
        )
    assert response.status_code == 200
    with Session(clean_db) as session:
        snapshot = (
            session.query(RagAnswerRuntimeSnapshot).filter_by(department_id=department_id).one()
        )
        assert snapshot.target_kind == "base"
        assert snapshot.deployment_id is None
        assert snapshot.deployment_version == 0
        assert snapshot.deployment_row_version is None
        assert snapshot.adapter_id is None


def test_explicit_base_admission_is_server_owned(clean_db, monkeypatch, tmp_path: Path) -> None:
    with Session(clean_db) as session:
        identities, department, document, extraction, chunk, indexing = _seed_unique(
            session, tmp_path
        )
        department_id = department.id
        actor_subject = identities["viewer"].subject
        hit = _hit(document, extraction, chunk, indexing)
        session.add(
            DepartmentAdapterDeployment(
                department_id=department.id,
                target_kind="base",
                base_model_id="Qwen/Qwen3-0.6B",
                base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
                deployment_version=1,
                version=1,
            )
        )
        session.commit()
    monkeypatch.delenv("DEPTSLM_ADAPTER_RUNTIME_URL", raising=False)
    monkeypatch.delenv("DEPTSLM_ADAPTER_RUNTIME_TOKEN", raising=False)
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = _Runtime()
        app.state.rag_qdrant = _Qdrant(hit)
        response = client.post(
            f"/departments/{department_id}/rag/answers",
            headers=_headers(actor_subject),
            json={"question": "What is approved?"},
        )
    assert response.status_code == 200
    with Session(clean_db) as session:
        snapshot = (
            session.query(RagAnswerRuntimeSnapshot).filter_by(department_id=department_id).one()
        )
        assert snapshot.target_kind == "base"
        assert snapshot.deployment_id is not None
        assert snapshot.deployment_version == 1
        assert snapshot.deployment_row_version == 1


def test_valid_adapter_admission_freezes_exact_authority_and_calls_adapter_runtime(
    clean_db, factory, monkeypatch, tmp_path: Path
) -> None:
    authority, operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with Session(clean_db) as session:
        department, document, extraction, chunk, indexing = _seed_adapter_rag_source(
            session, authority, tmp_path
        )
        department_id = department.id
        hit = _hit(document, extraction, chunk, indexing)
        identity = session.get(UserIdentity, authority.admin_id)
        assert identity is not None
        # The Phase 12.3 authority uses its own issuer; the shared Phase 7
        # HTTP fixture signs tokens with its fixed test issuer.
        identity.issuer = phase7_tests.ISSUER
        session.commit()

    class AdapterRuntime:
        calls = 0

        def generate(self, target, _question, _evidence):
            assert target.target_kind == "adapter"
            self.calls += 1
            return {
                "status": "answered",
                "answer": "Approved by adapter [S1].",
                "citations": ["S1"],
            }

    adapter_runtime = AdapterRuntime()
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = _Runtime()
        app.state.adapter_runtime_client = adapter_runtime
        app.state.rag_qdrant = _Qdrant(hit)
        response = client.post(
            f"/departments/{department_id}/rag/answers",
            headers=_headers(authority.subject),
            json={"question": "What is approved?"},
        )
    assert response.status_code == 200
    assert adapter_runtime.calls == 1
    with Session(clean_db) as session:
        run = session.query(RagAnswerRun).filter_by(department_id=department_id).one()
        snapshot = session.query(RagAnswerRuntimeSnapshot).filter_by(run_id=run.id).one()
        assert run.status == "answered"
        assert snapshot.target_kind == "adapter"
        assert snapshot.adapter_id == operation["target_adapter_id"]
        assert snapshot.adapter_version == operation["target_adapter_version"]
        assert snapshot.review_id == operation["target_review_id"]
        assert snapshot.evaluation_id == operation["target_evaluation_id"]
        assert snapshot.suite_id == operation["suite_id"]
        assert snapshot.registry_attempt_id == operation["registry_attempt_id"]
        assert snapshot.dependency_id == operation["dependency_id"]
        assert (
            session.query(PersistentAuditEvent)
            .filter_by(
                department_id=department_id,
                action="rag.answer.start",
                resource_id=str(run.id),
            )
            .count()
            == 1
        )


def _start_snapshot(factory, authority):
    return _start_run(
        factory,
        SimpleNamespace(minimum_score=Decimal("0.45")),
        phase12_3_tests._principal(authority),
        phase12_3_tests._scope(authority),
        1,
    )


def _rollback_adapter_to_base_and_release_retention(
    factory, authority, root: Path, adapter_id, adapter_version
) -> None:
    """Move the pointer away from an adapter before exercising E-B fences."""

    with factory.begin() as session:
        phase12_3_tests.enqueue_rollback(
            session,
            phase12_3_tests._principal(authority),
            phase12_3_tests._scope(authority),
            target="base",
            adapter_id=None,
            expected_adapter_version=None,
            retention_id=None,
            expected_retention_version=None,
            expected_deployment_version=1,
        )
    assert phase12_3_tests.run_once(factory, data_dir=root, worker_id=uuid4()) is True
    with factory() as session:
        retention = session.scalar(
            select(AdapterRollbackRetention).where(
                AdapterRollbackRetention.department_id == authority.department_id,
                AdapterRollbackRetention.adapter_id == adapter_id,
                AdapterRollbackRetention.status == "active",
            )
        )
        assert retention is not None
        retention_id, retention_version = retention.id, retention.version
    with factory.begin() as session:
        released = phase12_3_tests.release_rollback_retention(
            session,
            phase12_3_tests._principal(authority),
            phase12_3_tests._scope(authority),
            adapter_id=adapter_id,
            retention_id=retention_id,
            expected_adapter_version=adapter_version,
            expected_retention_version=retention_version,
        )
        assert released["status"] == "released"


_RACE_WAIT_SECONDS = 20


@contextmanager
def _bounded_race_database(engine):
    """Apply bounded PostgreSQL lock/query clocks to every race connection."""

    def on_checkout(dbapi_connection, _connection_record, _connection_proxy):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET lock_timeout = '3s'")
            cursor.execute("SET statement_timeout = '15s'")
        finally:
            cursor.close()

    event.listen(engine, "checkout", on_checkout)
    try:
        yield
    finally:
        event.remove(engine, "checkout", on_checkout)


class _DepartmentLockHooks:
    """Test-only SQL hooks for deterministic department-lock serialization."""

    def __init__(self, engine) -> None:
        self._local = threading.local()
        self._engine = engine
        self._before: dict[str, Callable[[], None]] = {}
        self._after: dict[str, Callable[[], None]] = {}
        self._before_seen: set[str] = set()
        self._after_seen: set[str] = set()
        self._guard = threading.Lock()

    def install(self) -> None:
        event.listen(self._engine, "before_cursor_execute", self._before_cursor_execute)
        event.listen(self._engine, "after_cursor_execute", self._after_cursor_execute)

    def uninstall(self) -> None:
        event.remove(self._engine, "before_cursor_execute", self._before_cursor_execute)
        event.remove(self._engine, "after_cursor_execute", self._after_cursor_execute)

    def participant(self, value: str) -> None:
        self._local.participant = value

    def clear_participant(self) -> None:
        self._local.participant = None

    def before(self, participant: str, callback: Callable[[], None]) -> None:
        self._before[participant] = callback

    def after(self, participant: str, callback: Callable[[], None]) -> None:
        self._after[participant] = callback

    @staticmethod
    def _is_department_lock(statement: str) -> bool:
        normalized = " ".join(statement.lower().split())
        return "from departments" in normalized and "for update" in normalized

    def _dispatch(
        self,
        callbacks: dict[str, Callable[[], None]],
        seen: set[str],
    ) -> None:
        participant = getattr(self._local, "participant", None)
        if participant is None or participant not in callbacks:
            return
        with self._guard:
            if participant in seen:
                return
            seen.add(participant)
        callbacks[participant]()

    def _before_cursor_execute(
        self, _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if self._is_department_lock(statement):
            self._dispatch(self._before, self._before_seen)

    def _after_cursor_execute(
        self, _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        if self._is_department_lock(statement):
            self._dispatch(self._after, self._after_seen)


def _wait_for(event_value: threading.Event, label: str) -> None:
    if not event_value.wait(_RACE_WAIT_SECONDS):
        raise AssertionError(f"timed out waiting for {label}")


def _run_race_worker(
    hooks: _DepartmentLockHooks,
    outcomes: dict[str, object],
    errors: dict[str, BaseException],
    label: str,
    function: Callable[[], object],
) -> threading.Thread:
    def run() -> None:
        hooks.participant(label)
        try:
            outcomes[label] = function()
        except BaseException as error:  # noqa: BLE001 - surfaced by the test below
            errors[label] = error
        finally:
            hooks.clear_participant()

    thread = threading.Thread(target=run, name=f"phase12-4-{label}", daemon=True)
    thread.start()
    return thread


def _run_ordered_race(
    engine,
    admission: Callable[[], object],
    governance: Callable[[], object],
    *,
    winner: str,
) -> tuple[dict[str, object], dict[str, BaseException]]:
    """Run two real transactions with one deterministic lock winner.

    The SQLAlchemy after-cursor hook fires while the department row lock is
    held. The losing participant's before-cursor hook then blocks its own
    department-lock query until the winner commits. This exercises the
    department-first order used by admission, governance workers, and E-B
    registration without sleeps or direct deployment-row mutation.
    """

    hooks = _DepartmentLockHooks(engine)
    outcomes: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    admission_locked = threading.Event()
    admission_attempted = threading.Event()
    governance_locked = threading.Event()
    governance_attempted = threading.Event()

    if winner == "admission":
        hooks.after(
            "admission",
            lambda: (admission_locked.set(), _wait_for(governance_attempted, "governance attempt")),
        )
        hooks.before("governance", governance_attempted.set)
    elif winner == "governance":
        hooks.after(
            "governance",
            lambda: (governance_locked.set(), _wait_for(admission_attempted, "admission attempt")),
        )
        hooks.before("admission", admission_attempted.set)
    else:  # pragma: no cover - closed test parameter
        raise AssertionError(winner)

    hooks.install()
    threads: list[threading.Thread] = []
    try:
        if winner == "admission":
            threads.append(_run_race_worker(hooks, outcomes, errors, "admission", admission))
            _wait_for(admission_locked, "admission department lock")
            threads.append(_run_race_worker(hooks, outcomes, errors, "governance", governance))
        else:
            threads.append(_run_race_worker(hooks, outcomes, errors, "governance", governance))
            _wait_for(governance_locked, "governance department lock")
            threads.append(_run_race_worker(hooks, outcomes, errors, "admission", admission))
    finally:
        for thread in threads:
            thread.join(_RACE_WAIT_SECONDS + 5)
        hooks.uninstall()

    assert all(not thread.is_alive() for thread in threads), "race worker remained alive"
    return outcomes, errors


def _assert_successful_race(outcomes: dict[str, object], errors: dict[str, BaseException]) -> None:
    if errors:
        details = "; ".join(
            f"{label}: {type(error).__name__}: {error}" for label, error in errors.items()
        )
        pytest.fail(
            f"PostgreSQL race failed; deadlocks/timeouts are not business outcomes: {details}"
        )
    assert set(outcomes) == {"admission", "governance"}


def _admission_counts(session: Session, department_id) -> dict[str, int]:
    return {
        "runs": session.query(RagAnswerRun).filter_by(department_id=department_id).count(),
        "snapshots": session.query(RagAnswerRuntimeSnapshot)
        .filter_by(department_id=department_id)
        .count(),
        "start_audits": session.query(PersistentAuditEvent)
        .filter_by(department_id=department_id, action="rag.answer.start")
        .count(),
    }


def _assert_adapter_snapshot_matches_pointer(session: Session, snapshot, pointer) -> None:
    assert snapshot.target_kind == "adapter"
    assert snapshot.deployment_id == pointer.id
    assert snapshot.deployment_version == pointer.deployment_version
    assert snapshot.deployment_row_version == pointer.version
    assert snapshot.adapter_id == pointer.adapter_id
    assert snapshot.adapter_version == pointer.adapter_version
    assert snapshot.review_id == pointer.review_id
    assert snapshot.review_version == pointer.review_version
    assert snapshot.evaluation_id == pointer.evaluation_id
    assert snapshot.evaluation_version == pointer.evaluation_version
    assert snapshot.suite_id == pointer.suite_id
    adapter = session.get(Adapter, pointer.adapter_id)
    review = session.get(AdapterReview, pointer.review_id)
    run = session.get(AdapterEvaluationRun, pointer.evaluation_id)
    suite = session.get(EvaluationSuite, pointer.suite_id)
    assert adapter is not None and review is not None and run is not None and suite is not None
    registry = session.get(AdapterRegistryAttempt, run.registry_attempt_id)
    dependency = session.get(AdapterUpstreamDependency, run.dependency_id)
    assert registry is not None and dependency is not None
    assert snapshot.suite_version == suite.version == run.suite_version
    assert snapshot.registry_attempt_id == registry.id == run.registry_attempt_id
    assert snapshot.registry_attempt_version == registry.version == run.registry_attempt_version
    assert snapshot.registry_publication_attempt_id == registry.publication_attempt_id
    assert (
        snapshot.registry_attempt_number == registry.attempt_number == run.registry_attempt_number
    )
    assert (
        snapshot.registry_execution_scope_id
        == registry.execution_scope_id
        == adapter.execution_scope_id
    )
    assert (
        snapshot.registry_manifest_sha256
        == adapter.registry_manifest_sha256
        == run.registry_manifest_sha256
    )
    assert (
        snapshot.adapter_config_sha256
        == adapter.registry_adapter_config_sha256
        == run.registry_adapter_config_sha256
    )
    assert (
        snapshot.adapter_config_byte_size
        == adapter.registry_adapter_config_byte_size
        == run.registry_adapter_config_byte_size
    )
    assert (
        snapshot.adapter_model_sha256
        == adapter.registry_adapter_model_sha256
        == run.registry_adapter_model_sha256
    )
    assert (
        snapshot.adapter_model_byte_size
        == adapter.registry_adapter_model_byte_size
        == run.registry_adapter_model_byte_size
    )
    assert snapshot.dependency_id == dependency.id == run.dependency_id
    assert snapshot.dependency_version == dependency.version == run.dependency_version
    assert snapshot.base_model_id == run.base_model_id
    assert snapshot.base_model_revision == run.base_model_revision


def _assert_base_snapshot(snapshot) -> None:
    assert snapshot.target_kind == "base"
    assert snapshot.adapter_id is None
    assert snapshot.adapter_version is None
    assert snapshot.review_id is None
    assert snapshot.review_version is None
    assert snapshot.evaluation_id is None
    assert snapshot.evaluation_version is None
    assert snapshot.suite_id is None
    assert snapshot.suite_version is None
    assert snapshot.registry_attempt_id is None
    assert snapshot.registry_attempt_version is None
    assert snapshot.registry_publication_attempt_id is None
    assert snapshot.registry_attempt_number is None
    assert snapshot.registry_execution_scope_id is None
    assert snapshot.registry_manifest_sha256 is None
    assert snapshot.adapter_config_sha256 is None
    assert snapshot.adapter_config_byte_size is None
    assert snapshot.adapter_model_sha256 is None
    assert snapshot.adapter_model_byte_size is None


def _assert_one_admission(session: Session, department_id) -> RagAnswerRuntimeSnapshot:
    counts = _admission_counts(session, department_id)
    assert counts == {"runs": 1, "snapshots": 1, "start_audits": 1}
    run = session.query(RagAnswerRun).filter_by(department_id=department_id).one()
    snapshot = session.query(RagAnswerRuntimeSnapshot).filter_by(run_id=run.id).one()
    assert run.status == "running"
    return snapshot


def _assert_deployment_operation_succeeded(session: Session, operation_id, event_type: str) -> None:
    operation = session.get(AdapterDeploymentOperation, operation_id)
    assert operation is not None and operation.status == "succeeded"
    assert (
        session.query(AdapterDeploymentEvent)
        .filter_by(operation_id=operation_id, event_type=event_type)
        .count()
        == 1
    )
    assert (
        session.query(PersistentAuditEvent)
        .filter_by(action="adapter.deployment.success", resource_id=str(operation_id))
        .count()
        == 1
    )


@pytest.mark.parametrize("winner", ["admission", "governance"])
def test_concurrent_admission_vs_first_base_to_a_promotion(
    engine, factory, tmp_path: Path, winner: str
) -> None:
    """Base admission and the real first promotion serialize on department lock."""

    authority, operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)

    def admission():
        return _start_snapshot(factory, authority)

    def promotion():
        return phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4())

    with _bounded_race_database(engine):
        outcomes, errors = _run_ordered_race(engine, admission, promotion, winner=winner)
    _assert_successful_race(outcomes, errors)
    with factory() as session:
        snapshot = _assert_one_admission(session, authority.department_id)
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        assert pointer is not None and pointer.target_kind == "adapter"
        _assert_deployment_operation_succeeded(session, operation["id"], "promote")
        if winner == "admission":
            _assert_base_snapshot(snapshot)
        else:
            _assert_adapter_snapshot_matches_pointer(session, snapshot, pointer)


@pytest.mark.parametrize("winner", ["admission", "governance"])
def test_concurrent_admission_on_a_vs_a_to_b_promotion(
    engine, factory, tmp_path: Path, winner: str
) -> None:
    """A admission and the real A-to-B promotion publish one complete target."""

    authority, operation_a = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory() as session:
        pointer_a = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        assert pointer_a is not None and pointer_a.target_kind == "adapter"
    adapter_b_id, adapter_b_version = phase12_3_tests._clone_adapter_authority(
        factory, authority, operation_a["target_adapter_id"]
    )
    phase12_3_tests._prepare_registry_final(tmp_path, factory, authority, adapter_id=adapter_b_id)
    with factory() as session:
        suite_a = session.get(EvaluationSuite, operation_a["suite_id"])
        assert suite_a is not None
    suite_b = phase12_3_tests._clone_suite(factory, suite_a)
    operation_b = phase12_3_tests._prepare_approved_promotion_for_suite(
        factory,
        tmp_path,
        authority,
        adapter_id=adapter_b_id,
        suite_id=suite_b.id,
        expected_deployment_version=1,
    )
    assert operation_b["target_adapter_version"] == adapter_b_version

    def admission():
        return _start_snapshot(factory, authority)

    def promotion():
        return phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4())

    with _bounded_race_database(engine):
        outcomes, errors = _run_ordered_race(engine, admission, promotion, winner=winner)
    _assert_successful_race(outcomes, errors)
    with factory() as session:
        snapshot = _assert_one_admission(session, authority.department_id)
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        assert pointer is not None and pointer.target_kind == "adapter"
        _assert_adapter_snapshot_matches_pointer(
            session,
            snapshot,
            pointer_a if winner == "admission" else pointer,
        )
        assert pointer.adapter_id in {
            operation_a["target_adapter_id"],
            operation_b["target_adapter_id"],
        }
        _assert_deployment_operation_succeeded(session, operation_b["id"], "promote")


@pytest.mark.parametrize("winner", ["admission", "governance"])
def test_concurrent_admission_on_a_vs_a_to_base_rollback(
    engine, factory, tmp_path: Path, winner: str
) -> None:
    """A admission and the real explicit rollback serialize without mixed NULLs."""

    authority, operation_a = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory() as session:
        pointer_a = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        assert pointer_a is not None and pointer_a.target_kind == "adapter"
    with factory.begin() as session:
        rollback = phase12_3_tests.enqueue_rollback(
            session,
            phase12_3_tests._principal(authority),
            phase12_3_tests._scope(authority),
            target="base",
            adapter_id=None,
            expected_adapter_version=None,
            retention_id=None,
            expected_retention_version=None,
            expected_deployment_version=1,
        )

    def admission():
        return _start_snapshot(factory, authority)

    def rollback_worker():
        return phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4())

    with _bounded_race_database(engine):
        outcomes, errors = _run_ordered_race(engine, admission, rollback_worker, winner=winner)
    _assert_successful_race(outcomes, errors)
    with factory() as session:
        snapshot = _assert_one_admission(session, authority.department_id)
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        assert pointer is not None and pointer.target_kind == "base"
        _assert_deployment_operation_succeeded(session, rollback["id"], "rollback_base")
        if winner == "admission":
            _assert_adapter_snapshot_matches_pointer(session, snapshot, pointer_a)
        else:
            _assert_base_snapshot(snapshot)


@pytest.mark.parametrize("winner", ["admission", "registration"])
def test_concurrent_admission_on_a_vs_eb_registration(
    engine, factory, tmp_path: Path, winner: str
) -> None:
    """Admission and E-B registration serialize before either can cross authority."""

    authority, operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True

    def registration():
        return _register_or_resume(
            factory,
            department_id=authority.department_id,
            adapter_id=operation["target_adapter_id"],
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
            limit=1,
            item_limit=2,
            apply=True,
        )

    def admission():
        return _start_snapshot(factory, authority)

    first = "admission" if winner == "admission" else "governance"
    with _bounded_race_database(engine):
        outcomes, errors = _run_ordered_race(engine, admission, registration, winner=first)
    with factory() as session:
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        assert pointer is not None
        assert pointer.target_kind == "adapter"
        counts = _admission_counts(session, authority.department_id)
        purge_count = (
            session.query(AdapterPurgeOperation)
            .filter_by(
                department_id=authority.department_id,
                adapter_id=operation["target_adapter_id"],
            )
            .count()
        )
        if winner == "admission":
            assert set(outcomes) == {"admission"}
            assert set(errors) == {"governance"}
            assert isinstance(errors.get("governance"), ServiceError)
            assert str(errors["governance"]) == (
                "Adapter purge conflicts with active RAG runtime snapshot"
            )
            assert counts == {"runs": 1, "snapshots": 1, "start_audits": 1}
            snapshot = _assert_one_admission(session, authority.department_id)
            _assert_adapter_snapshot_matches_pointer(session, snapshot, pointer)
            assert purge_count == 0
        else:
            assert set(outcomes) == {"governance"}
            assert set(errors) == {"admission"}
            assert isinstance(errors.get("admission"), ServiceError)
            assert counts == {"runs": 0, "snapshots": 0, "start_audits": 0}
            assert purge_count == 1
            registered_operation_id, registered_item_ids = outcomes["governance"]
            assert registered_operation_id is not None and len(registered_item_ids) == 2
            purge = (
                session.query(AdapterPurgeOperation)
                .filter_by(
                    department_id=authority.department_id,
                    adapter_id=operation["target_adapter_id"],
                )
                .one()
            )
            assert purge.status == "registered"
            adapter = session.get(Adapter, operation["target_adapter_id"])
            assert adapter is not None and adapter.status == "purge_pending"
            assert pointer.target_kind == "adapter"


def test_inflight_base_snapshot_stays_base_after_adapter_promotion(factory, tmp_path: Path) -> None:
    authority, operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    started_base = _start_snapshot(factory, authority)
    assert started_base.runtime_target.target_kind == "base"
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True

    class Base:
        def generate(self, question, evidence, **_kwargs):
            return ("base", question, evidence)

    class Adapter:
        def generate(self, target, question, evidence):
            return ("adapter", target.adapter_id, question, evidence)

    routed = RoutedRagRuntime(Base(), Adapter(), started_base.runtime_target)
    assert routed.generate("q", ()) == ("base", "q", ())
    started_adapter = _start_snapshot(factory, authority)
    assert started_adapter.runtime_target.target_kind == "adapter"
    assert started_adapter.runtime_target.adapter_id == operation["target_adapter_id"]
    assert RoutedRagRuntime(Base(), Adapter(), started_adapter.runtime_target).generate(
        "q", ()
    ) == ("adapter", operation["target_adapter_id"], "q", ())


def test_inflight_adapter_snapshot_stays_a_after_b_promotion(factory, tmp_path: Path) -> None:
    authority, operation_a = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    started_a = _start_snapshot(factory, authority)
    adapter_b_id, adapter_b_version = phase12_3_tests._clone_adapter_authority(
        factory, authority, operation_a["target_adapter_id"]
    )
    phase12_3_tests._prepare_registry_final(tmp_path, factory, authority, adapter_id=adapter_b_id)
    with factory() as session:
        suite_a = session.get(EvaluationSuite, operation_a["suite_id"])
        assert suite_a is not None
    suite_b = phase12_3_tests._clone_suite(factory, suite_a)
    operation_b = phase12_3_tests._prepare_approved_promotion_for_suite(
        factory,
        tmp_path,
        authority,
        adapter_id=adapter_b_id,
        suite_id=suite_b.id,
        expected_deployment_version=1,
    )
    assert operation_b["target_adapter_version"] == adapter_b_version
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    assert started_a.runtime_target.adapter_id == operation_a["target_adapter_id"]

    class Base:
        def generate(self, question, evidence, **_kwargs):
            return ("base", question, evidence)

    class Adapter:
        def generate(self, target, question, evidence):
            return ("adapter", target.adapter_id, question, evidence)

    assert RoutedRagRuntime(Base(), Adapter(), started_a.runtime_target).generate("q", ()) == (
        "adapter",
        operation_a["target_adapter_id"],
        "q",
        (),
    )
    started_b = _start_snapshot(factory, authority)
    assert started_b.runtime_target.adapter_id == adapter_b_id
    assert started_b.runtime_target.adapter_version == operation_b["target_adapter_version"]


def test_inflight_adapter_snapshot_stays_a_after_base_rollback(factory, tmp_path: Path) -> None:
    authority, operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    started_a = _start_snapshot(factory, authority)
    with factory.begin() as session:
        phase12_3_tests.enqueue_rollback(
            session,
            phase12_3_tests._principal(authority),
            phase12_3_tests._scope(authority),
            target="base",
            adapter_id=None,
            expected_adapter_version=None,
            retention_id=None,
            expected_retention_version=None,
            expected_deployment_version=1,
        )
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    assert started_a.runtime_target.adapter_id == operation["target_adapter_id"]
    started_base = _start_snapshot(factory, authority)
    assert started_base.runtime_target.target_kind == "base"


@pytest.mark.parametrize("terminal_status", ["answered", "insufficient_information", "failed"])
def test_terminal_runtime_requests_do_not_fence_eb_registration(
    factory, tmp_path: Path, terminal_status: str
) -> None:
    authority, _operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    started = _start_snapshot(factory, authority)
    _rollback_adapter_to_base_and_release_retention(
        factory,
        authority,
        tmp_path,
        started.runtime_target.adapter_id,
        started.runtime_target.adapter_version,
    )
    with factory.begin() as session:
        run = session.get(RagAnswerRun, started.id)
        assert run is not None
        now = datetime.now(UTC)
        run.status = terminal_status
        run.finished_at = now
        run.version += 1
        if terminal_status == "answered":
            run.retrieval_candidate_count = 1
            run.retrieval_authorized_count = 1
            run.selected_source_count = 1
        elif terminal_status == "insufficient_information":
            run.retrieval_candidate_count = 0
            run.retrieval_authorized_count = 0
            run.selected_source_count = 0
        else:
            run.error_code = "generation_failed"
    operation_id, item_ids = _register_or_resume(
        factory,
        department_id=authority.department_id,
        adapter_id=started.runtime_target.adapter_id,
        actor_issuer=authority.issuer,
        actor_subject=authority.subject,
        limit=1,
        item_limit=2,
        apply=True,
    )
    assert operation_id is not None and len(item_ids) == 2


def test_running_runtime_snapshot_blocks_eb_registration_and_finalization(
    factory, tmp_path: Path
) -> None:
    authority, operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    started = _start_snapshot(factory, authority)
    adapter_id = operation["target_adapter_id"]
    _rollback_adapter_to_base_and_release_retention(
        factory,
        authority,
        tmp_path,
        adapter_id,
        operation["target_adapter_version"],
    )

    # Registration is the real E-B path and is fenced by the running snapshot.
    with pytest.raises(ServiceError, match="active RAG runtime snapshot"):
        _register_or_resume(
            factory,
            department_id=authority.department_id,
            adapter_id=adapter_id,
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
            limit=1,
            item_limit=2,
            apply=True,
        )

    # Create the durable E-B operation only after temporarily terminalizing the
    # test request; then restore its exact snapshot to running so finalization
    # exercises the independent E-B finalization fence.
    with factory.begin() as session:
        run = session.get(RagAnswerRun, started.id)
        assert run is not None
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.error_code = "generation_failed"
        run.version += 1
    operation_id, item_ids = _register_or_resume(
        factory,
        department_id=authority.department_id,
        adapter_id=adapter_id,
        actor_issuer=authority.issuer,
        actor_subject=authority.subject,
        limit=1,
        item_limit=2,
        apply=True,
    )
    assert operation_id is not None and len(item_ids) == 2
    _mark_operation_deleting(factory, operation_id, authority.department_id)
    with factory.begin() as session:
        run = session.get(RagAnswerRun, started.id)
        assert run is not None
        run.status = "running"
        run.finished_at = None
        run.error_code = None
        run.retrieval_candidate_count = None
        run.retrieval_authorized_count = None
        run.selected_source_count = None
        run.version += 1
        for item in session.query(AdapterPurgeItem).filter_by(
            operation_id=operation_id, department_id=authority.department_id
        ):
            item.status = "completed"
            item.completed_at = datetime.now(UTC)
            item.version += 1
        for reservation in session.query(AdapterPurgeReservation).filter_by(
            operation_id=operation_id, department_id=authority.department_id
        ):
            reservation.status = "completed"
            reservation.completed_at = datetime.now(UTC)
            reservation.version += 1
    with pytest.raises(ServiceError, match="active RAG runtime snapshot"):
        _finalize_operation(
            factory,
            data_dir=tmp_path,
            operation_id=operation_id,
            department_id=authority.department_id,
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
        )
    with factory() as session:
        persisted = session.get(AdapterPurgeOperation, operation_id)
        assert persisted is not None and persisted.status == "deleting"
        run = session.get(RagAnswerRun, started.id)
        assert run is not None and run.status == "running"

    # Once the request is terminal, the same completed operation can finalize.
    with factory.begin() as session:
        run = session.get(RagAnswerRun, started.id)
        assert run is not None
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.error_code = "generation_failed"
        run.version += 1
    _finalize_operation(
        factory,
        data_dir=tmp_path,
        operation_id=operation_id,
        department_id=authority.department_id,
        actor_issuer=authority.issuer,
        actor_subject=authority.subject,
    )
    with factory() as session:
        persisted = session.get(AdapterPurgeOperation, operation_id)
        assert persisted is not None and persisted.status == "completed"
        assert (
            session.query(PersistentAuditEvent)
            .filter_by(
                department_id=authority.department_id,
                action="adapter.purge",
                resource_id=str(operation_id),
            )
            .count()
            == 1
        )


def _invalidate_adapter_authority(
    session: Session, operation: dict[str, object], kind: str
) -> None:
    adapter = session.get(Adapter, operation["target_adapter_id"])
    review = session.get(AdapterReview, operation["target_review_id"])
    evaluation = session.get(AdapterEvaluationRun, operation["target_evaluation_id"])
    suite = session.get(EvaluationSuite, operation["suite_id"])
    registry = session.get(AdapterRegistryAttempt, operation["registry_attempt_id"])
    dependency = session.get(AdapterUpstreamDependency, operation["dependency_id"])
    evidence = (
        session.query(AdapterEvaluationEvidence)
        .filter_by(run_id=operation["target_evaluation_id"], target="candidate")
        .one()
    )
    assert all(
        value is not None for value in (adapter, review, evaluation, suite, registry, dependency)
    )
    deployment = (
        session.query(DepartmentAdapterDeployment)
        .filter_by(department_id=adapter.department_id)
        .one()
    )
    now = datetime.now(UTC)
    if kind == "adapter_version":
        deployment.adapter_version += 1
    elif kind == "adapter_purge_pending":
        adapter.status = "purge_pending"
    elif kind == "review_rejected":
        review.status = "rejected"
    elif kind == "review_version":
        deployment.review_version += 1
    elif kind == "evaluation_failed":
        evaluation.status = "failed"
        evaluation.gate_status = "pending"
        evaluation.error_code = "candidate_runtime_unavailable"
        evaluation.finished_at = now
    elif kind == "candidate_gate":
        evaluation.gate_status = "failed"
        evidence.gate_status = "failed"
        evidence.failed_gate_count = 1
    elif kind == "suite_archived":
        suite.status = "archived"
        suite.archived_at = now
    elif kind == "registry_failed":
        registry.status = "failed"
        registry.error_code = "adapter_registry_manifest_invalid"
        registry.finished_at = now
    elif kind == "registry_version":
        registry.version += 1
    elif kind == "publication_attempt":
        adapter.publication_attempt_id = uuid4()
    elif kind == "execution_scope":
        registry.execution_scope_id = uuid4()
    elif kind == "manifest_digest":
        adapter.registry_manifest_sha256 = "0" * 64
    elif kind == "config_digest":
        adapter.registry_adapter_config_sha256 = "0" * 64
    elif kind == "config_size":
        adapter.registry_adapter_config_byte_size += 1
    elif kind == "model_digest":
        adapter.registry_adapter_model_sha256 = "0" * 64
        adapter.source_adapter_model_sha256 = "0" * 64
    elif kind == "model_size":
        adapter.registry_adapter_model_byte_size += 1
        adapter.source_adapter_model_byte_size += 1
    elif kind == "dependency_released":
        dependency.status = "released"
        dependency.released_at = now
    elif kind == "dependency_version":
        dependency.version += 1
    elif kind == "base_model":
        deployment.base_model_id = "unreviewed/base"
    elif kind == "base_revision":
        deployment.base_model_revision = "unreviewed-revision"
    elif kind == "active_purge":
        # The active E-B operation is registered outside this mutation
        # transaction so the real purge registration path is exercised.
        return
    else:  # pragma: no cover - the parameter list below is closed
        raise AssertionError(kind)


@pytest.mark.parametrize(
    "mutation",
    [
        "adapter_version",
        "adapter_purge_pending",
        "review_rejected",
        "review_version",
        "evaluation_failed",
        "candidate_gate",
        "suite_archived",
        "registry_failed",
        "registry_version",
        "publication_attempt",
        "execution_scope",
        "manifest_digest",
        "config_digest",
        "config_size",
        "model_digest",
        "model_size",
        "dependency_released",
        "dependency_version",
        "base_model",
        "base_revision",
        "active_purge",
    ],
)
def test_invalid_adapter_authority_creates_no_run_snapshot_audit_or_runtime_call(
    clean_db, factory, monkeypatch, tmp_path: Path, mutation: str
) -> None:
    authority, operation = phase12_3_tests._prepare_approved_promotion(factory, tmp_path)
    assert phase12_3_tests.run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with Session(clean_db) as session:
        identity = session.get(UserIdentity, authority.admin_id)
        assert identity is not None
        identity.issuer = phase7_tests.ISSUER
        _invalidate_adapter_authority(session, operation, mutation)
        session.commit()
    if mutation == "active_purge":
        # Preserve the valid deployed target while inserting only the
        # content-free active E-B authority row.  The dedicated fence test
        # above exercises the real registration path; this matrix mutation
        # isolates the admission-time active-operation lookup.
        with factory.begin() as session:
            adapter = session.get(Adapter, operation["target_adapter_id"])
            assert adapter is not None
            source = session.get(AdapterImportSource, adapter.source_bundle_id)
            assert adapter is not None and source is not None
            source_attempt = session.get(
                AdapterImportAttempt, adapter.source_authoritative_attempt_id
            )
            registry_attempt = session.get(AdapterRegistryAttempt, operation["registry_attempt_id"])
            assert source_attempt is not None and registry_attempt is not None
            session.add(
                AdapterPurgeOperation(
                    id=uuid4(),
                    department_id=authority.department_id,
                    adapter_id=adapter.id,
                    source_bundle_id=source.id,
                    requested_by_user_id=authority.admin_id,
                    limit_value=1,
                    item_limit_value=2,
                    status="registered",
                    expected_adapter_version=adapter.version,
                    expected_source_version=source.version,
                    expected_source_attempt_version=source_attempt.version,
                    expected_registry_attempt_version=registry_attempt.version,
                    source_authoritative_attempt_id=source_attempt.id,
                    source_publication_attempt_id=source_attempt.publication_attempt_id,
                    source_attempt_number=source_attempt.attempt_number,
                    registry_attempt_id=registry_attempt.id,
                    registry_publication_attempt_id=registry_attempt.publication_attempt_id,
                    registry_attempt_number=registry_attempt.attempt_number,
                    authority_snapshot={},
                    eligible_item_count=0,
                )
            )
    with clean_db.connect() as connection:
        before = {
            "runs": connection.execute(
                text("SELECT count(*) FROM rag_answer_runs WHERE department_id = :department_id"),
                {"department_id": str(authority.department_id)},
            ).scalar_one(),
            "snapshots": connection.execute(
                text(
                    "SELECT count(*) FROM rag_answer_runtime_snapshots "
                    "WHERE department_id = :department_id"
                ),
                {"department_id": str(authority.department_id)},
            ).scalar_one(),
            "audits": connection.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE department_id = :department_id AND action = 'rag.answer.start'"
                ),
                {"department_id": str(authority.department_id)},
            ).scalar_one(),
        }
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = _Runtime()
        app.state.adapter_runtime_client = _Runtime()
        app.state.rag_qdrant = _Qdrant()
        response = client.post(
            f"/departments/{authority.department_id}/rag/answers",
            headers=_headers(authority.subject),
            json={"question": "Must be rejected"},
        )
    assert response.status_code == 503
    with clean_db.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM rag_answer_runs WHERE department_id = :department_id"),
                {"department_id": str(authority.department_id)},
            ).scalar_one()
            == before["runs"]
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM rag_answer_runtime_snapshots "
                    "WHERE department_id = :department_id"
                ),
                {"department_id": str(authority.department_id)},
            ).scalar_one()
            == before["snapshots"]
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE department_id = :department_id AND action = 'rag.answer.start'"
                ),
                {"department_id": str(authority.department_id)},
            ).scalar_one()
            == before["audits"]
        )


class _Runtime:
    def query_embedding(self, _question):
        vector = [0.0] * 1024
        vector[0] = 1.0
        return vector

    def generate(self, _question, _evidence):
        return {"status": "answered", "answer": "Approved for testing [S1].", "citations": ["S1"]}


class _Qdrant:
    def __init__(self, hit=None):
        self.hit = hit

    def verify_collection(self):
        return None

    def search_published(self, _scope, _query, *, limit):
        assert limit == 20
        return (self.hit,)
