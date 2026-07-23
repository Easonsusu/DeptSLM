"""Department-scoped evaluation metadata APIs and queue admission."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope
from app.evaluation_domain import (
    RUNNER_CONTRACT_VERSION,
    EvaluationContractError,
    decode_evaluation_cursor,
    derive_base_seed,
    encode_evaluation_cursor,
    production_contract,
)
from app.evaluation_suites import EVALUATOR_ROLES
from app.models import EvaluationRun, EvaluationSuite
from app.services import (
    ServiceError,
    append_mutation_audit,
    authorize_transaction,
)

_CODE_REVISION = re.compile(r"^[0-9a-f]{40}$")
EvaluationResource = TypeVar("EvaluationResource", EvaluationSuite, EvaluationRun)


@dataclass(frozen=True, slots=True)
class EvaluationPage(Generic[EvaluationResource]):
    items: tuple[EvaluationResource, ...]
    next_cursor: str | None


def list_evaluation_suites(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    cursor: str | None,
) -> EvaluationPage[EvaluationSuite]:
    _validate_page(limit)
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            EVALUATOR_ROLES,
            lock=False,
            audit_action="evaluation.suite.list.authorization",
        )
        statement = select(EvaluationSuite).where(
            EvaluationSuite.department_id == request_scope.department.value
        )
        if cursor is not None:
            position = decode_evaluation_cursor(
                cursor,
                department_id=request_scope.department.value,
                resource="suite",
            )
            statement = statement.where(
                or_(
                    EvaluationSuite.created_at < position.created_at,
                    and_(
                        EvaluationSuite.created_at == position.created_at,
                        EvaluationSuite.id > position.resource_id,
                    ),
                )
            )
        rows = tuple(
            session.scalars(
                statement.order_by(EvaluationSuite.created_at.desc(), EvaluationSuite.id).limit(
                    limit + 1
                )
            )
        )
        items = rows[:limit]
        next_cursor = (
            encode_evaluation_cursor(
                department_id=request_scope.department.value,
                resource="suite",
                created_at=items[-1].created_at,
                resource_id=items[-1].id,
            )
            if len(rows) > limit
            else None
        )
        return EvaluationPage(items, next_cursor)
    except EvaluationContractError as error:
        raise ServiceError(422, "Invalid evaluation cursor") from error
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_evaluation_suite(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    suite_id: UUID,
) -> EvaluationSuite:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            EVALUATOR_ROLES,
            lock=False,
            audit_action="evaluation.suite.read.authorization",
        )
        suite = session.execute(
            select(EvaluationSuite).where(
                EvaluationSuite.id == suite_id,
                EvaluationSuite.department_id == request_scope.department.value,
            )
        ).scalar_one_or_none()
        if suite is None:
            raise ServiceError(404, "Evaluation suite not found")
        return suite
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def enqueue_evaluation_run(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    suite_id: UUID,
    *,
    code_revision: str | None,
) -> EvaluationRun:
    if code_revision is None or _CODE_REVISION.fullmatch(code_revision) is None:
        raise ServiceError(503, "Evaluation runner unavailable")
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            EVALUATOR_ROLES,
            lock=True,
            audit_action="evaluation.run.enqueue.authorization",
        )
        suite = session.execute(
            select(EvaluationSuite)
            .where(
                EvaluationSuite.id == suite_id,
                EvaluationSuite.department_id == request_scope.department.value,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if suite is None:
            raise ServiceError(404, "Evaluation suite not found")
        if suite.status != "active":
            raise ServiceError(409, "Evaluation suite is archived")
        run_id = uuid4()
        contract = production_contract()
        run = EvaluationRun(
            id=run_id,
            department_id=request_scope.department.value,
            suite_id=suite.id,
            requested_by_user_id=authorization.identity.id,
            status="queued",
            gate_status="pending",
            runner_contract_version=RUNNER_CONTRACT_VERSION,
            code_revision=code_revision,
            base_seed=derive_base_seed(run_id),
            case_count=suite.case_count,
            completed_case_count=0,
            answered_case_count=0,
            insufficient_case_count=0,
            **contract,
        )
        session.add(run)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="evaluation.run.enqueue",
            resource_type="evaluation_run",
            resource_id=run.id,
        )
        session.flush()
        return run
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Evaluation run conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_evaluation_runs(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    cursor: str | None,
) -> EvaluationPage[EvaluationRun]:
    _validate_page(limit)
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            EVALUATOR_ROLES,
            lock=False,
            audit_action="evaluation.run.list.authorization",
        )
        statement = select(EvaluationRun).where(
            EvaluationRun.department_id == request_scope.department.value
        )
        if cursor is not None:
            position = decode_evaluation_cursor(
                cursor,
                department_id=request_scope.department.value,
                resource="run",
            )
            statement = statement.where(
                or_(
                    EvaluationRun.created_at < position.created_at,
                    and_(
                        EvaluationRun.created_at == position.created_at,
                        EvaluationRun.id > position.resource_id,
                    ),
                )
            )
        rows = tuple(
            session.scalars(
                statement.order_by(EvaluationRun.created_at.desc(), EvaluationRun.id).limit(
                    limit + 1
                )
            )
        )
        items = rows[:limit]
        next_cursor = (
            encode_evaluation_cursor(
                department_id=request_scope.department.value,
                resource="run",
                created_at=items[-1].created_at,
                resource_id=items[-1].id,
            )
            if len(rows) > limit
            else None
        )
        return EvaluationPage(items, next_cursor)
    except EvaluationContractError as error:
        raise ServiceError(422, "Invalid evaluation cursor") from error
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_evaluation_run(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    run_id: UUID,
) -> EvaluationRun:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            EVALUATOR_ROLES,
            lock=False,
            audit_action="evaluation.run.read.authorization",
        )
        run = session.execute(
            select(EvaluationRun).where(
                EvaluationRun.id == run_id,
                EvaluationRun.department_id == request_scope.department.value,
            )
        ).scalar_one_or_none()
        if run is None:
            raise ServiceError(404, "Evaluation run not found")
        return run
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def cancel_evaluation_run(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    run_id: UUID,
    *,
    expected_version: int,
) -> EvaluationRun:
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ServiceError(409, "Evaluation run version conflict")
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            EVALUATOR_ROLES,
            lock=True,
            audit_action="evaluation.run.cancel.authorization",
        )
        run = session.execute(
            select(EvaluationRun)
            .where(
                EvaluationRun.id == run_id,
                EvaluationRun.department_id == request_scope.department.value,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if run is None:
            raise ServiceError(404, "Evaluation run not found")
        if run.version != expected_version:
            raise ServiceError(409, "Evaluation run version conflict")
        if run.status not in {"queued", "running"}:
            raise ServiceError(409, "Evaluation run is already terminal")
        now = session.scalar(select(_clock_timestamp()))
        run.cancellation_requested_at = now
        if run.status == "queued":
            run.status = "cancelled"
            run.error_code = "cancelled"
            run.finished_at = now
        run.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="evaluation.run.cancel",
            resource_type="evaluation_run",
            resource_id=run.id,
        )
        session.flush()
        return run
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _validate_page(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ServiceError(422, "Invalid pagination")


def _clock_timestamp():
    from sqlalchemy import func

    return func.clock_timestamp()
