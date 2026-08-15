"""Department-scoped Phase 12.2 adapter evaluation metadata boundaries."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.adapter_evaluation_domain import (
    ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION,
    ADAPTER_EVALUATION_GATE_POLICY_VERSION,
    ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION,
    ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION,
    ADAPTER_EVALUATION_SEED_POLICY_VERSION,
    derive_adapter_evaluation_base_seed,
)
from app.adapter_registry_domain import canonical_json_bytes
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.evaluation_suites import EVALUATOR_ROLES
from app.models import (
    Adapter,
    AdapterEvaluationEvidence,
    AdapterEvaluationRun,
    AdapterPurgeOperation,
    AdapterRegistryAttempt,
    AdapterUpstreamDependency,
    EvaluationSuite,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction

ADAPTER_EVALUATION_ADMIN_ROLES = frozenset(
    {DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN}
)
ADAPTER_EVALUATION_READ_ROLES = EVALUATOR_ROLES
_CODE_REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class AdapterEvaluationPage:
    items: tuple[dict[str, object], ...]
    limit: int
    next_cursor: str | None


def _validate_page(limit: object, cursor: object) -> None:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
        or cursor is not None
        and (not isinstance(cursor, str) or len(cursor) > 1024)
    ):
        raise ServiceError(422, "Invalid pagination")


def _encode_cursor(created_at: datetime, run_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "id": str(run_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, UUID] | None:
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {"created_at", "id"}:
            raise ValueError
        created_at = datetime.fromisoformat(value["created_at"])
        run_id = UUID(value["id"])
        if created_at.tzinfo is None or run_id.int == 0:
            raise ValueError
        return created_at, run_id
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ServiceError(422, "Invalid pagination") from None


def _safe_evidence(row: AdapterEvaluationEvidence) -> dict[str, object]:
    metrics = {
        name: getattr(row, name)
        for name in (
            "retrieval_recall_at_5",
            "retrieval_recall_at_10",
            "retrieval_recall_at_20",
            "retrieval_mrr_at_20",
            "answer_status_accuracy",
            "citation_precision",
            "citation_recall",
            "normalized_exact_match",
            "character_f1",
            "invalid_contract_rate",
        )
    }
    deltas = None
    if row.target == "candidate":
        deltas = {name: getattr(row, "delta_" + name) for name in metrics}
    return {
        "target": row.target,
        "gate_status": row.gate_status,
        "failed_gate_count": row.failed_gate_count,
        **metrics,
        "deltas": deltas,
    }


def _safe_run(
    run: AdapterEvaluationRun, evidence: tuple[AdapterEvaluationEvidence, ...]
) -> dict[str, object]:
    return {
        "id": run.id,
        "department_id": run.department_id,
        "adapter_id": run.adapter_id,
        "suite_id": run.suite_id,
        "status": run.status,
        "gate_status": run.gate_status,
        "error_code": run.error_code,
        "expected_adapter_version": run.expected_adapter_version,
        "adapter_version": run.adapter_version,
        "base_model_id": run.base_model_id,
        "base_model_revision": run.base_model_revision,
        "runner_contract_version": run.runner_contract_version,
        "metric_contract_version": run.metric_contract_version,
        "gate_policy_version": run.gate_policy_version,
        "seed_policy_version": run.seed_policy_version,
        "case_count": run.case_count,
        "completed_case_count": run.completed_case_count,
        "evidence": [_safe_evidence(item) for item in evidence],
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "cancelled_at": run.cancelled_at,
        "version": run.version,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _read_evidence(
    session: Session, run: AdapterEvaluationRun
) -> tuple[AdapterEvaluationEvidence, ...]:
    return tuple(
        session.scalars(
            select(AdapterEvaluationEvidence)
            .where(
                AdapterEvaluationEvidence.run_id == run.id,
                AdapterEvaluationEvidence.department_id == run.department_id,
                AdapterEvaluationEvidence.adapter_id == run.adapter_id,
                AdapterEvaluationEvidence.suite_id == run.suite_id,
            )
            .order_by(AdapterEvaluationEvidence.target)
        )
    )


def _authoritative_registry_attempt(
    session: Session, adapter: Adapter
) -> AdapterRegistryAttempt | None:
    return session.execute(
        select(AdapterRegistryAttempt)
        .where(
            AdapterRegistryAttempt.department_id == adapter.department_id,
            AdapterRegistryAttempt.adapter_id == adapter.id,
            AdapterRegistryAttempt.execution_scope_id == adapter.execution_scope_id,
            AdapterRegistryAttempt.publication_attempt_id == adapter.publication_attempt_id,
            AdapterRegistryAttempt.attempt_number == adapter.attempt_number,
            AdapterRegistryAttempt.status == "succeeded",
        )
        .with_for_update()
    ).scalar_one_or_none()


def _active_purge(session: Session, department_id: UUID, adapter_id: UUID) -> bool:
    return (
        session.execute(
            select(AdapterPurgeOperation.id).where(
                AdapterPurgeOperation.department_id == department_id,
                AdapterPurgeOperation.adapter_id == adapter_id,
                AdapterPurgeOperation.status.in_(("registered", "deleting")),
            )
        ).scalar_one_or_none()
        is not None
    )


def _check_eligibility(
    session: Session,
    *,
    department_id: UUID,
    adapter_id: UUID,
    suite_id: UUID,
    expected_adapter_version: int,
) -> tuple[Adapter, AdapterRegistryAttempt, EvaluationSuite, AdapterUpstreamDependency]:
    adapter = session.execute(
        select(Adapter)
        .where(
            Adapter.id == adapter_id,
            Adapter.department_id == department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if adapter is None:
        raise ServiceError(404, "Adapter not found")
    suite = session.execute(
        select(EvaluationSuite)
        .where(
            EvaluationSuite.id == suite_id,
            EvaluationSuite.department_id == department_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if suite is None:
        raise ServiceError(404, "Evaluation suite not found")
    if suite.status != "active":
        raise ServiceError(409, "Evaluation suite is archived")
    if _active_purge(session, department_id, adapter_id):
        raise ServiceError(409, "Adapter purge is active")
    if (
        adapter.status != "validated"
        or adapter.purged_at is not None
        or adapter.worker_id is not None
        or adapter.claim_token is not None
        or adapter.lease_expires_at is not None
        or adapter.version != expected_adapter_version
        or not adapter.verified_governance_lineage
        or not adapter.verified_artifact_compatibility
        or adapter.training_provenance_verified is not False
        or adapter.base_model_id != "Qwen/Qwen3-0.6B"
        or adapter.base_model_revision != "c1899de289a04d12100db370d81485cdf75e47ca"
        or adapter.registry_manifest_sha256 is None
        or adapter.registry_adapter_config_sha256 is None
        or adapter.registry_adapter_config_byte_size is None
        or adapter.registry_adapter_model_sha256 is None
        or adapter.registry_adapter_model_byte_size is None
    ):
        raise ServiceError(409, "Adapter is not eligible for evaluation")
    dependency = session.execute(
        select(AdapterUpstreamDependency)
        .where(
            AdapterUpstreamDependency.adapter_id == adapter.id,
            AdapterUpstreamDependency.department_id == department_id,
            AdapterUpstreamDependency.status == "active",
        )
        .with_for_update()
    ).scalar_one_or_none()
    registry_attempt = _authoritative_registry_attempt(session, adapter)
    if (
        dependency is None
        or registry_attempt is None
        or registry_attempt.ownership_manifest is None
    ):
        raise ServiceError(409, "Adapter authority is unavailable")
    manifest = registry_attempt.ownership_manifest
    files = manifest.get("files") if isinstance(manifest, dict) else None
    config_file = files.get("adapter_config.json") if isinstance(files, dict) else None
    model_file = files.get("adapter_model.safetensors") if isinstance(files, dict) else None
    try:
        manifest_digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
    except (TypeError, ValueError):
        manifest_digest = ""
    if (
        not isinstance(manifest, dict)
        or manifest_digest != adapter.registry_manifest_sha256
        or manifest.get("department_id") != str(department_id)
        or manifest.get("adapter_id") != str(adapter.id)
        or manifest.get("publication_attempt_id") != str(adapter.publication_attempt_id)
        or manifest.get("attempt_number") != adapter.attempt_number
        or not isinstance(config_file, dict)
        or not isinstance(model_file, dict)
        or config_file.get("sha256") != adapter.registry_adapter_config_sha256
        or config_file.get("byte_size") != adapter.registry_adapter_config_byte_size
        or model_file.get("sha256") != adapter.registry_adapter_model_sha256
        or model_file.get("byte_size") != adapter.registry_adapter_model_byte_size
    ):
        raise ServiceError(409, "Adapter authority is unavailable")
    if (
        suite.suite_contract_version != "phase9-evaluation-suite-v1"
        or suite.artifact_contract_version != "phase9-evaluation-artifact-v1"
        or suite.metric_contract_version != ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION
        or suite.gate_policy_version != ADAPTER_EVALUATION_GATE_POLICY_VERSION
    ):
        raise ServiceError(409, "Evaluation suite authority is unavailable")
    return adapter, registry_attempt, suite, dependency


def enqueue_adapter_evaluation(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    suite_id: UUID,
    expected_adapter_version: int,
    code_revision: str | None,
) -> dict[str, object]:
    if (
        isinstance(expected_adapter_version, bool)
        or not isinstance(expected_adapter_version, int)
        or expected_adapter_version < 1
        or code_revision is None
        or _CODE_REVISION.fullmatch(code_revision) is None
    ):
        raise ServiceError(503, "Adapter evaluation unavailable")
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ADAPTER_EVALUATION_ADMIN_ROLES,
            lock=True,
            audit_action="adapter.evaluation.enqueue.authorization",
        )
        adapter, registry_attempt, suite, dependency = _check_eligibility(
            session,
            department_id=request_scope.department.value,
            adapter_id=adapter_id,
            suite_id=suite_id,
            expected_adapter_version=expected_adapter_version,
        )
        run = AdapterEvaluationRun(
            id=uuid4(),
            department_id=request_scope.department.value,
            adapter_id=adapter.id,
            suite_id=suite.id,
            requested_by_user_id=authorization.identity.id,
            status="queued",
            gate_status="pending",
            expected_adapter_version=expected_adapter_version,
            adapter_version=adapter.version,
            registry_attempt_id=registry_attempt.id,
            registry_attempt_version=registry_attempt.version,
            registry_publication_attempt_id=registry_attempt.publication_attempt_id,
            registry_attempt_number=registry_attempt.attempt_number,
            registry_manifest_sha256=adapter.registry_manifest_sha256,
            registry_adapter_config_sha256=adapter.registry_adapter_config_sha256,
            registry_adapter_config_byte_size=adapter.registry_adapter_config_byte_size,
            registry_adapter_model_sha256=adapter.registry_adapter_model_sha256,
            registry_adapter_model_byte_size=adapter.registry_adapter_model_byte_size,
            dependency_id=dependency.id,
            dependency_version=dependency.version,
            suite_version=suite.version,
            suite_artifact_manifest_sha256=suite.artifact_manifest_sha256,
            suite_canonical_cases_sha256=suite.canonical_cases_sha256,
            suite_canonical_cases_byte_size=suite.canonical_cases_byte_size,
            retrieval_recall_at_5_min=suite.retrieval_recall_at_5_min,
            retrieval_mrr_at_20_min=suite.retrieval_mrr_at_20_min,
            answer_status_accuracy_min=suite.answer_status_accuracy_min,
            citation_precision_min=suite.citation_precision_min,
            citation_recall_min=suite.citation_recall_min,
            normalized_exact_match_min=suite.normalized_exact_match_min,
            character_f1_min=suite.character_f1_min,
            invalid_contract_rate_max=suite.invalid_contract_rate_max,
            base_model_id=adapter.base_model_id,
            base_model_revision=adapter.base_model_revision,
            runner_contract_version=ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION,
            artifact_contract_version=ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION,
            metric_contract_version=ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION,
            gate_policy_version=ADAPTER_EVALUATION_GATE_POLICY_VERSION,
            seed_policy_version=ADAPTER_EVALUATION_SEED_POLICY_VERSION,
            code_revision=code_revision,
            base_seed=derive_adapter_evaluation_base_seed(
                request_scope.department.value, adapter.id, adapter.version, suite.id
            ),
            case_count=suite.case_count,
            completed_case_count=0,
        )
        session.add(run)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="adapter.evaluation.enqueue",
            resource_type="adapter_evaluation",
            resource_id=run.id,
        )
        return _safe_run(run, ())
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Adapter evaluation conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_adapter_evaluations(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    limit: int,
    cursor: str | None,
) -> AdapterEvaluationPage:
    _validate_page(limit, cursor)
    decoded = _decode_cursor(cursor)
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ADAPTER_EVALUATION_READ_ROLES,
            lock=False,
            audit_action="adapter.evaluation.list.authorization",
        )
        filters = [
            AdapterEvaluationRun.department_id == request_scope.department.value,
            AdapterEvaluationRun.adapter_id == adapter_id,
        ]
        if decoded is not None:
            created_at, run_id = decoded
            filters.append(
                or_(
                    AdapterEvaluationRun.created_at < created_at,
                    and_(
                        AdapterEvaluationRun.created_at == created_at,
                        AdapterEvaluationRun.id > run_id,
                    ),
                )
            )
        rows = tuple(
            session.scalars(
                select(AdapterEvaluationRun)
                .where(*filters)
                .order_by(AdapterEvaluationRun.created_at.desc(), AdapterEvaluationRun.id)
                .limit(limit + 1)
            )
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more else None
        return AdapterEvaluationPage(
            tuple(_safe_run(row, _read_evidence(session, row)) for row in rows),
            limit,
            next_cursor,
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_adapter_evaluation(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    evaluation_id: UUID,
) -> dict[str, object]:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ADAPTER_EVALUATION_READ_ROLES,
            lock=False,
            audit_action="adapter.evaluation.read.authorization",
        )
        run = session.execute(
            select(AdapterEvaluationRun).where(
                AdapterEvaluationRun.id == evaluation_id,
                AdapterEvaluationRun.department_id == request_scope.department.value,
                AdapterEvaluationRun.adapter_id == adapter_id,
            )
        ).scalar_one_or_none()
        if run is None:
            raise ServiceError(404, "Adapter evaluation not found")
        return _safe_run(run, _read_evidence(session, run))
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def cancel_adapter_evaluation(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    adapter_id: UUID,
    evaluation_id: UUID,
    expected_version: int,
) -> dict[str, object]:
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ServiceError(409, "Adapter evaluation version conflict")
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ADAPTER_EVALUATION_ADMIN_ROLES,
            lock=True,
            audit_action="adapter.evaluation.cancel.authorization",
        )
        run = session.execute(
            select(AdapterEvaluationRun)
            .where(
                AdapterEvaluationRun.id == evaluation_id,
                AdapterEvaluationRun.department_id == request_scope.department.value,
                AdapterEvaluationRun.adapter_id == adapter_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if run is None:
            raise ServiceError(404, "Adapter evaluation not found")
        if run.version != expected_version:
            raise ServiceError(409, "Adapter evaluation version conflict")
        if run.status not in {"queued", "running"}:
            raise ServiceError(409, "Adapter evaluation is already terminal")
        now = session.scalar(select(__import__("sqlalchemy").func.clock_timestamp()))
        run.cancellation_requested_at = now
        if run.status == "queued":
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.finished_at = now
            run.cancelled_at = now
        run.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="adapter.evaluation.cancel",
            resource_type="adapter_evaluation",
            resource_id=run.id,
        )
        return _safe_run(run, _read_evidence(session, run))
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


__all__ = [
    "ADAPTER_EVALUATION_ADMIN_ROLES",
    "ADAPTER_EVALUATION_READ_ROLES",
    "AdapterEvaluationPage",
    "cancel_adapter_evaluation",
    "enqueue_adapter_evaluation",
    "list_adapter_evaluations",
    "read_adapter_evaluation",
]
