"""Phase 12.3 adapter review and deployment-governance services.

Governance is intentionally separate from the immutable adapter artifact
lifecycle.  These services persist only closed metadata, use the canonical
department-first authorization helper, and never load or route an adapter.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapter_evaluation_domain import (
    ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION,
    ADAPTER_EVALUATION_GATE_POLICY_VERSION,
    ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION,
    ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION,
    ADAPTER_EVALUATION_SEED_POLICY_VERSION,
)
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.models import (
    Adapter,
    AdapterDeploymentEvent,
    AdapterDeploymentOperation,
    AdapterEvaluationEvidence,
    AdapterEvaluationRun,
    AdapterRegistryAttempt,
    AdapterReview,
    AdapterRollbackRetention,
    AdapterUpstreamDependency,
    DepartmentAdapterDeployment,
    EvaluationSuite,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction

GOVERNANCE_ADMIN_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
GOVERNANCE_READ_ROLES = frozenset(
    {DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN, DepartmentRole.INSTRUCTOR}
)
_CODE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
_BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"


@dataclass(frozen=True, slots=True)
class GovernancePage:
    items: tuple[dict[str, object], ...]
    limit: int
    next_cursor: str | None


def _safe_version(value: object, *, label: str, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if allow_zero else 1):
        raise ServiceError(422, f"Invalid expected {label} version")
    return value


def _safe_uuid(value: object, *, label: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ServiceError(422, f"Invalid {label} selector")
    return value


def _validate_page(limit: object, cursor: object) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
        or cursor is not None
        and (not isinstance(cursor, str) or len(cursor) > 1024)
    ):
        raise ServiceError(422, "Invalid pagination")


def _encode_cursor(created_at: datetime, row_id: UUID) -> str:
    raw = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(row_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError
        raw = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(raw).decode())
        if set(value) != {"created_at", "id"}:
            raise ValueError
        created_at = datetime.fromisoformat(value["created_at"])
        row_id = UUID(value["id"])
        if created_at.tzinfo is None or row_id.int == 0:
            raise ValueError
        return created_at, row_id
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ServiceError(422, "Invalid pagination") from None


def _active_purge(session: Session, department_id: UUID, adapter_id: UUID) -> bool:
    from app.models import AdapterPurgeOperation

    return (
        session.scalar(
            select(AdapterPurgeOperation.id).where(
                AdapterPurgeOperation.department_id == department_id,
                AdapterPurgeOperation.adapter_id == adapter_id,
                AdapterPurgeOperation.status.in_(("registered", "deleting")),
            )
        )
        is not None
    )


def _active_evaluation(session: Session, department_id: UUID, adapter_id: UUID) -> bool:
    return (
        session.scalar(
            select(AdapterEvaluationRun.id).where(
                AdapterEvaluationRun.department_id == department_id,
                AdapterEvaluationRun.adapter_id == adapter_id,
                AdapterEvaluationRun.status.in_(("queued", "running")),
            )
        )
        is not None
    )


def _active_operation(session: Session, department_id: UUID) -> AdapterDeploymentOperation | None:
    return session.scalar(
        select(AdapterDeploymentOperation)
        .where(
            AdapterDeploymentOperation.department_id == department_id,
            AdapterDeploymentOperation.status.in_(("queued", "running")),
        )
        .with_for_update()
    )


def _safe_review(
    row: AdapterReview, *, evaluation_gate_status: str | None = None
) -> dict[str, object]:
    return {
        "id": row.id,
        "department_id": row.department_id,
        "adapter_id": row.adapter_id,
        "adapter_version": row.adapter_version,
        "evaluation_id": row.evaluation_id,
        "evaluation_version": row.evaluation_version,
        "suite_id": row.suite_id,
        "suite_version": row.suite_version,
        "status": row.status,
        "evaluation_gate_status": evaluation_gate_status,
        "version": row.version,
        "started_at": row.started_at,
        "decided_at": row.decided_at,
        "archived_at": row.archived_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _safe_deployment(
    row: DepartmentAdapterDeployment | None, department_id: UUID
) -> dict[str, object]:
    if row is None:
        return {
            "department_id": department_id,
            "target": "base",
            "explicit": False,
            "deployment_version": 0,
            "version": 0,
            "adapter_id": None,
            "adapter_version": None,
            "review_id": None,
            "evaluation_id": None,
            "base_model_id": _BASE_MODEL_ID,
            "base_model_revision": _BASE_MODEL_REVISION,
            "created_at": None,
            "updated_at": None,
        }
    return {
        "department_id": row.department_id,
        "target": row.target_kind,
        "explicit": True,
        "deployment_version": row.deployment_version,
        "version": row.version,
        "adapter_id": row.adapter_id,
        "adapter_version": row.adapter_version,
        "review_id": row.review_id,
        "evaluation_id": row.evaluation_id,
        "base_model_id": row.base_model_id,
        "base_model_revision": row.base_model_revision,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _safe_operation(row: AdapterDeploymentOperation) -> dict[str, object]:
    return {
        "id": row.id,
        "department_id": row.department_id,
        "operation_type": row.operation_type,
        "status": row.status,
        "expected_deployment_version": row.expected_deployment_version,
        "target": "adapter" if row.target_adapter_id is not None else "base",
        "target_adapter_id": row.target_adapter_id,
        "target_adapter_version": row.target_adapter_version,
        "target_review_id": row.target_review_id,
        "target_evaluation_id": row.target_evaluation_id,
        "target_retention_id": row.target_retention_id,
        "attempt_number": row.attempt_number,
        "error_code": row.error_code,
        "cancellation_requested": row.cancellation_requested_at is not None,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "version": row.version,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _safe_event(row: AdapterDeploymentEvent) -> dict[str, object]:
    return {
        "id": row.id,
        "department_id": row.department_id,
        "operation_id": row.operation_id,
        "event_type": row.event_type,
        "deployment_version_before": row.deployment_version_before,
        "deployment_version_after": row.deployment_version_after,
        "from_target": row.from_target_kind,
        "from_adapter_id": row.from_adapter_id,
        "from_adapter_version": row.from_adapter_version,
        "to_target": row.to_target_kind,
        "to_adapter_id": row.to_adapter_id,
        "to_adapter_version": row.to_adapter_version,
        "approved_review_id": row.approved_review_id,
        "evaluation_id": row.evaluation_id,
        "suite_id": row.suite_id,
        "base_model_id": row.base_model_id,
        "base_model_revision": row.base_model_revision,
        "rollback_retention_id": row.rollback_retention_id,
        "created_at": row.created_at,
    }


def _safe_retention(row: AdapterRollbackRetention) -> dict[str, object]:
    return {
        "id": row.id,
        "department_id": row.department_id,
        "adapter_id": row.adapter_id,
        "adapter_version": row.adapter_version,
        "approved_review_id": row.approved_review_id,
        "evaluation_id": row.evaluation_id,
        "status": row.status,
        "release_reason": row.release_reason,
        "version": row.version,
        "created_at": row.created_at,
        "released_at": row.released_at,
    }


def _load_evaluation_authority(
    session: Session,
    department_id: UUID,
    adapter_id: UUID,
    evaluation_id: UUID,
    *,
    lock: bool,
) -> tuple[
    Adapter,
    AdapterEvaluationRun,
    EvaluationSuite,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    tuple[AdapterEvaluationEvidence, AdapterEvaluationEvidence],
]:
    adapter_query = select(Adapter).where(
        Adapter.id == adapter_id, Adapter.department_id == department_id
    )
    if lock:
        adapter_query = adapter_query.with_for_update()
    adapter = session.scalar(adapter_query)
    run_query = select(AdapterEvaluationRun).where(
        AdapterEvaluationRun.id == evaluation_id,
        AdapterEvaluationRun.department_id == department_id,
        AdapterEvaluationRun.adapter_id == adapter_id,
    )
    if lock:
        run_query = run_query.with_for_update()
    run = session.scalar(run_query)
    if adapter is None or run is None:
        raise ServiceError(404, "Adapter evaluation not found")
    suite = session.scalar(
        select(EvaluationSuite)
        .where(EvaluationSuite.id == run.suite_id, EvaluationSuite.department_id == department_id)
        .with_for_update()
        if lock
        else select(EvaluationSuite).where(
            EvaluationSuite.id == run.suite_id, EvaluationSuite.department_id == department_id
        )
    )
    registry = session.scalar(
        select(AdapterRegistryAttempt)
        .where(
            AdapterRegistryAttempt.id == run.registry_attempt_id,
            AdapterRegistryAttempt.department_id == department_id,
            AdapterRegistryAttempt.adapter_id == adapter_id,
            AdapterRegistryAttempt.publication_attempt_id == run.registry_publication_attempt_id,
            AdapterRegistryAttempt.attempt_number == run.registry_attempt_number,
        )
        .with_for_update()
    )
    dependency = session.scalar(
        select(AdapterUpstreamDependency)
        .where(
            AdapterUpstreamDependency.id == run.dependency_id,
            AdapterUpstreamDependency.department_id == department_id,
            AdapterUpstreamDependency.adapter_id == adapter_id,
        )
        .with_for_update()
    )
    evidence = tuple(
        session.scalars(
            select(AdapterEvaluationEvidence)
            .where(
                AdapterEvaluationEvidence.run_id == run.id,
                AdapterEvaluationEvidence.department_id == department_id,
                AdapterEvaluationEvidence.adapter_id == adapter_id,
                AdapterEvaluationEvidence.suite_id == run.suite_id,
            )
            .order_by(AdapterEvaluationEvidence.target)
        )
    )
    if suite is None or registry is None or dependency is None or len(evidence) != 2:
        raise ServiceError(409, "Evaluation authority is unavailable")
    by_target = {row.target: row for row in evidence}
    if set(by_target) != {"baseline", "candidate"}:
        raise ServiceError(409, "Evaluation evidence is incomplete")
    return (
        adapter,
        run,
        suite,
        registry,
        dependency,
        (by_target["baseline"], by_target["candidate"]),
    )


def _validate_review_start(
    adapter: Adapter,
    run: AdapterEvaluationRun,
    suite: EvaluationSuite,
    registry: AdapterRegistryAttempt,
    dependency: AdapterUpstreamDependency,
    evidence: tuple[AdapterEvaluationEvidence, AdapterEvaluationEvidence],
) -> None:
    if (
        run.status != "succeeded"
        or run.gate_status not in {"passed", "failed"}
        or run.cancellation_requested_at is not None
        or run.cancelled_at is not None
        or run.finished_at is None
        or run.result_manifest_sha256 is None
        or run.result_summary_sha256 is None
        or run.case_results_sha256 is None
        or run.case_results_byte_size is None
        or adapter.version != run.adapter_version
        or suite.version != run.suite_version
        or adapter.status in {"purge_pending", "purged"}
        or adapter.purged_at is not None
        or registry.status != "succeeded"
        or dependency.status != "active"
        or suite.status != "active"
        or run.runner_contract_version != ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION
        or run.artifact_contract_version != ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION
        or run.metric_contract_version != ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION
        or run.gate_policy_version != ADAPTER_EVALUATION_GATE_POLICY_VERSION
        or run.seed_policy_version != ADAPTER_EVALUATION_SEED_POLICY_VERSION
        or run.base_model_id != _BASE_MODEL_ID
        or run.base_model_revision != _BASE_MODEL_REVISION
        or run.registry_attempt_version != registry.version
        or run.registry_attempt_id != registry.id
        or run.registry_manifest_sha256 != adapter.registry_manifest_sha256
        or run.registry_adapter_config_sha256 != adapter.registry_adapter_config_sha256
        or run.registry_adapter_config_byte_size != adapter.registry_adapter_config_byte_size
        or run.registry_adapter_model_sha256 != adapter.registry_adapter_model_sha256
        or run.registry_adapter_model_byte_size != adapter.registry_adapter_model_byte_size
        or run.registry_publication_attempt_id != registry.publication_attempt_id
        or run.registry_attempt_number != registry.attempt_number
        or registry.execution_scope_id != adapter.execution_scope_id
        or run.dependency_id != dependency.id
        or run.dependency_version != dependency.version
        or run.suite_artifact_manifest_sha256 != suite.artifact_manifest_sha256
        or run.suite_canonical_cases_sha256 != suite.canonical_cases_sha256
        or run.suite_canonical_cases_byte_size != suite.canonical_cases_byte_size
        or evidence[0].target != "baseline"
        or evidence[1].target != "candidate"
    ):
        raise ServiceError(409, "Evaluation is not eligible for review")


def _review_snapshot_matches(
    review: AdapterReview,
    adapter: Adapter,
    run: AdapterEvaluationRun,
    suite: EvaluationSuite,
    registry: AdapterRegistryAttempt,
    dependency: AdapterUpstreamDependency,
    evidence: tuple[AdapterEvaluationEvidence, AdapterEvaluationEvidence],
) -> bool:
    """Require every review authority snapshot to equal current PostgreSQL state."""

    return (
        review.department_id == adapter.department_id == run.department_id == suite.department_id
        and review.adapter_id == adapter.id == run.adapter_id == registry.adapter_id
        and review.adapter_version == adapter.version == run.adapter_version
        and review.evaluation_id == run.id
        and review.evaluation_version == run.version
        and review.suite_id == suite.id == run.suite_id
        and review.suite_version == suite.version == run.suite_version
        and review.registry_attempt_id == registry.id == run.registry_attempt_id
        and review.registry_attempt_version == registry.version == run.registry_attempt_version
        and review.registry_publication_attempt_id
        == registry.publication_attempt_id
        == run.registry_publication_attempt_id
        and review.registry_attempt_number == registry.attempt_number == run.registry_attempt_number
        and review.registry_execution_scope_id
        == registry.execution_scope_id
        == adapter.execution_scope_id
        and review.registry_manifest_sha256
        == adapter.registry_manifest_sha256
        == run.registry_manifest_sha256
        and review.registry_adapter_config_sha256
        == adapter.registry_adapter_config_sha256
        == run.registry_adapter_config_sha256
        and review.registry_adapter_config_byte_size
        == adapter.registry_adapter_config_byte_size
        == run.registry_adapter_config_byte_size
        and review.registry_adapter_model_sha256
        == adapter.registry_adapter_model_sha256
        == run.registry_adapter_model_sha256
        and review.registry_adapter_model_byte_size
        == adapter.registry_adapter_model_byte_size
        == run.registry_adapter_model_byte_size
        and review.dependency_id == dependency.id == run.dependency_id
        and review.dependency_version == dependency.version == run.dependency_version
        and review.base_model_id == run.base_model_id
        and review.base_model_revision == run.base_model_revision
        and review.runner_contract_version
        == run.runner_contract_version
        == ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION
        and review.artifact_contract_version
        == run.artifact_contract_version
        == ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION
        and review.metric_contract_version
        == run.metric_contract_version
        == ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION
        and review.gate_policy_version
        == run.gate_policy_version
        == ADAPTER_EVALUATION_GATE_POLICY_VERSION
        and review.seed_policy_version
        == run.seed_policy_version
        == ADAPTER_EVALUATION_SEED_POLICY_VERSION
        and review.code_revision == run.code_revision
        and review.suite_artifact_manifest_sha256
        == suite.artifact_manifest_sha256
        == run.suite_artifact_manifest_sha256
        and review.suite_canonical_cases_sha256
        == suite.canonical_cases_sha256
        == run.suite_canonical_cases_sha256
        and review.suite_canonical_cases_byte_size
        == suite.canonical_cases_byte_size
        == run.suite_canonical_cases_byte_size
        and review.result_manifest_sha256 == run.result_manifest_sha256
        and review.result_summary_sha256 == run.result_summary_sha256
        and review.case_results_sha256 == run.case_results_sha256
        and review.case_results_byte_size == run.case_results_byte_size
        and review.baseline_evidence_id == evidence[0].id
        and review.candidate_evidence_id == evidence[1].id
        and all(
            row.run_id == run.id
            and row.department_id == run.department_id
            and row.adapter_id == run.adapter_id
            and row.suite_id == run.suite_id
            and row.adapter_version == run.adapter_version
            for row in evidence
        )
    )


def _validate_approval(
    adapter: Adapter,
    run: AdapterEvaluationRun,
    suite: EvaluationSuite,
    registry: AdapterRegistryAttempt,
    dependency: AdapterUpstreamDependency,
    evidence: tuple[AdapterEvaluationEvidence, AdapterEvaluationEvidence],
) -> None:
    _validate_review_start(adapter, run, suite, registry, dependency, evidence)
    candidate = evidence[1]
    if (
        adapter.status != "validated"
        or adapter.verified_governance_lineage is not True
        or adapter.verified_artifact_compatibility is not True
        or adapter.training_provenance_verified is not False
        or candidate.gate_status != "passed"
        or candidate.failed_gate_count != 0
        or run.gate_status != "passed"
        or registry.ownership_manifest is None
        or adapter.registry_manifest_sha256 is None
        or adapter.registry_adapter_config_sha256 is None
        or adapter.registry_adapter_model_sha256 is None
    ):
        raise ServiceError(409, "Evaluation quality gate is not eligible for approval")


def start_review(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    evaluation_id: UUID,
    expected_adapter_version: int,
    expected_evaluation_version: int,
) -> dict[str, object]:
    _safe_uuid(adapter_id, label="adapter")
    _safe_uuid(evaluation_id, label="evaluation")
    _safe_version(expected_adapter_version, label="adapter")
    _safe_version(expected_evaluation_version, label="evaluation")
    try:
        auth = authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_ADMIN_ROLES,
            lock=True,
            audit_action="adapter.review.start.authorization",
        )
        run = session.scalar(
            select(AdapterEvaluationRun)
            .where(
                AdapterEvaluationRun.id == evaluation_id,
                AdapterEvaluationRun.department_id == scope.department.value,
                AdapterEvaluationRun.adapter_id == adapter_id,
            )
            .with_for_update()
        )
        if run is None:
            raise ServiceError(404, "Evaluation not found")
        if (
            run.version != expected_evaluation_version
            or run.adapter_version != expected_adapter_version
        ):
            raise ServiceError(409, "Evaluation authority changed")
        adapter, run, suite, registry, dependency, evidence = _load_evaluation_authority(
            session, scope.department.value, adapter_id, evaluation_id, lock=True
        )
        if adapter.version != expected_adapter_version:
            raise ServiceError(409, "Adapter authority changed")
        _validate_review_start(adapter, run, suite, registry, dependency, evidence)
        if _active_purge(session, scope.department.value, adapter.id) or _active_evaluation(
            session, scope.department.value, adapter.id
        ):
            # The run itself is terminal, so only an active purge is relevant;
            # an active second evaluation remains a separate safety fence.
            raise ServiceError(409, "Adapter governance conflicts with active operation")
        pending = session.scalar(
            select(AdapterReview)
            .where(
                AdapterReview.department_id == scope.department.value,
                AdapterReview.adapter_id == adapter.id,
                AdapterReview.adapter_version == adapter.version,
                AdapterReview.status == "pending",
            )
            .with_for_update()
        )
        if pending is not None:
            raise ServiceError(409, "A review is already pending")
        review = AdapterReview(
            id=uuid4(),
            department_id=scope.department.value,
            adapter_id=adapter.id,
            adapter_version=adapter.version,
            evaluation_id=run.id,
            evaluation_version=run.version,
            baseline_evidence_id=evidence[0].id,
            candidate_evidence_id=evidence[1].id,
            suite_id=suite.id,
            suite_version=suite.version,
            registry_attempt_id=registry.id,
            registry_attempt_version=registry.version,
            registry_publication_attempt_id=registry.publication_attempt_id,
            registry_attempt_number=registry.attempt_number,
            registry_execution_scope_id=registry.execution_scope_id,
            registry_manifest_sha256=adapter.registry_manifest_sha256 or "",
            registry_adapter_config_sha256=adapter.registry_adapter_config_sha256 or "",
            registry_adapter_config_byte_size=adapter.registry_adapter_config_byte_size or 0,
            registry_adapter_model_sha256=adapter.registry_adapter_model_sha256 or "",
            registry_adapter_model_byte_size=adapter.registry_adapter_model_byte_size or 0,
            dependency_id=dependency.id,
            dependency_version=dependency.version,
            base_model_id=run.base_model_id,
            base_model_revision=run.base_model_revision,
            runner_contract_version=run.runner_contract_version,
            artifact_contract_version=run.artifact_contract_version,
            metric_contract_version=run.metric_contract_version,
            gate_policy_version=run.gate_policy_version,
            seed_policy_version=run.seed_policy_version,
            code_revision=run.code_revision,
            suite_artifact_manifest_sha256=suite.artifact_manifest_sha256,
            suite_canonical_cases_sha256=suite.canonical_cases_sha256,
            suite_canonical_cases_byte_size=suite.canonical_cases_byte_size,
            result_manifest_sha256=run.result_manifest_sha256 or "",
            result_summary_sha256=run.result_summary_sha256 or "",
            case_results_sha256=run.case_results_sha256 or "",
            case_results_byte_size=run.case_results_byte_size or 0,
            status="pending",
            requested_by_user_id=auth.identity.id,
            version=1,
        )
        session.add(review)
        session.flush()
        append_mutation_audit(
            session,
            actor=auth.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action="adapter.review.start",
            resource_type="adapter_review",
            resource_id=review.id,
        )
        return _safe_review(review, evaluation_gate_status=run.gate_status)
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Adapter review conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def transition_review(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    review_id: UUID,
    action: str,
    expected_adapter_version: int,
    expected_review_version: int,
) -> dict[str, object]:
    _safe_uuid(adapter_id, label="adapter")
    _safe_uuid(review_id, label="review")
    _safe_version(expected_adapter_version, label="adapter")
    _safe_version(expected_review_version, label="review")
    if action not in {"approve", "reject", "archive"}:
        raise ServiceError(422, "Invalid review action")
    try:
        auth = authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_ADMIN_ROLES,
            lock=True,
            audit_action=f"adapter.review.{action}.authorization",
        )
        review = session.scalar(
            select(AdapterReview)
            .where(
                AdapterReview.id == review_id,
                AdapterReview.department_id == scope.department.value,
                AdapterReview.adapter_id == adapter_id,
            )
            .with_for_update()
        )
        if review is None:
            raise ServiceError(404, "Adapter review not found")
        if (
            review.version != expected_review_version
            or review.adapter_version != expected_adapter_version
        ):
            raise ServiceError(409, "Review authority changed")
        adapter, run, suite, registry, dependency, evidence = _load_evaluation_authority(
            session, scope.department.value, adapter_id, review.evaluation_id, lock=True
        )
        if (
            review.evaluation_id != run.id
            or review.baseline_evidence_id != evidence[0].id
            or review.candidate_evidence_id != evidence[1].id
            or not _review_snapshot_matches(
                review, adapter, run, suite, registry, dependency, evidence
            )
        ):
            raise ServiceError(409, "Review authority changed")
        now = session.scalar(select(func.clock_timestamp()))
        if action == "approve":
            if review.status != "pending":
                raise ServiceError(409, "Review is not pending")
            _validate_approval(adapter, run, suite, registry, dependency, evidence)
            review.status = "approved"
            review.decided_at = now
            review.reviewed_by_user_id = auth.identity.id
        elif action == "reject":
            if review.status != "pending":
                raise ServiceError(409, "Review is not pending")
            _validate_review_start(adapter, run, suite, registry, dependency, evidence)
            review.status = "rejected"
            review.decided_at = now
            review.reviewed_by_user_id = auth.identity.id
        else:
            if review.status not in {"approved", "rejected"}:
                raise ServiceError(409, "Review cannot be archived")
            if review.status == "approved":
                current = _current_deployment(session, scope.department.value)
                retained = session.scalar(
                    select(AdapterRollbackRetention.id).where(
                        AdapterRollbackRetention.department_id == scope.department.value,
                        AdapterRollbackRetention.adapter_id == review.adapter_id,
                        AdapterRollbackRetention.adapter_version == review.adapter_version,
                        AdapterRollbackRetention.status == "active",
                    )
                )
                active_op = _active_operation(session, scope.department.value)
                if (
                    (
                        current is not None
                        and current.target_kind == "adapter"
                        and current.adapter_id == review.adapter_id
                        and current.adapter_version == review.adapter_version
                    )
                    or retained is not None
                    or active_op is not None
                ):
                    raise ServiceError(409, "Approved review is retained by deployment governance")
            review.status = "archived"
            review.archived_at = now
            review.decided_at = review.decided_at or now
            review.reviewed_by_user_id = review.reviewed_by_user_id or auth.identity.id
        review.version += 1
        session.flush()
        append_mutation_audit(
            session,
            actor=auth.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action=f"adapter.review.{action}",
            resource_type="adapter_review",
            resource_id=review.id,
        )
        return _safe_review(review, evaluation_gate_status=run.gate_status)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_reviews(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    limit: int,
    cursor: str | None,
) -> GovernancePage:
    _safe_uuid(adapter_id, label="adapter")
    _validate_page(limit, cursor)
    decoded = _decode_cursor(cursor)
    try:
        authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_READ_ROLES,
            lock=False,
            audit_action="adapter.review.list.authorization",
        )
        filters = [
            AdapterReview.department_id == scope.department.value,
            AdapterReview.adapter_id == adapter_id,
        ]
        if decoded:
            created_at, row_id = decoded
            filters.append(
                or_(
                    AdapterReview.created_at < created_at,
                    and_(AdapterReview.created_at == created_at, AdapterReview.id > row_id),
                )
            )
        rows = tuple(
            session.scalars(
                select(AdapterReview)
                .where(*filters)
                .order_by(AdapterReview.created_at.desc(), AdapterReview.id)
                .limit(limit + 1)
            )
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        values = []
        for row in rows:
            gate = session.scalar(
                select(AdapterEvaluationRun.gate_status).where(
                    AdapterEvaluationRun.id == row.evaluation_id,
                    AdapterEvaluationRun.department_id == row.department_id,
                )
            )
            values.append(_safe_review(row, evaluation_gate_status=gate))
        return GovernancePage(
            tuple(values),
            limit,
            _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more else None,
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_review(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    review_id: UUID,
) -> dict[str, object]:
    _safe_uuid(adapter_id, label="adapter")
    _safe_uuid(review_id, label="review")
    try:
        authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_READ_ROLES,
            lock=False,
            audit_action="adapter.review.read.authorization",
        )
        row = session.scalar(
            select(AdapterReview).where(
                AdapterReview.id == review_id,
                AdapterReview.department_id == scope.department.value,
                AdapterReview.adapter_id == adapter_id,
            )
        )
        if row is None:
            raise ServiceError(404, "Adapter review not found")
        gate = session.scalar(
            select(AdapterEvaluationRun.gate_status).where(
                AdapterEvaluationRun.id == row.evaluation_id,
                AdapterEvaluationRun.department_id == row.department_id,
            )
        )
        return _safe_review(row, evaluation_gate_status=gate)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _current_deployment(
    session: Session, department_id: UUID, *, lock: bool = False
) -> DepartmentAdapterDeployment | None:
    query = select(DepartmentAdapterDeployment).where(
        DepartmentAdapterDeployment.department_id == department_id
    )
    if lock:
        query = query.with_for_update()
    return session.scalar(query)


def _validate_approved_target(
    session: Session,
    department_id: UUID,
    adapter_id: UUID,
    adapter_version: int,
    review_id: UUID,
    review_version: int,
) -> tuple[
    Adapter,
    AdapterReview,
    AdapterEvaluationRun,
    EvaluationSuite,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    tuple[AdapterEvaluationEvidence, AdapterEvaluationEvidence],
]:
    adapter, review = session.execute(
        select(Adapter, AdapterReview)
        .join(
            AdapterReview,
            and_(
                AdapterReview.adapter_id == Adapter.id,
                AdapterReview.department_id == Adapter.department_id,
            ),
        )
        .where(
            Adapter.id == adapter_id,
            Adapter.department_id == department_id,
            AdapterReview.id == review_id,
        )
        .with_for_update()
    ).one_or_none() or (None, None)
    if adapter is None or review is None:
        raise ServiceError(404, "Adapter review not found")
    if (
        review.version != review_version
        or adapter.version != adapter_version
        or review.status != "approved"
        or review.archived_at is not None
    ):
        raise ServiceError(409, "Approved review authority changed")
    authority = _load_evaluation_authority(
        session, department_id, adapter_id, review.evaluation_id, lock=True
    )
    _validate_approval(*authority)
    if not _review_snapshot_matches(review, *authority):
        raise ServiceError(409, "Approved review authority changed")
    if _active_purge(session, department_id, adapter_id):
        raise ServiceError(409, "Adapter purge is active")
    return (authority[0], review, *authority[1:])


def _operation_from_target(
    *,
    auth_id: UUID,
    department_id: UUID,
    operation_type: str,
    expected_version: int,
    current: DepartmentAdapterDeployment | None,
    adapter: Adapter | None,
    review: AdapterReview | None,
    run: AdapterEvaluationRun | None,
    dependency: AdapterUpstreamDependency | None,
    retention: AdapterRollbackRetention | None,
) -> AdapterDeploymentOperation:
    return AdapterDeploymentOperation(
        id=uuid4(),
        department_id=department_id,
        requested_by_user_id=auth_id,
        operation_type=operation_type,
        status="queued",
        expected_deployment_version=expected_version,
        target_adapter_id=adapter.id if adapter is not None else None,
        target_adapter_version=adapter.version if adapter is not None else None,
        target_review_id=review.id if review is not None else None,
        target_review_version=review.version if review is not None else None,
        target_evaluation_id=run.id if run is not None else None,
        target_evaluation_version=run.version if run is not None else None,
        target_retention_id=retention.id if retention is not None else None,
        target_retention_version=retention.version if retention is not None else None,
        current_target_kind=current.target_kind if current is not None else "base",
        current_adapter_id=current.adapter_id if current is not None else None,
        current_adapter_version=current.adapter_version if current is not None else None,
        current_deployment_version=current.deployment_version if current is not None else 0,
        base_model_id=_BASE_MODEL_ID,
        base_model_revision=_BASE_MODEL_REVISION,
        registry_attempt_id=run.registry_attempt_id if run is not None else None,
        registry_attempt_version=run.registry_attempt_version if run is not None else None,
        registry_publication_attempt_id=run.registry_publication_attempt_id
        if run is not None
        else None,
        registry_attempt_number=run.registry_attempt_number if run is not None else None,
        registry_execution_scope_id=adapter.execution_scope_id if adapter is not None else None,
        registry_manifest_sha256=adapter.registry_manifest_sha256 if adapter is not None else None,
        registry_adapter_config_sha256=adapter.registry_adapter_config_sha256
        if adapter is not None
        else None,
        registry_adapter_config_byte_size=adapter.registry_adapter_config_byte_size
        if adapter is not None
        else None,
        registry_adapter_model_sha256=adapter.registry_adapter_model_sha256
        if adapter is not None
        else None,
        registry_adapter_model_byte_size=adapter.registry_adapter_model_byte_size
        if adapter is not None
        else None,
        dependency_id=dependency.id if dependency is not None else None,
        dependency_version=dependency.version if dependency is not None else None,
        suite_id=run.suite_id if run is not None else None,
        suite_version=run.suite_version if run is not None else None,
        suite_artifact_manifest_sha256=run.suite_artifact_manifest_sha256
        if run is not None
        else None,
        suite_canonical_cases_sha256=run.suite_canonical_cases_sha256 if run is not None else None,
        suite_canonical_cases_byte_size=run.suite_canonical_cases_byte_size
        if run is not None
        else None,
        result_manifest_sha256=run.result_manifest_sha256 if run is not None else None,
        result_summary_sha256=run.result_summary_sha256 if run is not None else None,
        case_results_sha256=run.case_results_sha256 if run is not None else None,
        case_results_byte_size=run.case_results_byte_size if run is not None else None,
        runner_contract_version=run.runner_contract_version if run is not None else None,
        artifact_contract_version=run.artifact_contract_version if run is not None else None,
        metric_contract_version=run.metric_contract_version if run is not None else None,
        gate_policy_version=run.gate_policy_version if run is not None else None,
        seed_policy_version=run.seed_policy_version if run is not None else None,
        code_revision=run.code_revision if run is not None else None,
        attempt_number=1,
        version=1,
    )


def enqueue_promotion(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    review_id: UUID,
    expected_adapter_version: int,
    expected_review_version: int,
    expected_deployment_version: int,
) -> dict[str, object]:
    _safe_uuid(adapter_id, label="adapter")
    _safe_uuid(review_id, label="review")
    _safe_version(expected_adapter_version, label="adapter")
    _safe_version(expected_review_version, label="review")
    _safe_version(expected_deployment_version, label="deployment", allow_zero=True)
    try:
        auth = authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_ADMIN_ROLES,
            lock=True,
            audit_action="adapter.deployment.promote.authorization",
        )
        current = _current_deployment(session, scope.department.value, lock=True)
        current_version = current.deployment_version if current is not None else 0
        if current_version != expected_deployment_version:
            raise ServiceError(409, "Deployment version conflict")
        if _active_operation(session, scope.department.value) is not None:
            raise ServiceError(409, "Deployment operation is already active")
        authority = _validate_approved_target(
            session,
            scope.department.value,
            adapter_id,
            expected_adapter_version,
            review_id,
            expected_review_version,
        )
        adapter, review, run, _suite, _registry, dependency, _evidence = authority
        if (
            current is not None
            and current.target_kind == "adapter"
            and current.adapter_id == adapter.id
            and current.adapter_version == adapter.version
        ):
            raise ServiceError(409, "Adapter is already deployed")
        operation = _operation_from_target(
            auth_id=auth.identity.id,
            department_id=scope.department.value,
            operation_type="promote",
            expected_version=expected_deployment_version,
            current=current,
            adapter=adapter,
            review=review,
            run=run,
            dependency=dependency,
            retention=None,
        )
        session.add(operation)
        session.flush()
        append_mutation_audit(
            session,
            actor=auth.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action="adapter.deployment.promote.enqueue",
            resource_type="adapter_deployment_operation",
            resource_id=operation.id,
        )
        return _safe_operation(operation)
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Deployment operation conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def enqueue_rollback(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    target: str,
    adapter_id: UUID | None,
    expected_adapter_version: int | None,
    retention_id: UUID | None,
    expected_retention_version: int | None,
    expected_deployment_version: int,
) -> dict[str, object]:
    if target not in {"base", "adapter"}:
        raise ServiceError(422, "Invalid rollback target")
    _safe_version(expected_deployment_version, label="deployment", allow_zero=True)
    try:
        auth = authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_ADMIN_ROLES,
            lock=True,
            audit_action="adapter.deployment.rollback.authorization",
        )
        current = _current_deployment(session, scope.department.value, lock=True)
        if current is None or current.deployment_version != expected_deployment_version:
            raise ServiceError(409, "Deployment version conflict")
        if _active_operation(session, scope.department.value) is not None:
            raise ServiceError(409, "Deployment operation is already active")
        if target == "base":
            if current.target_kind == "base":
                raise ServiceError(409, "Department is already on base")
            operation_type = "rollback_base"
            adapter = session.scalar(
                select(Adapter)
                .where(
                    Adapter.id == current.adapter_id,
                    Adapter.department_id == scope.department.value,
                )
                .with_for_update()
            )
            if adapter is None:
                raise ServiceError(409, "Current adapter is unavailable")
            operation = _operation_from_target(
                auth_id=auth.identity.id,
                department_id=scope.department.value,
                operation_type=operation_type,
                expected_version=expected_deployment_version,
                current=current,
                adapter=None,
                review=None,
                run=None,
                dependency=None,
                retention=None,
            )
        else:
            _safe_uuid(adapter_id, label="adapter")
            _safe_version(expected_adapter_version, label="adapter")
            _safe_uuid(retention_id, label="rollback retention")
            _safe_version(expected_retention_version, label="rollback retention")
            retention = session.scalar(
                select(AdapterRollbackRetention)
                .where(
                    AdapterRollbackRetention.id == retention_id,
                    AdapterRollbackRetention.department_id == scope.department.value,
                    AdapterRollbackRetention.adapter_id == adapter_id,
                )
                .with_for_update()
            )
            if (
                retention is None
                or retention.status != "active"
                or retention.version != expected_retention_version
                or retention.adapter_version != expected_adapter_version
            ):
                raise ServiceError(409, "Rollback target is unavailable")
            if (
                current.target_kind == "adapter"
                and current.adapter_id == adapter_id
                and current.adapter_version == expected_adapter_version
            ):
                raise ServiceError(409, "Adapter is already deployed")
            authority = _validate_approved_target(
                session,
                scope.department.value,
                adapter_id,
                expected_adapter_version,
                retention.approved_review_id,
                retention.review_version,
            )
            adapter, review, run, _suite, _registry, dependency, _evidence = authority
            if (
                retention.approved_review_id != review.id
                or retention.review_version != review.version
                or retention.evaluation_id != run.id
                or retention.evaluation_version != run.version
                or retention.suite_id != run.suite_id
            ):
                raise ServiceError(409, "Rollback target authority changed")
            operation = _operation_from_target(
                auth_id=auth.identity.id,
                department_id=scope.department.value,
                operation_type="rollback_adapter",
                expected_version=expected_deployment_version,
                current=current,
                adapter=adapter,
                review=review,
                run=run,
                dependency=dependency,
                retention=retention,
            )
        session.add(operation)
        session.flush()
        append_mutation_audit(
            session,
            actor=auth.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action="adapter.deployment.rollback.enqueue",
            resource_type="adapter_deployment_operation",
            resource_id=operation.id,
        )
        return _safe_operation(operation)
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Deployment operation conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_deployment(
    session: Session, principal: AuthenticatedPrincipal, scope: DepartmentRequestScope
) -> dict[str, object]:
    try:
        authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_READ_ROLES,
            lock=False,
            audit_action="adapter.deployment.read.authorization",
        )
        return _safe_deployment(
            _current_deployment(session, scope.department.value), scope.department.value
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_operations(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    limit: int,
    offset: int,
) -> tuple[dict[str, object], ...]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise ServiceError(422, "Invalid pagination")
    try:
        authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_READ_ROLES,
            lock=False,
            audit_action="adapter.deployment.operation.list.authorization",
        )
        rows = session.scalars(
            select(AdapterDeploymentOperation)
            .where(AdapterDeploymentOperation.department_id == scope.department.value)
            .order_by(AdapterDeploymentOperation.created_at.desc(), AdapterDeploymentOperation.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return tuple(_safe_operation(row) for row in rows)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_operation(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    operation_id: UUID,
) -> dict[str, object]:
    _safe_uuid(operation_id, label="operation")
    try:
        authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_READ_ROLES,
            lock=False,
            audit_action="adapter.deployment.operation.read.authorization",
        )
        row = session.scalar(
            select(AdapterDeploymentOperation).where(
                AdapterDeploymentOperation.id == operation_id,
                AdapterDeploymentOperation.department_id == scope.department.value,
            )
        )
        if row is None:
            raise ServiceError(404, "Deployment operation not found")
        return _safe_operation(row)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def cancel_operation(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    operation_id: UUID,
    expected_version: int,
) -> dict[str, object]:
    _safe_uuid(operation_id, label="operation")
    _safe_version(expected_version, label="operation")
    try:
        auth = authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_ADMIN_ROLES,
            lock=True,
            audit_action="adapter.deployment.operation.cancel.authorization",
        )
        row = session.scalar(
            select(AdapterDeploymentOperation)
            .where(
                AdapterDeploymentOperation.id == operation_id,
                AdapterDeploymentOperation.department_id == scope.department.value,
            )
            .with_for_update()
        )
        if row is None:
            raise ServiceError(404, "Deployment operation not found")
        if row.version != expected_version:
            raise ServiceError(409, "Deployment operation version conflict")
        if row.status not in {"queued", "running"}:
            raise ServiceError(409, "Deployment operation is terminal")
        now = session.scalar(select(func.clock_timestamp()))
        row.cancellation_requested_at = now
        row.version += 1
        if row.status == "queued":
            row.status = "cancelled"
            row.error_code = "cancelled"
            row.finished_at = now
        session.flush()
        append_mutation_audit(
            session,
            actor=auth.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action="adapter.deployment.operation.cancel",
            resource_type="adapter_deployment_operation",
            resource_id=row.id,
        )
        return _safe_operation(row)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_events(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    limit: int,
    offset: int,
) -> tuple[dict[str, object], ...]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise ServiceError(422, "Invalid pagination")
    try:
        authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_READ_ROLES,
            lock=False,
            audit_action="adapter.deployment.event.list.authorization",
        )
        rows = session.scalars(
            select(AdapterDeploymentEvent)
            .where(AdapterDeploymentEvent.department_id == scope.department.value)
            .order_by(AdapterDeploymentEvent.created_at.desc(), AdapterDeploymentEvent.id)
            .offset(offset)
            .limit(limit)
        ).all()
        return tuple(_safe_event(row) for row in rows)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def release_rollback_retention(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    retention_id: UUID,
    expected_adapter_version: int,
    expected_retention_version: int,
) -> dict[str, object]:
    _safe_uuid(adapter_id, label="adapter")
    _safe_uuid(retention_id, label="rollback retention")
    _safe_version(expected_adapter_version, label="adapter")
    _safe_version(expected_retention_version, label="rollback retention")
    try:
        auth = authorize_transaction(
            session,
            principal,
            scope,
            GOVERNANCE_ADMIN_ROLES,
            lock=True,
            audit_action="adapter.rollback_retention.release.authorization",
        )
        retention = session.scalar(
            select(AdapterRollbackRetention)
            .where(
                AdapterRollbackRetention.id == retention_id,
                AdapterRollbackRetention.department_id == scope.department.value,
                AdapterRollbackRetention.adapter_id == adapter_id,
            )
            .with_for_update()
        )
        adapter = session.scalar(
            select(Adapter)
            .where(Adapter.id == adapter_id, Adapter.department_id == scope.department.value)
            .with_for_update()
        )
        current = _current_deployment(session, scope.department.value, lock=True)
        if retention is None or adapter is None:
            raise ServiceError(404, "Rollback retention not found")
        if (
            retention.version != expected_retention_version
            or adapter.version != expected_adapter_version
        ):
            raise ServiceError(409, "Rollback retention authority changed")
        if retention.status != "active":
            raise ServiceError(409, "Rollback retention is already released")
        if (
            current is not None
            and current.target_kind == "adapter"
            and current.adapter_id == adapter_id
            and current.adapter_version == expected_adapter_version
        ):
            raise ServiceError(409, "Current deployment cannot release its retention")
        if (
            _active_operation(session, scope.department.value) is not None
            or _active_purge(session, scope.department.value, adapter_id)
            or _active_evaluation(session, scope.department.value, adapter_id)
        ):
            raise ServiceError(409, "Rollback retention is retained by an active operation")
        if (
            session.scalar(
                select(AdapterReview.id).where(
                    AdapterReview.department_id == scope.department.value,
                    AdapterReview.adapter_id == adapter_id,
                    AdapterReview.adapter_version == expected_adapter_version,
                    AdapterReview.status == "pending",
                )
            )
            is not None
        ):
            raise ServiceError(409, "Pending review retains adapter")
        current_run = None
        current_review = None
        if current is not None and current.target_kind == "adapter":
            current_run = session.scalar(
                select(AdapterEvaluationRun).where(
                    AdapterEvaluationRun.id == current.evaluation_id,
                    AdapterEvaluationRun.department_id == scope.department.value,
                    AdapterEvaluationRun.adapter_id == current.adapter_id,
                    AdapterEvaluationRun.adapter_version == current.adapter_version,
                )
            )
            if current_run is None or current_run.suite_id != current.suite_id:
                raise ServiceError(409, "Current deployment authority is unavailable")
            current_review = session.scalar(
                select(AdapterReview).where(
                    AdapterReview.id == current.review_id,
                    AdapterReview.department_id == scope.department.value,
                    AdapterReview.adapter_id == current.adapter_id,
                    AdapterReview.adapter_version == current.adapter_version,
                    AdapterReview.evaluation_id == current.evaluation_id,
                    AdapterReview.evaluation_version == current.evaluation_version,
                    AdapterReview.suite_id == current.suite_id,
                    AdapterReview.status == "approved",
                    AdapterReview.archived_at.is_(None),
                )
            )
            if current_review is None:
                raise ServiceError(409, "Current deployment review is unavailable")
        now = session.scalar(select(func.clock_timestamp()))
        before = current.deployment_version if current is not None else 0
        event = AdapterDeploymentEvent(
            id=uuid4(),
            department_id=scope.department.value,
            event_type="rollback_retention_release",
            deployment_version_before=before,
            deployment_version_after=before,
            from_target_kind=current.target_kind if current is not None else "base",
            from_adapter_id=current.adapter_id if current is not None else None,
            from_adapter_version=current.adapter_version if current is not None else None,
            to_target_kind=current.target_kind if current is not None else "base",
            to_adapter_id=current.adapter_id if current is not None else None,
            to_adapter_version=current.adapter_version if current is not None else None,
            approved_review_id=current_review.id if current_review is not None else None,
            approved_review_version=current_review.version if current_review is not None else None,
            evaluation_id=current_run.id if current_run is not None else None,
            evaluation_version=current_run.version if current_run is not None else None,
            suite_id=current_run.suite_id if current_run is not None else None,
            base_model_id=_BASE_MODEL_ID,
            base_model_revision=_BASE_MODEL_REVISION,
            rollback_retention_id=retention.id,
            actor_user_id=auth.identity.id,
        )
        session.add(event)
        session.flush()
        retention.status = "released"
        retention.release_reason = "manual_release"
        retention.release_event_id = event.id
        retention.released_at = now
        retention.version += 1
        append_mutation_audit(
            session,
            actor=auth.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action="adapter.rollback_retention.release",
            resource_type="adapter_rollback_retention",
            resource_id=retention.id,
        )
        return _safe_retention(retention)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


__all__ = [
    "GOVERNANCE_ADMIN_ROLES",
    "GOVERNANCE_READ_ROLES",
    "GovernancePage",
    "cancel_operation",
    "enqueue_promotion",
    "enqueue_rollback",
    "list_events",
    "list_operations",
    "list_reviews",
    "read_deployment",
    "read_operation",
    "read_review",
    "release_rollback_retention",
    "start_review",
    "transition_review",
]
