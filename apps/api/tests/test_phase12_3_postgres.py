"""Required PostgreSQL 16 functional proof for Phase 12.3 governance."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from phase12_3_container_smoke import _prepare_registry_final
from sqlalchemy import func, inspect, select, text, update
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
from app.adapter_governance_services import (
    cancel_operation,
    enqueue_promotion,
    enqueue_rollback,
    release_rollback_retention,
    start_review,
    transition_review,
)
from app.adapter_governance_worker import (
    AdapterGovernanceWorkerError,
    claim_next_operation,
    fail_owned_operation,
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
    AdapterRollbackRetention,
    Department,
    DepartmentAdapterDeployment,
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
    # The public enqueue response intentionally omits internal snapshot fields.
    # Keep the test's durable-authority assertions explicit by loading those
    # fields from the persisted operation row rather than widening the API.
    with factory() as session:
        persisted = session.get(AdapterDeploymentOperation, operation["id"])
        assert persisted is not None
        operation.update(
            {
                "target_review_version": persisted.target_review_version,
                "target_evaluation_version": persisted.target_evaluation_version,
                "suite_id": persisted.suite_id,
            }
        )
    return authority, operation


def _clone_suite(factory: sessionmaker[Session], suite: EvaluationSuite) -> EvaluationSuite:
    """Create a second immutable suite with the same reviewed contract values."""

    values = {
        column.name: getattr(suite, column.name)
        for column in EvaluationSuite.__table__.columns
        if column.name not in {"id", "created_at", "updated_at"}
    }
    values["id"] = uuid4()
    with factory.begin() as session:
        clone = EvaluationSuite(**values)
        session.add(clone)
        session.flush()
        session.expunge(clone)
        return clone


def _prepare_approved_promotion_for_suite(
    factory: sessionmaker[Session],
    root: Path,
    authority: object,
    *,
    adapter_id,
    suite_id,
    expected_deployment_version: int,
) -> dict[str, object]:
    """Build a second real evaluation/review/promotion for the same adapter."""

    with factory.begin() as session:
        adapter = session.get(Adapter, adapter_id)
        assert adapter is not None
        enqueue_adapter_evaluation(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            suite_id=suite_id,
            expected_adapter_version=adapter.version,
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
            select(AdapterEvaluationRun).where(
                AdapterEvaluationRun.adapter_id == adapter_id,
                AdapterEvaluationRun.suite_id == suite_id,
            )
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
        return enqueue_promotion(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=adapter.id,
            review_id=approved["id"],
            expected_adapter_version=adapter.version,
            expected_review_version=approved["version"],
            expected_deployment_version=expected_deployment_version,
        )


def test_real_promotion_publishes_one_pointer_event_and_audit(factory, tmp_path: Path) -> None:
    authority, operation = _prepare_approved_promotion(factory, tmp_path)
    with factory() as session:
        before_adapter = session.get(Adapter, operation["target_adapter_id"])
        assert before_adapter is not None
        adapter_version = before_adapter.version
        adapter_status = before_adapter.status
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
        review = session.get(AdapterReview, operation["target_review_id"])
        run = session.get(AdapterEvaluationRun, operation["target_evaluation_id"])
        adapter = session.get(Adapter, operation["target_adapter_id"])
        retentions = session.scalar(
            select(func.count(AdapterRollbackRetention.id)).where(
                AdapterRollbackRetention.department_id == authority.department_id
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
        assert pointer.review_id == operation["target_review_id"]
        assert pointer.review_version == operation["target_review_version"]
        assert pointer.evaluation_id == operation["target_evaluation_id"]
        assert pointer.evaluation_version == operation["target_evaluation_version"]
        assert pointer.suite_id == operation["suite_id"]
        assert review is not None and review.status == "approved"
        assert run is not None and run.suite_id == pointer.suite_id
        assert adapter is not None and adapter.status == adapter_status
        assert adapter.version == adapter_version
        assert pointer.deployment_version == 1
        assert events == 1
        assert audits == 1
        assert retentions == 0


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


def test_target_snapshot_updates_suite_on_base_and_new_suite_transitions(
    factory, tmp_path: Path
) -> None:
    """Exercise the existing-pointer path with two immutable suite authorities."""

    authority, first_operation = _prepare_approved_promotion(factory, tmp_path)
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory() as session:
        adapter = session.get(Adapter, first_operation["target_adapter_id"])
        suite_a = session.get(EvaluationSuite, first_operation["suite_id"])
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        assert adapter is not None and suite_a is not None and pointer is not None
        adapter_version = adapter.version
        adapter_status = adapter.status
    suite_b = _clone_suite(factory, suite_a)

    with factory.begin() as session:
        rollback = enqueue_rollback(
            session,
            _principal(authority),
            _scope(authority),
            target="base",
            adapter_id=None,
            expected_adapter_version=None,
            retention_id=None,
            expected_retention_version=None,
            expected_deployment_version=1,
        )
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True

    with factory() as session:
        base_pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        active_retention = session.scalar(
            select(AdapterRollbackRetention).where(
                AdapterRollbackRetention.department_id == authority.department_id,
                AdapterRollbackRetention.adapter_id == first_operation["target_adapter_id"],
                AdapterRollbackRetention.status == "active",
            )
        )
        assert base_pointer is not None
        assert base_pointer.target_kind == "base"
        assert base_pointer.adapter_id is None
        assert base_pointer.review_id is None
        assert base_pointer.evaluation_id is None
        assert base_pointer.suite_id is None
        assert base_pointer.deployment_version == 2
        assert active_retention is not None

        # The approved review is retained while its rollback reservation is
        # active.  Release that exact non-current retention, then archive the
        # old review so the database's one-approved-review-per-adapter-version
        # constraint permits a second immutable suite authority for the same
        # adapter version.
        retention_id = active_retention.id
        retention_version = active_retention.version
    with factory.begin() as session:
        released = release_rollback_retention(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=first_operation["target_adapter_id"],
            retention_id=retention_id,
            expected_adapter_version=adapter_version,
            expected_retention_version=retention_version,
        )
        assert released["status"] == "released"
        archived = transition_review(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=first_operation["target_adapter_id"],
            review_id=first_operation["target_review_id"],
            action="archive",
            expected_adapter_version=adapter_version,
            expected_review_version=first_operation["target_review_version"],
        )
        assert archived["status"] == "archived"

    second_operation = _prepare_approved_promotion_for_suite(
        factory,
        tmp_path,
        authority,
        adapter_id=first_operation["target_adapter_id"],
        suite_id=suite_b.id,
        expected_deployment_version=2,
    )
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True

    with factory() as session:
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        adapter = session.get(Adapter, first_operation["target_adapter_id"])
        review = session.get(AdapterReview, second_operation["target_review_id"])
        run = session.get(AdapterEvaluationRun, second_operation["target_evaluation_id"])
        active_retentions = session.scalar(
            select(func.count(AdapterRollbackRetention.id)).where(
                AdapterRollbackRetention.department_id == authority.department_id,
                AdapterRollbackRetention.status == "active",
            )
        )
        promote_events = session.scalar(
            select(func.count(AdapterDeploymentEvent.id)).where(
                AdapterDeploymentEvent.department_id == authority.department_id,
                AdapterDeploymentEvent.event_type == "promote",
            )
        )
        rollback_events = session.scalar(
            select(func.count(AdapterDeploymentEvent.id)).where(
                AdapterDeploymentEvent.department_id == authority.department_id,
                AdapterDeploymentEvent.event_type == "rollback_base",
            )
        )
        success_audits = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.department_id == authority.department_id,
                PersistentAuditEvent.action == "adapter.deployment.success",
            )
        )
        assert pointer is not None and adapter is not None
        assert pointer.target_kind == "adapter"
        assert pointer.adapter_id == first_operation["target_adapter_id"]
        assert pointer.adapter_version == adapter_version
        assert pointer.review_id == second_operation["target_review_id"]
        assert pointer.evaluation_id == second_operation["target_evaluation_id"]
        assert pointer.suite_id == suite_b.id
        assert pointer.suite_id != suite_a.id
        assert pointer.deployment_version == 3
        assert review is not None and review.status == "approved"
        assert run is not None and run.suite_id == suite_b.id
        assert adapter.status == adapter_status == "validated"
        assert adapter.version == adapter_version
        assert active_retentions == 0
        assert promote_events == 2
        assert rollback_events == 1
        assert success_audits == 3
        assert rollback["id"] != second_operation["id"]


def test_manual_release_of_noncurrent_retention_is_metadata_only(factory, tmp_path: Path) -> None:
    authority, operation = _prepare_approved_promotion(factory, tmp_path)
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory.begin() as session:
        rollback = enqueue_rollback(
            session,
            _principal(authority),
            _scope(authority),
            target="base",
            adapter_id=None,
            expected_adapter_version=None,
            retention_id=None,
            expected_retention_version=None,
            expected_deployment_version=1,
        )
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory() as session:
        retention = session.scalar(
            select(AdapterRollbackRetention).where(
                AdapterRollbackRetention.department_id == authority.department_id,
                AdapterRollbackRetention.adapter_id == operation["target_adapter_id"],
                AdapterRollbackRetention.status == "active",
            )
        )
        adapter = session.get(Adapter, operation["target_adapter_id"])
        assert retention is not None and adapter is not None
        retention_id = retention.id
        retention_version = retention.version
        adapter_version = adapter.version
        final_path = (
            tmp_path
            / "adapters"
            / "registry"
            / str(authority.department_id)
            / str(operation["target_adapter_id"])
        )
    with factory.begin() as session:
        redeploy = enqueue_promotion(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=operation["target_adapter_id"],
            review_id=operation["target_review_id"],
            expected_adapter_version=adapter_version,
            expected_review_version=operation["target_review_version"],
            expected_deployment_version=2,
        )
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with pytest.raises(ServiceError, match="Current deployment cannot release"):
        with factory.begin() as session:
            release_rollback_retention(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=operation["target_adapter_id"],
                retention_id=retention_id,
                expected_adapter_version=adapter_version,
                expected_retention_version=retention_version,
            )
    with factory.begin() as session:
        rollback_again = enqueue_rollback(
            session,
            _principal(authority),
            _scope(authority),
            target="base",
            adapter_id=None,
            expected_adapter_version=None,
            retention_id=None,
            expected_retention_version=None,
            expected_deployment_version=3,
        )
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory.begin() as session:
        released = release_rollback_retention(
            session,
            _principal(authority),
            _scope(authority),
            adapter_id=operation["target_adapter_id"],
            retention_id=retention_id,
            expected_adapter_version=adapter_version,
            expected_retention_version=retention_version,
        )
        assert released["status"] == "released"
    with factory() as session:
        retention = session.get(AdapterRollbackRetention, retention_id)
        adapter = session.get(Adapter, operation["target_adapter_id"])
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        release_events = session.scalar(
            select(func.count(AdapterDeploymentEvent.id)).where(
                AdapterDeploymentEvent.rollback_retention_id == retention_id,
                AdapterDeploymentEvent.event_type == "rollback_retention_release",
            )
        )
        release_audits = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.action == "adapter.rollback_retention.release",
                PersistentAuditEvent.resource_id == str(retention_id),
            )
        )
        assert retention is not None and retention.status == "released"
        assert retention.version == retention_version + 1
        assert retention.release_reason == "manual_release"
        assert adapter is not None and adapter.version == adapter_version
        assert pointer is not None and pointer.deployment_version == 4
        assert release_events == 1
        assert release_audits == 1
        assert final_path.is_dir()
    with pytest.raises(ServiceError, match="authority changed"):
        with factory.begin() as session:
            release_rollback_retention(
                session,
                _principal(authority),
                _scope(authority),
                adapter_id=operation["target_adapter_id"],
                retention_id=retention_id,
                expected_adapter_version=adapter_version,
                expected_retention_version=retention_version,
            )
    assert rollback["id"] != operation["id"]
    assert redeploy["id"] != rollback_again["id"]


@pytest.mark.parametrize(
    "change",
    [
        "identity_revoked",
        "membership_revoked",
        "membership_expired",
        "membership_role_changed",
        "department_archived",
    ],
)
def test_final_requester_authorization_matrix_is_fail_closed(
    factory, tmp_path: Path, change: str
) -> None:
    authority, operation = _prepare_approved_promotion(factory, tmp_path)
    with factory.begin() as session:
        if change == "identity_revoked":
            identity = session.get(UserIdentity, authority.admin_id)
            assert identity is not None
            identity.status = "revoked"
        elif change == "department_archived":
            department = session.get(Department, authority.department_id)
            assert department is not None
            department.status = "archived"
        else:
            membership = session.scalar(
                select(Membership).where(
                    Membership.user_id == authority.admin_id,
                    Membership.department_id == authority.department_id,
                )
            )
            assert membership is not None
            if change == "membership_revoked":
                membership.status = "revoked"
            elif change == "membership_expired":
                membership.expires_at = func.clock_timestamp() - text("interval '1 second'")
            else:
                membership.role = "instructor"
    assert run_once(factory, data_dir=tmp_path, worker_id=uuid4()) is True
    with factory() as session:
        row = session.get(AdapterDeploymentOperation, operation["id"])
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        event_count = session.scalar(
            select(func.count(AdapterDeploymentEvent.id)).where(
                AdapterDeploymentEvent.operation_id == operation["id"]
            )
        )
        audit_count = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.action == "adapter.deployment.success",
                PersistentAuditEvent.resource_id == str(operation["id"]),
            )
        )
        retention_count = session.scalar(
            select(func.count(AdapterRollbackRetention.id)).where(
                AdapterRollbackRetention.department_id == authority.department_id
            )
        )
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "requester_unauthorized"
        assert pointer is None
        assert event_count == 0
        assert audit_count == 0
        assert retention_count == 0


def test_stale_claim_cannot_finalize_or_publish(factory, tmp_path: Path) -> None:
    _authority, operation = _prepare_approved_promotion(factory, tmp_path)
    claim = claim_next_operation(factory, worker_id=uuid4(), lease_seconds=120)
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


def test_live_claim_cancellation_is_terminalized_without_publication(
    factory, tmp_path: Path
) -> None:
    authority, operation = _prepare_approved_promotion(factory, tmp_path)
    claim = claim_next_operation(factory, worker_id=uuid4(), lease_seconds=120)
    assert claim is not None
    with factory.begin() as session:
        cancelled = cancel_operation(
            session,
            _principal(authority),
            _scope(authority),
            operation_id=claim.id,
            expected_version=claim.version,
        )
        assert cancelled["cancellation_requested"] is True
    with pytest.raises(AdapterGovernanceWorkerError, match="cancelled"):
        finalize_owned_operation(factory, claim, data_dir=tmp_path)
    fail_owned_operation(factory, claim, "cancelled")
    with factory() as session:
        row = session.get(AdapterDeploymentOperation, operation["id"])
        pointer = session.scalar(
            select(DepartmentAdapterDeployment).where(
                DepartmentAdapterDeployment.department_id == authority.department_id
            )
        )
        events = session.scalar(
            select(func.count(AdapterDeploymentEvent.id)).where(
                AdapterDeploymentEvent.operation_id == operation["id"]
            )
        )
        audits = session.scalar(
            select(func.count(PersistentAuditEvent.id)).where(
                PersistentAuditEvent.action == "adapter.deployment.success",
                PersistentAuditEvent.resource_id == str(operation["id"]),
            )
        )
        assert row is not None and row.status == "cancelled"
        assert row.error_code == "cancelled"
        assert pointer is None
        assert events == 0
        assert audits == 0


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


def test_phase12_3_migration_cycle_preserves_phase12_2_rows(engine) -> None:
    """Downgrade only governance, preserve evaluation data, and restore head."""

    config = Config("alembic.ini")
    with engine.connect() as connection:
        evaluation_count = connection.scalar(text("SELECT count(*) FROM adapter_evaluation_runs"))
    command.downgrade(config, "0015_phase12_adapter_evaluation")
    try:
        inspector = inspect(engine)
        assert {
            "adapter_reviews",
            "department_adapter_deployments",
            "adapter_deployment_operations",
            "adapter_deployment_events",
            "adapter_rollback_retentions",
        }.isdisjoint(set(inspector.get_table_names()))
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM adapter_evaluation_runs")) == (
                evaluation_count
            )
    finally:
        command.upgrade(config, "0016_phase12_adapter_governance")
    inspector = inspect(engine)
    assert {
        "adapter_reviews",
        "department_adapter_deployments",
        "adapter_deployment_operations",
        "adapter_deployment_events",
        "adapter_rollback_retentions",
    }.issubset(set(inspector.get_table_names()))
