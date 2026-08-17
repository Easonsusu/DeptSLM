"""PostgreSQL admission and migration coverage for Phase 12.4 routing."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import test_phase7_postgres as phase7_tests
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from test_phase7_postgres import _client, _headers, _hit, _seed

from alembic import command
from app.database import create_database_engine
from app.main import app
from app.models import (
    Department,
    DepartmentAdapterDeployment,
    RagAnswerRun,
    RagAnswerRuntimeSnapshot,
    UserIdentity,
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


def _seed_unique(session, tmp_path: Path):
    original = phase7_tests._identity

    def identity(db, department, role, subject):
        return original(db, department, role, f"{subject}-{uuid4().hex}")

    phase7_tests._identity = identity
    try:
        return _seed(session, tmp_path)
    finally:
        phase7_tests._identity = original


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


class _Runtime:
    def query_embedding(self, _question):
        vector = [0.0] * 1024
        vector[0] = 1.0
        return vector

    def generate(self, _question, _evidence):
        return {"status": "answered", "answer": "Approved for testing [S1].", "citations": ["S1"]}


class _Qdrant:
    def __init__(self, hit):
        self.hit = hit

    def verify_collection(self):
        return None

    def search_published(self, _scope, _query, *, limit):
        assert limit == 20
        return (self.hit,)
