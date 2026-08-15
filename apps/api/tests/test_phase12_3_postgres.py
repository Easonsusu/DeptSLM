"""Required PostgreSQL 16 functional proof for Phase 12.3 governance."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from phase12_3_container_smoke import _prepare_registry_final
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_phase12_2_postgres import (
    _evaluation_artifacts,
    _make_authority,
    _principal,
    _scope,
    _storage,
)

from alembic import command
from app.adapter_evaluation_queue import claim_next, finalize_success
from app.adapter_evaluation_services import enqueue_adapter_evaluation
from app.adapter_governance_services import enqueue_promotion, start_review, transition_review
from app.adapter_governance_worker import (
    AdapterGovernanceWorkerError,
    finalize_owned_operation,
    run_once,
)
from app.database import create_database_engine
from app.evaluation_domain import AggregateMetrics, GateEvaluation
from app.evaluation_suites import GroundTruthAuthoritySnapshot
from app.models import (
    Adapter,
    AdapterDeploymentEvent,
    AdapterDeploymentOperation,
    AdapterEvaluationRun,
    AdapterReview,
    DepartmentAdapterDeployment,
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


def _prepare_approved_promotion(
    factory: sessionmaker[Session], root: Path
) -> tuple[object, dict[str, object]]:
    """Create one complete approved promotion using the real Phase 12.2 authorities."""

    _storage(root)
    authority, _adapter, suite = _make_authority(factory)
    adapter_id, adapter_version = _prepare_registry_final(root, factory, authority)
    with factory.begin() as session:
        enqueue_adapter_evaluation(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter_id,
            suite_id=suite.id,
            expected_adapter_version=adapter_version,
            code_revision=authority.code_revision,
        )
    claim = claim_next(factory, uuid4(), 120, authority.code_revision)
    assert claim is not None
    store, manifest, rows, files = _evaluation_artifacts(factory, claim, root)
    metrics = AggregateMetrics(*(Decimal("0.5") for _ in AggregateMetrics.__dataclass_fields__))
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
        data_dir=root,
        suite_cases=(),
        suite_authority=GroundTruthAuthoritySnapshot({}, ()),
        result_store=store,
        result_manifest=manifest,
        result_files=files,
    )
    with factory.begin() as session:
        adapter = session.get(Adapter, adapter_id)
        run = session.scalar(
            select(AdapterEvaluationRun).where(AdapterEvaluationRun.adapter_id == adapter_id)
        )
        assert adapter is not None and run is not None
        review = start_review(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            evaluation_id=run.id,
            expected_adapter_version=adapter.version,
            expected_evaluation_version=run.version,
        )
        approved = transition_review(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            review_id=review["id"],
            action="approve",
            expected_adapter_version=adapter.version,
            expected_review_version=review["version"],
        )
        operation = enqueue_promotion(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            review_id=approved["id"],
            expected_adapter_version=adapter.version,
            expected_review_version=approved["version"],
            expected_deployment_version=0,
        )
    return authority, operation


def test_real_promotion_publishes_one_pointer_event_and_audit(factory, tmp_path: Path) -> None:
    authority, operation = _prepare_approved_promotion(factory, tmp_path)
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory() as session:
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        events = session.scalar(
            select(func.count(AdapterDeploymentEvent.id)).where(
                AdapterDeploymentEvent.operation_id == operation["id"],
                AdapterDeploymentEvent.event_type == "promote",
            )
        )
        audits = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.action == "adapter.deployment.success",
                PersistentAuditEvent.resource_id == str(operation["id"]),
            )
        )
        assert pointer is not None
        assert pointer.target_kind == "adapter"
        assert pointer.adapter_id == operation["target_adapter_id"]
        assert pointer.adapter_version == operation["target_adapter_version"]
        assert pointer.deployment_version == 1
        assert events == 1
        assert audits == 1


def test_requester_revocation_after_enqueue_blocks_final_success(factory, tmp_path: Path) -> None:
    authority, operation = _prepare_approved_promotion(factory, tmp_path)
    with factory.begin() as session:
        identity = session.get(UserIdentity, authority.admin_id)
        assert identity is not None
        identity.status = "revoked"
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory() as session:
        row = session.get(AdapterDeploymentOperation, operation["id"])
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        events = session.scalar(
            select(func.count(AdapterDeploymentEvent.id)).where(
                AdapterDeploymentEvent.operation_id == operation["id"],
                AdapterDeploymentEvent.event_type == "promote",
            )
        )
        audits = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.action == "adapter.deployment.success",
                PersistentAuditEvent.resource_id == str(operation["id"]),
            )
        )
        assert row is not None and row.status == "failed"
        assert row.error_code == "requester_unauthorized"
        assert pointer is None
        assert events == 0
        assert audits == 0


def test_stale_claim_cannot_finalize_or_publish(factory, tmp_path: Path) -> None:
    _authority, operation = _prepare_approved_promotion(factory, tmp_path)
    claim = claim_next(factory, uuid4(), 120, "a" * 40)
    assert claim is not None
    with factory.begin() as session:
        session.execute(
            update(AdapterDeploymentOperation)
            .where(AdapterDeploymentOperation.id == claim.id)
            .values(claim_token=uuid4())
        )
    with pytest.raises(AdapterGovernanceWorkerError, match="claim_lost"):
        finalize_owned_operation(factory, claim, data_dir=tmp_path)
    with factory() as session:
        assert (
            session.scalar(
                select(func.count(AdapterDeploymentEvent.id)).where(
                    AdapterDeploymentEvent.operation_id == operation["id"]
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(PersistentAuditEvent.id)).where(
                    PersistentAuditEvent.action == "adapter.deployment.success",
                    PersistentAuditEvent.resource_id == str(operation["id"]),
                )
            )
            == 0
        )


def test_path_adapter_mismatch_is_rejected_before_review_mutation(factory, tmp_path: Path) -> None:
    _storage(tmp_path)
    authority, _adapter, suite = _make_authority(factory)
    adapter_id, adapter_version = _prepare_registry_final(tmp_path, factory, authority)
    with factory.begin() as session:
        queued = enqueue_adapter_evaluation(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter_id,
            suite_id=suite.id,
            expected_adapter_version=adapter_version,
            code_revision=authority.code_revision,
        )
    with pytest.raises(ServiceError, match="Evaluation not found"):
        with factory.begin() as session:
            start_review(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=uuid4(),
                evaluation_id=queued["id"],
                expected_adapter_version=adapter_version,
                expected_evaluation_version=queued["version"],
            )


def test_path_adapter_mismatch_is_rejected_before_review_transition(
    factory, tmp_path: Path
) -> None:
    authority, operation = _prepare_approved_promotion(factory, tmp_path)
    with factory() as session:
        review = session.scalar(
            select(AdapterReview).where(
                AdapterReview.department_id == authority.department_id,
                AdapterReview.adapter_id == operation["target_adapter_id"],
            )
        )
        assert review is not None
        adapter_version = operation["target_adapter_version"]
    with pytest.raises(ServiceError, match="Adapter review not found"):
        with factory.begin() as session:
            transition_review(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=uuid4(),
                review_id=review.id,
                action="archive",
                expected_adapter_version=adapter_version,
                expected_review_version=review.version,
            )
