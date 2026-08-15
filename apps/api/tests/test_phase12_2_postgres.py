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
from app.adapter_evaluation_artifacts import AdapterEvaluationArtifactStore
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
from app.authorization import DepartmentScope
from app.database import create_database_engine
from app.evaluation_domain import AggregateMetrics, GateEvaluation
from app.evaluation_suites import GroundTruthAuthoritySnapshot
from app.models import (
    Adapter,
    AdapterEvaluationAttempt,
    AdapterEvaluationEvidence,
    AdapterEvaluationRun,
    AdapterImportSource,
    AdapterPurgeOperation,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    EvaluationSuite,
    Membership,
    PersistentAuditEvent,
    UserIdentity,
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


def _run_concurrently(*calls):
    """Run real database operations with bounded lock/deadlock diagnostics."""

    barrier = threading.Barrier(len(calls))
    outcomes: list[object] = []
    errors: list[BaseException] = []

    def invoke(call):
        try:
            barrier.wait(timeout=10)
            outcomes.append(call())
        except ServiceError as error:
            outcomes.append(error)
        except BaseException as error:  # pragma: no cover - assertion below reports it
            errors.append(error)

    threads = [threading.Thread(target=invoke, args=(call,), daemon=False) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "PostgreSQL lock race deadlocked"
    assert not errors, errors
    return outcomes


def _evaluation_artifacts(factory, claim, root: Path):
    """Create a complete content-free result publication for finalization tests."""

    with factory() as session:
        run = session.get(AdapterEvaluationRun, claim.id)
        assert run is not None
        manifest = {
            "artifact_contract_version": run.artifact_contract_version,
            "department_id": str(run.department_id),
            "evaluation_id": str(run.id),
            "adapter_id": str(run.adapter_id),
            "adapter_version": run.adapter_version,
            "suite_id": str(run.suite_id),
            "publication_attempt_id": str(claim.publication_attempt_id),
            "attempt_number": claim.attempt_number,
            "base_seed": run.base_seed,
            "baseline_lane_id": str(uuid4()),
            "candidate_lane_id": str(uuid4()),
            "base_model_id": run.base_model_id,
            "base_model_revision": run.base_model_revision,
            "registry_publication_attempt_id": str(run.registry_publication_attempt_id),
            "registry_attempt_number": run.registry_attempt_number,
            "registry_manifest_sha256": run.registry_manifest_sha256,
            "adapter_config_sha256": run.registry_adapter_config_sha256,
            "adapter_config_byte_size": run.registry_adapter_config_byte_size,
            "adapter_model_sha256": run.registry_adapter_model_sha256,
            "adapter_model_byte_size": run.registry_adapter_model_byte_size,
            "runner_contract_version": run.runner_contract_version,
            "metric_contract_version": run.metric_contract_version,
            "gate_policy_version": run.gate_policy_version,
            "seed_policy_version": run.seed_policy_version,
            "code_revision": run.code_revision,
        }
    metrics = {name: Decimal("0.5000") for name in AggregateMetrics.__dataclass_fields__}
    summary = {
        "baseline_metrics": metrics,
        "candidate_metrics": metrics,
        "metric_deltas": {name: Decimal("0") for name in metrics},
        "baseline_gate_status": "passed",
        "candidate_gate_status": "passed",
        "baseline_failed_gate_count": 0,
        "candidate_failed_gate_count": 0,
    }
    rows = []
    for target in ("baseline", "candidate"):
        rows.append(
            {
                "target": target,
                "case_id": uuid4(),
                "expected_status": "answered",
                "actual_status": "answered",
                "relevant_chunk_count": 1,
                "retrieval_candidate_count": 1,
                "retrieved_relevant_at_5": 1,
                "retrieved_relevant_at_10": 1,
                "retrieved_relevant_at_20": 1,
                "reciprocal_rank_at_20": Decimal("1"),
                "status_correct": True,
                "cited_count": 1,
                "cited_relevant_count": 1,
                "citation_precision": Decimal("1"),
                "citation_recall": Decimal("1"),
                "normalized_exact_match": Decimal("1"),
                "character_f1": Decimal("1"),
                "answer_contract_valid": True,
                "case_gate_passed": True,
                "error_code": None,
            }
        )
    (root / "eval_results").mkdir(parents=True, exist_ok=True)
    store = AdapterEvaluationArtifactStore(root)
    staged = store.stage_run(
        _scope_value(claim.department_id),
        claim.id,
        claim.publication_attempt_id,
        manifest=manifest,
        summary=summary,
        case_rows=rows,
    )
    published = store.publish(staged)
    files = dict(published.files)
    return store, manifest, rows, files


def _scope_value(department_id):
    return DepartmentScope(department_id)


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


def test_phase12_2_real_enqueue_vs_purge_race_has_one_authority_winner(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)

    def enqueue():
        with factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
            return enqueue_adapter_evaluation(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=adapter.id,
                suite_id=suite.id,
                expected_adapter_version=adapter.version,
                code_revision=authority.code_revision,
            )

    def purge():
        return purge_adapter_artifacts(
            factory,
            data_dir=Path(tmp_path),
            department_id=authority.department_id,
            adapter_id=adapter.id,
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
            apply=True,
        )

    outcomes = _run_concurrently(enqueue, purge)
    assert sum(not isinstance(value, ServiceError) for value in outcomes) == 1
    assert sum(isinstance(value, ServiceError) for value in outcomes) == 1
    with factory() as session:
        active_eval = session.scalar(
            select(AdapterEvaluationRun.id).where(
                AdapterEvaluationRun.department_id == authority.department_id,
                AdapterEvaluationRun.adapter_id == adapter.id,
                AdapterEvaluationRun.status.in_(("queued", "running")),
            )
        )
        active_purge = session.scalar(
            select(AdapterPurgeOperation.id).where(
                AdapterPurgeOperation.department_id == authority.department_id,
                AdapterPurgeOperation.adapter_id == adapter.id,
                AdapterPurgeOperation.status.in_(("registered", "deleting")),
            )
        )
        assert not (active_eval is not None and active_purge is not None)


def test_phase12_2_active_eb_registration_blocks_evaluation_enqueue(
    factory, tmp_path, monkeypatch
) -> None:
    authority, adapter, suite = _make_authority(factory)
    started = threading.Event()
    release = threading.Event()
    import app.adapter_purge as purge_module

    real_execute = purge_module._execute_item

    def delayed_execute(*args, **kwargs):
        started.set()
        assert release.wait(15), "purge test worker did not release"
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(purge_module, "_execute_item", delayed_execute)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            purge_adapter_artifacts(
                factory,
                data_dir=Path(tmp_path),
                department_id=authority.department_id,
                adapter_id=adapter.id,
                actor_issuer=authority.issuer,
                actor_subject=authority.subject,
                apply=True,
            )
        except BaseException as error:  # pragma: no cover - assertion below reports it
            errors.append(error)

    thread = threading.Thread(target=worker, daemon=False)
    thread.start()
    assert started.wait(15), "purge registration did not reach its real item path"
    with factory.begin() as session:
        with pytest.raises(ServiceError, match="purge"):
            enqueue_adapter_evaluation(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=adapter.id,
                suite_id=suite.id,
                expected_adapter_version=adapter.version,
                code_revision=authority.code_revision,
            )
    release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert not errors, errors


def test_phase12_2_finalization_publishes_exact_decimal_evidence_and_replays_safely(
    factory, tmp_path
) -> None:
    authority, adapter, suite = _make_authority(factory)
    _enqueue_eval(factory, authority, adapter, suite)
    claim = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    store, manifest, rows, files = _evaluation_artifacts(factory, claim, Path(tmp_path))
    metrics = AggregateMetrics(*(Decimal("0.5000") for _ in range(10)))
    gate = GateEvaluation(True, 0, {})
    finalize_success(
        factory,
        claim,
        baseline_metrics=metrics,
        candidate_metrics=metrics,
        baseline_gate=gate,
        candidate_gate=gate,
        result_manifest_sha256=files["manifest.json"].sha256,
        result_summary_sha256=files["summary.json"].sha256,
        case_results_sha256=files["case_results.jsonl"].sha256,
        case_results_byte_size=files["case_results.jsonl"].byte_size,
        case_rows=tuple(rows),
        data_dir=Path(tmp_path),
        suite_cases=(),
        suite_authority=GroundTruthAuthoritySnapshot({}, ()),
        result_store=store,
        result_manifest=manifest,
        result_files=files,
    )
    with factory() as session:
        run = session.get(AdapterEvaluationRun, claim.id)
        evidence = session.scalars(
            select(AdapterEvaluationEvidence).where(AdapterEvaluationEvidence.run_id == claim.id)
        ).all()
        audits = session.scalars(
            select(PersistentAuditEvent).where(
                PersistentAuditEvent.action == "adapter.evaluation.complete",
                PersistentAuditEvent.resource_id == str(claim.id),
            )
        ).all()
        assert run is not None and run.status == "succeeded"
        assert len(evidence) == 2 and {row.target for row in evidence} == {"baseline", "candidate"}
        assert all(row.retrieval_recall_at_5 == Decimal("0.500000000000000000") for row in evidence)
        assert len(audits) == 1
        assert all(row.delta_retrieval_recall_at_5 in {None, Decimal("0E-18")} for row in evidence)
    with pytest.raises(AdapterEvaluationQueueError, match="claim_lost"):
        finalize_success(
            factory,
            claim,
            baseline_metrics=metrics,
            candidate_metrics=metrics,
            baseline_gate=gate,
            candidate_gate=gate,
            result_manifest_sha256=files["manifest.json"].sha256,
            result_summary_sha256=files["summary.json"].sha256,
            case_results_sha256=files["case_results.jsonl"].sha256,
            case_results_byte_size=files["case_results.jsonl"].byte_size,
            case_rows=tuple(rows),
            data_dir=Path(tmp_path),
            suite_cases=(),
            suite_authority=GroundTruthAuthoritySnapshot({}, ()),
            result_store=store,
            result_manifest=manifest,
            result_files=files,
        )
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.action == "adapter.evaluation.complete",
                    PersistentAuditEvent.resource_id == str(claim.id),
                )
            )
            == 1
        )


def test_phase12_2_gate_failed_candidate_still_publishes_succeeded_run(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)
    _enqueue_eval(factory, authority, adapter, suite)
    claim = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    store, manifest, rows, files = _evaluation_artifacts(factory, claim, Path(tmp_path))
    metrics = AggregateMetrics(*(Decimal("0.5000") for _ in range(10)))
    finalize_success(
        factory,
        claim,
        baseline_metrics=metrics,
        candidate_metrics=metrics,
        baseline_gate=GateEvaluation(True, 0, {}),
        candidate_gate=GateEvaluation(False, 1, {"retrieval_recall_at_5_min": False}),
        result_manifest_sha256=files["manifest.json"].sha256,
        result_summary_sha256=files["summary.json"].sha256,
        case_results_sha256=files["case_results.jsonl"].sha256,
        case_results_byte_size=files["case_results.jsonl"].byte_size,
        case_rows=tuple(rows),
        data_dir=Path(tmp_path),
        suite_cases=(),
        suite_authority=GroundTruthAuthoritySnapshot({}, ()),
        result_store=store,
        result_manifest=manifest,
        result_files=files,
    )
    with factory() as session:
        run = session.get(AdapterEvaluationRun, claim.id)
        candidate = session.scalar(
            select(AdapterEvaluationEvidence).where(
                AdapterEvaluationEvidence.run_id == claim.id,
                AdapterEvaluationEvidence.target == "candidate",
            )
        )
        assert run is not None and run.status == "succeeded" and run.gate_status == "failed"
        assert candidate is not None and candidate.gate_status == "failed"


def test_phase12_2_finalization_vs_eb_finalization_has_no_deadlock(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)
    _enqueue_eval(factory, authority, adapter, suite)
    claim = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    store, manifest, rows, files = _evaluation_artifacts(factory, claim, Path(tmp_path))
    metrics = AggregateMetrics(*(Decimal("0.5000") for _ in range(10)))
    gate = GateEvaluation(True, 0, {})

    def finalize():
        return finalize_success(
            factory,
            claim,
            baseline_metrics=metrics,
            candidate_metrics=metrics,
            baseline_gate=gate,
            candidate_gate=gate,
            result_manifest_sha256=files["manifest.json"].sha256,
            result_summary_sha256=files["summary.json"].sha256,
            case_results_sha256=files["case_results.jsonl"].sha256,
            case_results_byte_size=files["case_results.jsonl"].byte_size,
            case_rows=tuple(rows),
            data_dir=Path(tmp_path),
            suite_cases=(),
            suite_authority=GroundTruthAuthoritySnapshot({}, ()),
            result_store=store,
            result_manifest=manifest,
            result_files=files,
        )

    def purge():
        return purge_adapter_artifacts(
            factory,
            data_dir=Path(tmp_path),
            department_id=authority.department_id,
            adapter_id=adapter.id,
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
            apply=True,
        )

    outcomes = _run_concurrently(finalize, purge)
    assert len(outcomes) == 2
    with factory() as session:
        run = session.get(AdapterEvaluationRun, claim.id)
        assert run is not None
        assert not (
            run.status in {"queued", "running"}
            and session.scalar(
                select(AdapterPurgeOperation.id).where(
                    AdapterPurgeOperation.department_id == authority.department_id,
                    AdapterPurgeOperation.adapter_id == adapter.id,
                    AdapterPurgeOperation.status.in_(("registered", "deleting")),
                )
            )
            is not None
        )


def test_phase12_2_cancellation_vs_eb_registration_has_no_deadlock(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)
    queued = _enqueue_eval(factory, authority, adapter, suite)

    def cancel():
        with factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.execute(text("SET LOCAL statement_timeout = '5s'"))
            return cancel_adapter_evaluation(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=adapter.id,
                evaluation_id=queued["id"],
                expected_version=queued["version"],
            )

    def purge():
        return purge_adapter_artifacts(
            factory,
            data_dir=Path(tmp_path),
            department_id=authority.department_id,
            adapter_id=adapter.id,
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
            apply=True,
        )

    outcomes = _run_concurrently(cancel, purge)
    assert len(outcomes) == 2
    assert not any(isinstance(value, BaseException) for value in outcomes)


def test_phase12_2_reclaim_vs_eb_registration_fences_stale_claim(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)
    _enqueue_eval(factory, authority, adapter, suite)
    stale = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert stale is not None
    with factory.begin() as session:
        session.execute(
            update(AdapterEvaluationRun)
            .where(AdapterEvaluationRun.id == stale.id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    def reclaim():
        return claim_next(factory, uuid4(), 30, authority.code_revision)

    def purge():
        return purge_adapter_artifacts(
            factory,
            data_dir=Path(tmp_path),
            department_id=authority.department_id,
            adapter_id=adapter.id,
            actor_issuer=authority.issuer,
            actor_subject=authority.subject,
            apply=True,
        )

    outcomes = _run_concurrently(reclaim, purge)
    assert len(outcomes) == 2
    assert fail_owned(factory, stale, "claim_lost") is False
    replacements = [value for value in outcomes if hasattr(value, "claim_token")]
    if replacements:
        replacement = replacements[0]
        assert replacement.claim_token != stale.claim_token
        assert replacement.publication_attempt_id != stale.publication_attempt_id
        assert replacement.attempt_number == stale.attempt_number + 1
    with factory() as session:
        attempts = session.scalars(
            select(AdapterEvaluationAttempt).where(AdapterEvaluationAttempt.run_id == stale.id)
        ).all()
        run = session.get(AdapterEvaluationRun, stale.id)
        assert run is not None
        assert any(item.status == "reclaimed" for item in attempts) or run.status in {
            "failed",
            "cancelled",
        }


def test_phase12_2_cancellation_fences_stale_finalizer(factory, tmp_path) -> None:
    authority, adapter, suite = _make_authority(factory)
    queued = _enqueue_eval(factory, authority, adapter, suite)
    claim = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    with factory() as session:
        current_version = session.get(AdapterEvaluationRun, claim.id).version
    with factory.begin() as session:
        cancelled = cancel_adapter_evaluation(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            evaluation_id=queued["id"],
            expected_version=current_version,
        )
        assert cancelled["status"] == "running"
    with pytest.raises(AdapterEvaluationQueueError, match="adapter_authority_changed|claim_lost"):
        finalize_success(
            factory,
            claim,
            baseline_metrics=AggregateMetrics(*(Decimal("0.5") for _ in range(10))),
            candidate_metrics=AggregateMetrics(*(Decimal("0.5") for _ in range(10))),
            baseline_gate=GateEvaluation(True, 0, {}),
            candidate_gate=GateEvaluation(True, 0, {}),
            result_manifest_sha256="a" * 64,
            result_summary_sha256="b" * 64,
            case_results_sha256="c" * 64,
            case_results_byte_size=1,
            data_dir=Path(tmp_path),
            suite_cases=(),
            suite_authority=GroundTruthAuthoritySnapshot({}, ()),
            result_store=object(),
            result_manifest={},
            result_files={},
        )
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterEvaluationEvidence.id)).where(
                    AdapterEvaluationEvidence.run_id == claim.id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.action == "adapter.evaluation.complete",
                    PersistentAuditEvent.resource_id == str(claim.id),
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "adapter_version",
        "adapter_status",
        "registry_version",
        "registry_status",
        "dependency_version",
        "dependency_status",
        "suite_version",
        "suite_status",
        "requester_status",
        "requester_expired",
    ),
)
def test_phase12_2_final_authority_mutation_matrix_fails_closed(
    factory, tmp_path, mutation
) -> None:
    authority, adapter, suite = _make_authority(factory)
    _enqueue_eval(factory, authority, adapter, suite)
    claim = claim_next(factory, uuid4(), 30, authority.code_revision)
    assert claim is not None
    with factory.begin() as session:
        if mutation == "adapter_version":
            session.get(Adapter, adapter.id).version += 1
        elif mutation == "adapter_status":
            session.get(Adapter, adapter.id).status = "purge_pending"
        elif mutation in {"registry_version", "registry_status"}:
            registry = session.scalar(
                select(AdapterRegistryAttempt).where(
                    AdapterRegistryAttempt.adapter_id == adapter.id
                )
            )
            assert registry is not None
            if mutation == "registry_version":
                registry.version += 1
            else:
                registry.status = "failed"
        elif mutation in {"dependency_version", "dependency_status"}:
            dependency = session.scalar(
                select(AdapterUpstreamDependency).where(
                    AdapterUpstreamDependency.adapter_id == adapter.id
                )
            )
            assert dependency is not None
            if mutation == "dependency_version":
                dependency.version += 1
            else:
                dependency.status = "released"
        elif mutation in {"suite_version", "suite_status"}:
            if mutation == "suite_version":
                session.get(EvaluationSuite, suite.id).version += 1
            else:
                session.get(EvaluationSuite, suite.id).status = "archived"
        elif mutation == "requester_status":
            session.get(UserIdentity, authority.admin_id).status = "revoked"
        else:
            session.scalar(
                select(Membership).where(
                    Membership.user_id == authority.admin_id,
                    Membership.department_id == authority.department_id,
                )
            ).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    metrics = AggregateMetrics(*(Decimal("0.5000") for _ in range(10)))
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
            data_dir=Path(tmp_path),
            suite_cases=(),
            suite_authority=GroundTruthAuthoritySnapshot({}, ()),
            result_store=object(),
            result_manifest={},
            result_files={},
        )
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterEvaluationEvidence.id)).where(
                    AdapterEvaluationEvidence.run_id == claim.id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.action == "adapter.evaluation.complete",
                    PersistentAuditEvent.resource_id == str(claim.id),
                )
            )
            == 0
        )
