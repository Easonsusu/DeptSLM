"""Required PostgreSQL 16 functional and concurrency coverage for Phase 12.2."""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from test_phase12_1c_integration import Authority, _enqueue, _principal, _scope, _seed_authority

from alembic import command
from app.adapter_evaluation_queue import (
    AdapterEvaluationQueueError,
    claim_next,
    fail_owned,
    finalize_success,
    renew_lease,
)
from app.adapter_evaluation_services import cancel_adapter_evaluation, enqueue_adapter_evaluation
from app.adapter_purge import purge_adapter_artifacts
from app.adapter_registry_domain import canonical_json_bytes
from app.database import create_database_engine
from app.evaluation_domain import AggregateMetrics, GateEvaluation
from app.evaluation_suites import GroundTruthAuthoritySnapshot
from app.models import (
    Adapter,
    AdapterEvaluationAttempt,
    AdapterEvaluationRun,
    AdapterImportSource,
    AdapterPurgeOperation,
    AdapterRegistryAttempt,
    EvaluationSuite,
    PersistentAuditEvent,
)
from app.services import ServiceError

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
def factory(engine):
    return sessionmaker(engine)


def _make_authority(factory: sessionmaker[Session]) -> tuple[Authority, Adapter, EvaluationSuite]:
    with factory() as session:
        authority = _seed_authority(session)
    registry = _enqueue(factory, authority, apply=True)
    assert registry.adapter_id is not None
    adapter_id = registry.adapter_id
    suite_id = uuid4()
    now = datetime.now(UTC)
    with factory.begin() as session:
        adapter = session.get(Adapter, adapter_id)
        source = session.get(AdapterImportSource, authority.source_id)
        attempt = session.scalar(
            select(AdapterRegistryAttempt).where(AdapterRegistryAttempt.adapter_id == adapter_id)
        )
        assert adapter is not None and source is not None and attempt is not None
        manifest = {
            "department_id": str(authority.department_id),
            "adapter_id": str(adapter.id),
            "publication_attempt_id": str(attempt.publication_attempt_id),
            "attempt_number": attempt.attempt_number,
            "files": {
                "adapter_config.json": {"sha256": "a" * 64, "byte_size": 2},
                "adapter_model.safetensors": {"sha256": "b" * 64, "byte_size": 5},
            },
        }
        manifest_sha = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
        attempt.status = "succeeded"
        attempt.ownership_manifest = manifest
        attempt.staged_at = now
        attempt.published_at = now
        attempt.finished_at = now
        attempt.version = 2
        adapter.status = "validated"
        adapter.version += 1
        adapter.finished_at = now
        adapter.validated_at = now
        adapter.registry_manifest_sha256 = manifest_sha
        adapter.registry_adapter_config_sha256 = "a" * 64
        adapter.registry_adapter_config_byte_size = 2
        adapter.registry_adapter_model_sha256 = "b" * 64
        adapter.registry_adapter_model_byte_size = 5
        adapter.registry_attempt_version = attempt.version
        adapter.verified_governance_lineage = True
        adapter.verified_artifact_compatibility = True
        source.status = "consumed"
        source.consumed_at = now
        source.version += 1
        suite = EvaluationSuite(
            id=suite_id,
            department_id=authority.department_id,
            imported_by_user_id=authority.admin_id,
            status="active",
            suite_contract_version="phase9-evaluation-suite-v1",
            artifact_contract_version="phase9-evaluation-artifact-v1",
            metric_contract_version="phase9-deterministic-metrics-v1",
            answer_normalization_version="phase9-answer-normalization-v1",
            gate_policy_version="phase9-quality-gates-v1",
            case_count=1,
            answered_case_count=1,
            insufficient_case_count=0,
            artifact_manifest_sha256="c" * 64,
            canonical_cases_sha256="d" * 64,
            canonical_cases_byte_size=1,
            retrieval_recall_at_5_min=Decimal("0.5"),
            retrieval_mrr_at_20_min=Decimal("0.5"),
            answer_status_accuracy_min=Decimal("0.5"),
            citation_precision_min=Decimal("0.5"),
            citation_recall_min=Decimal("0.5"),
            normalized_exact_match_min=Decimal("0.5"),
            character_f1_min=Decimal("0.5"),
            invalid_contract_rate_max=Decimal("0.5"),
            version=1,
        )
        session.add(suite)
    with factory() as session:
        persisted_adapter = session.get(Adapter, adapter_id)
        persisted_suite = session.get(EvaluationSuite, suite_id)
        assert persisted_adapter is not None and persisted_suite is not None
        return authority, persisted_adapter, persisted_suite


def _enqueue_eval(factory, authority: Authority, adapter: Adapter, suite: EvaluationSuite):
    with factory.begin() as session:
        return enqueue_adapter_evaluation(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            suite_id=suite.id,
            expected_adapter_version=adapter.version,
            code_revision=authority.code_revision,
        )


def test_phase12_2_enqueue_claim_renew_reclaim_and_stale_fence(factory) -> None:
    authority, adapter, suite = _make_authority(factory)
    queued = _enqueue_eval(factory, authority, adapter, suite)
    first = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert first is not None
    renew_lease(factory, first, 30)
    with factory.begin() as session:
        session.execute(
            update(AdapterEvaluationRun)
            .where(AdapterEvaluationRun.id == first.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    replacement = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert replacement is not None
    assert replacement.claim_token != first.claim_token
    assert replacement.publication_attempt_id != first.publication_attempt_id
    assert replacement.attempt_number == first.attempt_number + 1
    assert fail_owned(factory, first, "claim_lost") is False
    with factory() as session:
        previous = session.scalar(
            select(AdapterEvaluationAttempt).where(
                AdapterEvaluationAttempt.run_id == first.id,
                AdapterEvaluationAttempt.attempt_number == first.attempt_number,
            )
        )
        assert previous is not None and previous.status == "reclaimed"
        run = session.get(AdapterEvaluationRun, first.id)
        assert run is not None and run.status == "running"
        assert run.result_publication_attempt_id == replacement.publication_attempt_id
    assert queued["status"] == "queued"


def test_phase12_2_cancel_is_server_version_fenced(factory) -> None:
    authority, adapter, suite = _make_authority(factory)
    queued = _enqueue_eval(factory, authority, adapter, suite)
    with factory.begin() as session:
        cancelled = cancel_adapter_evaluation(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            evaluation_id=queued["id"],
            expected_version=queued["version"],
        )
    assert cancelled["status"] == "cancelled"
    assert claim_next(factory, uuid4(), 30, authority.code_revision) is None
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.evaluation.cancel",
                )
            )
            >= 1
        )


def test_phase12_2_final_authority_mutation_denies_success_without_audit(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)
    _enqueue_eval(factory, authority, adapter, suite)
    claim = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    with factory.begin() as session:
        row = session.get(Adapter, adapter.id)
        assert row is not None
        row.version += 1
    metrics = AggregateMetrics(*(Decimal("0.5") for _ in range(10)))
    gate = GateEvaluation(True, 0, {})
    with pytest.raises(AdapterEvaluationQueueError, match="adapter_authority_changed"):
        finalize_success(
            factory,
            claim,
            baseline_metrics=metrics,
            candidate_metrics=metrics,
            baseline_gate=gate,
            candidate_gate=gate,
            result_manifest_sha256="a" * 64,
            result_summary_sha256="b" * 64,
            case_results_sha256="c" * 64,
            case_results_byte_size=1,
            data_dir=tmp_path,
            suite_cases=(),
            suite_authority=GroundTruthAuthoritySnapshot({}, ()),
            result_store=object(),
            result_manifest={},
            result_files={},
        )
    with factory() as session:
        run = session.get(AdapterEvaluationRun, claim.id)
        assert run is not None and run.status == "running"
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.department_id == authority.department_id,
                    PersistentAuditEvent.action == "adapter.evaluation.complete",
                )
            )
            == 0
        )


def test_phase12_2_active_evaluation_fences_real_purge_registration(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)
    _enqueue_eval(factory, authority, adapter, suite)
    with pytest.raises(ServiceError, match="active evaluation"):
        purge_adapter_artifacts(
            factory,
            data_dir=Path(tmp_path),
            department_id=authority.department_id,
            adapter_id=adapter.id,
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
            apply=True,
        )
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterPurgeOperation.id)).where(
                    AdapterPurgeOperation.department_id == authority.department_id,
                    AdapterPurgeOperation.adapter_id == adapter.id,
                )
            )
            == 0
        )


def test_phase12_2_concurrent_enqueue_serializes_without_deadlock(factory) -> None:
    authority, adapter, suite = _make_authority(factory)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with factory.begin() as session:
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                session.execute(text("SET LOCAL statement_timeout = '5s'"))
                barrier.wait(timeout=5)
                outcomes.append(
                    enqueue_adapter_evaluation(
                        session,
                        _principal(authority),
                        _scope(authority),
                        adapter_id=adapter.id,
                        suite_id=suite.id,
                        expected_adapter_version=adapter.version,
                        code_revision=authority.code_revision,
                    )
                )
        except ServiceError as error:
            outcomes.append(error)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "PostgreSQL lock race deadlocked"
    assert not errors, errors
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    assert sum(isinstance(value, ServiceError) for value in outcomes) == 1
