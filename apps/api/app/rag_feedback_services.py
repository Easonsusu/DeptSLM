"""PostgreSQL-only Phase 8 feedback, review, retention, and purge services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import String, and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by, array
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.models import (
    RagAnswerCitation,
    RagAnswerFeedback,
    RagAnswerFeedbackReason,
    RagAnswerFeedbackSourceTarget,
    RagAnswerRun,
)
from app.rag_feedback_domain import (
    CanonicalFeedback,
    FeedbackContractError,
    FeedbackSentiment,
    FeedbackStatus,
    canonicalize_feedback,
    decode_feedback_cursor,
    encode_feedback_cursor,
    validate_review_transition,
)
from app.schemas import RagFeedbackResponse
from app.services import ALL_ROLES, ServiceError, append_mutation_audit, authorize_transaction

REVIEWER_ROLES = frozenset(
    {
        DepartmentRole.SYSTEM_ADMIN,
        DepartmentRole.DEPARTMENT_ADMIN,
        DepartmentRole.INSTRUCTOR,
    }
)
PURGE_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
MAX_REVIEW_LIST_LIMIT = 100
MAX_PURGE_LIMIT = 1000


@dataclass(frozen=True, slots=True)
class FeedbackSubmissionResult:
    response: RagFeedbackResponse
    created: bool


@dataclass(frozen=True, slots=True)
class FeedbackPage:
    items: tuple[RagFeedbackResponse, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class PurgeResult:
    department_id: UUID
    eligible_count: int
    oldest_expires_at: datetime | None
    newest_expires_at: datetime | None
    purged_count: int
    applied: bool


def _server_now(session: Session):
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:
        raise ServiceError(503, "Database unavailable")
    return value


def _service_call(operation):
    try:
        return operation()
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Feedback conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _validate_limit(value: object, *, maximum: int, detail: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ServiceError(422, detail)
    return value


def _canonical_or_error(
    *,
    answer_status: str,
    sentiment: FeedbackSentiment,
    reason_codes: list[str],
    source_ids: list[str],
    available_source_ids: tuple[str, ...],
) -> CanonicalFeedback:
    try:
        return canonicalize_feedback(
            answer_status=answer_status,
            sentiment=sentiment,
            reason_codes=reason_codes,
            source_ids=source_ids,
            available_source_ids=available_source_ids,
        )
    except FeedbackContractError as error:
        if error.code == "run_unavailable":
            raise ServiceError(409, "Feedback unavailable for this run") from error
        raise ServiceError(422, "Invalid feedback selection") from error


def _citation_map(session: Session, *, department_id: UUID, run_id: UUID):
    rows = session.execute(
        select(RagAnswerCitation)
        .where(
            RagAnswerCitation.department_id == department_id,
            RagAnswerCitation.run_id == run_id,
        )
        .order_by(RagAnswerCitation.rank, RagAnswerCitation.id)
    ).scalars()
    return {row.source_label: row for row in rows}


def _stored_contract(session: Session, feedback: RagAnswerFeedback) -> CanonicalFeedback:
    reasons = tuple(
        session.scalars(
            select(RagAnswerFeedbackReason.reason_code)
            .where(
                RagAnswerFeedbackReason.feedback_id == feedback.id,
                RagAnswerFeedbackReason.department_id == feedback.department_id,
                RagAnswerFeedbackReason.run_id == feedback.run_id,
            )
            .order_by(RagAnswerFeedbackReason.rank)
        )
    )
    source_ids = tuple(
        row[0]
        for row in session.execute(
            select(RagAnswerCitation.source_label)
            .join(
                RagAnswerFeedbackSourceTarget,
                and_(
                    RagAnswerFeedbackSourceTarget.citation_id == RagAnswerCitation.id,
                    RagAnswerFeedbackSourceTarget.department_id == RagAnswerCitation.department_id,
                    RagAnswerFeedbackSourceTarget.run_id == RagAnswerCitation.run_id,
                ),
            )
            .where(
                RagAnswerFeedbackSourceTarget.feedback_id == feedback.id,
                RagAnswerFeedbackSourceTarget.department_id == feedback.department_id,
                RagAnswerFeedbackSourceTarget.run_id == feedback.run_id,
            )
            .order_by(RagAnswerFeedbackSourceTarget.rank)
        )
    )
    return CanonicalFeedback(feedback.sentiment, reasons, source_ids)


def _feedback_snapshot_statement():
    reason_codes = (
        select(
            func.coalesce(
                func.array_agg(
                    aggregate_order_by(
                        RagAnswerFeedbackReason.reason_code,
                        RagAnswerFeedbackReason.rank,
                    )
                ),
                array([], type_=String),
            )
        )
        .where(
            RagAnswerFeedbackReason.feedback_id == RagAnswerFeedback.id,
            RagAnswerFeedbackReason.department_id == RagAnswerFeedback.department_id,
            RagAnswerFeedbackReason.run_id == RagAnswerFeedback.run_id,
        )
        .correlate(RagAnswerFeedback)
        .scalar_subquery()
        .label("reason_codes")
    )
    source_ids = (
        select(
            func.coalesce(
                func.array_agg(
                    aggregate_order_by(
                        RagAnswerCitation.source_label,
                        RagAnswerFeedbackSourceTarget.rank,
                    )
                ),
                array([], type_=String),
            )
        )
        .select_from(RagAnswerFeedbackSourceTarget)
        .join(
            RagAnswerCitation,
            and_(
                RagAnswerCitation.id == RagAnswerFeedbackSourceTarget.citation_id,
                RagAnswerCitation.department_id == RagAnswerFeedbackSourceTarget.department_id,
                RagAnswerCitation.run_id == RagAnswerFeedbackSourceTarget.run_id,
            ),
        )
        .where(
            RagAnswerFeedbackSourceTarget.feedback_id == RagAnswerFeedback.id,
            RagAnswerFeedbackSourceTarget.department_id == RagAnswerFeedback.department_id,
            RagAnswerFeedbackSourceTarget.run_id == RagAnswerFeedback.run_id,
        )
        .correlate(RagAnswerFeedback)
        .scalar_subquery()
        .label("source_ids")
    )
    return select(
        RagAnswerFeedback,
        RagAnswerRun.status.label("answer_status"),
        reason_codes,
        source_ids,
    ).join(
        RagAnswerRun,
        and_(
            RagAnswerRun.id == RagAnswerFeedback.run_id,
            RagAnswerRun.department_id == RagAnswerFeedback.department_id,
        ),
    )


def _snapshot_response(
    feedback: RagAnswerFeedback,
    *,
    answer_status: str,
    reason_codes: list[str],
    source_ids: list[str],
) -> RagFeedbackResponse:
    reasons = tuple(reason_codes)
    sources = tuple(source_ids)
    try:
        canonical = canonicalize_feedback(
            answer_status=answer_status,
            sentiment=FeedbackSentiment(feedback.sentiment),
            reason_codes=reasons,
            source_ids=sources,
            available_source_ids=sources,
        )
    except (FeedbackContractError, ValueError):
        raise ServiceError(404, "Feedback not found") from None
    if canonical.reason_codes != reasons or canonical.source_ids != sources:
        raise ServiceError(404, "Feedback not found")
    return RagFeedbackResponse(
        id=feedback.id,
        run_id=feedback.run_id,
        answer_status=answer_status,
        sentiment=feedback.sentiment,
        reason_codes=list(reasons),
        source_ids=list(sources),
        status=feedback.status,
        resolution_code=feedback.resolution_code,
        created_at=feedback.created_at,
        reviewed_at=feedback.reviewed_at,
        expires_at=feedback.expires_at,
        version=feedback.version,
    )


def _response(
    session: Session, feedback: RagAnswerFeedback, *, answer_status: str | None = None
) -> RagFeedbackResponse:
    if answer_status is None:
        answer_status = session.scalar(
            select(RagAnswerRun.status).where(
                RagAnswerRun.id == feedback.run_id,
                RagAnswerRun.department_id == feedback.department_id,
            )
        )
    if answer_status not in {"answered", "insufficient_information"}:
        raise ServiceError(404, "Feedback not found")
    contract = _stored_contract(session, feedback)
    return RagFeedbackResponse(
        id=feedback.id,
        run_id=feedback.run_id,
        answer_status=answer_status,
        sentiment=feedback.sentiment,
        reason_codes=list(contract.reason_codes),
        source_ids=list(contract.source_ids),
        status=feedback.status,
        resolution_code=feedback.resolution_code,
        created_at=feedback.created_at,
        reviewed_at=feedback.reviewed_at,
        expires_at=feedback.expires_at,
        version=feedback.version,
    )


def submit_feedback(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    run_id: UUID,
    *,
    sentiment: FeedbackSentiment,
    reason_codes: list[str],
    source_ids: list[str],
    retention_days: int,
) -> FeedbackSubmissionResult:
    """Create once or replay an identical canonical PUT without mutation."""

    def operation() -> FeedbackSubmissionResult:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=True,
            audit_action="rag.feedback.submit.authorization",
        )
        run = session.execute(
            select(RagAnswerRun)
            .where(
                RagAnswerRun.id == run_id,
                RagAnswerRun.department_id == request_scope.department.value,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if run is None or run.requested_by_user_id != authorization.identity.id:
            raise ServiceError(404, "Feedback not found")
        if run.status not in {"answered", "insufficient_information"}:
            raise ServiceError(409, "Feedback unavailable for this run")
        now = _server_now(session)
        citations = _citation_map(
            session, department_id=request_scope.department.value, run_id=run.id
        )
        canonical = _canonical_or_error(
            answer_status=run.status,
            sentiment=sentiment,
            reason_codes=reason_codes,
            source_ids=source_ids,
            available_source_ids=tuple(citations),
        )
        existing = session.execute(
            select(RagAnswerFeedback)
            .where(
                RagAnswerFeedback.department_id == request_scope.department.value,
                RagAnswerFeedback.run_id == run.id,
                RagAnswerFeedback.submitted_by_user_id == authorization.identity.id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            if existing.expires_at <= now:
                raise ServiceError(404, "Feedback not found")
            if _stored_contract(session, existing) != canonical:
                raise ServiceError(409, "Feedback is immutable")
            return FeedbackSubmissionResult(
                _response(session, existing, answer_status=run.status), False
            )

        feedback = RagAnswerFeedback(
            department_id=request_scope.department.value,
            run_id=run.id,
            submitted_by_user_id=authorization.identity.id,
            sentiment=canonical.sentiment,
            status="open",
            expires_at=now + timedelta(days=retention_days),
            created_at=now,
            updated_at=now,
            version=1,
        )
        session.add(feedback)
        session.flush()
        for rank, reason_code in enumerate(canonical.reason_codes, 1):
            session.add(
                RagAnswerFeedbackReason(
                    feedback_id=feedback.id,
                    department_id=feedback.department_id,
                    run_id=feedback.run_id,
                    rank=rank,
                    reason_code=reason_code,
                    created_at=now,
                )
            )
        for rank, source_id in enumerate(canonical.source_ids, 1):
            session.add(
                RagAnswerFeedbackSourceTarget(
                    feedback_id=feedback.id,
                    department_id=feedback.department_id,
                    run_id=feedback.run_id,
                    citation_id=citations[source_id].id,
                    rank=rank,
                    created_at=now,
                )
            )
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="rag.feedback.submit",
            resource_type="rag_answer_feedback",
            resource_id=feedback.id,
        )
        session.flush()
        return FeedbackSubmissionResult(
            _response(session, feedback, answer_status=run.status), True
        )

    return _service_call(operation)


def read_own_feedback(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    run_id: UUID,
) -> RagFeedbackResponse:
    def operation() -> RagFeedbackResponse:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="rag.feedback.owner_read.authorization",
        )
        row = session.execute(
            _feedback_snapshot_statement().where(
                RagAnswerFeedback.department_id == request_scope.department.value,
                RagAnswerFeedback.run_id == run_id,
                RagAnswerFeedback.submitted_by_user_id == authorization.identity.id,
                RagAnswerRun.requested_by_user_id == authorization.identity.id,
                RagAnswerFeedback.expires_at > func.statement_timestamp(),
            )
        ).one_or_none()
        if row is None:
            raise ServiceError(404, "Feedback not found")
        return _snapshot_response(
            row[0], answer_status=row[1], reason_codes=row[2], source_ids=row[3]
        )

    return _service_call(operation)


def list_feedback_for_review(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    status: FeedbackStatus | None,
    sentiment: FeedbackSentiment | None,
    limit: int,
    cursor: str | None,
) -> FeedbackPage:
    limit = _validate_limit(limit, maximum=MAX_REVIEW_LIST_LIMIT, detail="Invalid feedback limit")

    def operation() -> FeedbackPage:
        authorize_transaction(
            session,
            principal,
            request_scope,
            REVIEWER_ROLES,
            lock=False,
            audit_action="rag.feedback.review_list.authorization",
        )
        status_value = status.value if status is not None else None
        sentiment_value = sentiment.value if sentiment is not None else None
        position = None
        if cursor is not None:
            try:
                position = decode_feedback_cursor(
                    cursor,
                    department_id=request_scope.department.value,
                    status=status_value,
                    sentiment=sentiment_value,
                )
            except FeedbackContractError as error:
                raise ServiceError(422, "Invalid feedback cursor") from error
        statement = _feedback_snapshot_statement().where(
            RagAnswerFeedback.department_id == request_scope.department.value,
            RagAnswerFeedback.expires_at > func.statement_timestamp(),
        )
        if status_value is not None:
            statement = statement.where(RagAnswerFeedback.status == status_value)
        if sentiment_value is not None:
            statement = statement.where(RagAnswerFeedback.sentiment == sentiment_value)
        if position is not None:
            statement = statement.where(
                or_(
                    RagAnswerFeedback.created_at > position.created_at,
                    and_(
                        RagAnswerFeedback.created_at == position.created_at,
                        RagAnswerFeedback.id > position.feedback_id,
                    ),
                )
            )
        rows = list(
            session.execute(
                statement.order_by(RagAnswerFeedback.created_at, RagAnswerFeedback.id).limit(
                    limit + 1
                )
            )
        )
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = visible[-1][0]
            next_cursor = encode_feedback_cursor(
                department_id=request_scope.department.value,
                status=status_value,
                sentiment=sentiment_value,
                created_at=last.created_at,
                feedback_id=last.id,
            )
        return FeedbackPage(
            tuple(
                _snapshot_response(
                    item,
                    answer_status=answer_status,
                    reason_codes=reason_codes,
                    source_ids=source_ids,
                )
                for item, answer_status, reason_codes, source_ids in visible
            ),
            next_cursor,
        )

    return _service_call(operation)


def read_feedback_for_review(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    feedback_id: UUID,
) -> RagFeedbackResponse:
    def operation() -> RagFeedbackResponse:
        authorize_transaction(
            session,
            principal,
            request_scope,
            REVIEWER_ROLES,
            lock=False,
            audit_action="rag.feedback.review_read.authorization",
        )
        row = session.execute(
            _feedback_snapshot_statement().where(
                RagAnswerFeedback.id == feedback_id,
                RagAnswerFeedback.department_id == request_scope.department.value,
                RagAnswerFeedback.expires_at > func.statement_timestamp(),
            )
        ).one_or_none()
        if row is None:
            raise ServiceError(404, "Feedback not found")
        return _snapshot_response(
            row[0], answer_status=row[1], reason_codes=row[2], source_ids=row[3]
        )

    return _service_call(operation)


def review_feedback(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    feedback_id: UUID,
    *,
    new_status: FeedbackStatus,
    resolution_code: str | None,
    expected_version: int,
) -> RagFeedbackResponse:
    def operation() -> RagFeedbackResponse:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            REVIEWER_ROLES,
            lock=True,
            audit_action="rag.feedback.review.authorization",
        )
        feedback = session.execute(
            select(RagAnswerFeedback)
            .where(
                RagAnswerFeedback.id == feedback_id,
                RagAnswerFeedback.department_id == request_scope.department.value,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if feedback is None or feedback.expires_at <= _server_now(session):
            raise ServiceError(404, "Feedback not found")
        if feedback.version != expected_version:
            raise ServiceError(409, "Feedback version conflict")
        try:
            validate_review_transition(
                current_status=feedback.status,
                new_status=new_status,
                resolution_code=resolution_code,
            )
        except FeedbackContractError as error:
            raise ServiceError(409, "Invalid feedback transition") from error
        now = _server_now(session)
        if feedback.expires_at <= now:
            raise ServiceError(404, "Feedback not found")
        feedback.status = new_status.value
        feedback.resolution_code = resolution_code
        feedback.reviewed_by_user_id = authorization.identity.id
        feedback.reviewed_at = now
        feedback.updated_at = now
        feedback.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="rag.feedback.review",
            resource_type="rag_answer_feedback",
            resource_id=feedback.id,
        )
        session.flush()
        return _response(session, feedback)

    return _service_call(operation)


def purge_feedback_batch(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    apply: bool,
) -> PurgeResult:
    """Inspect or delete one bounded oldest-first expired batch."""

    limit = _validate_limit(limit, maximum=MAX_PURGE_LIMIT, detail="Invalid purge limit")

    def operation() -> PurgeResult:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            PURGE_ROLES,
            lock=apply,
            audit_action="rag.feedback.purge.authorization",
        )
        statement = (
            select(RagAnswerFeedback)
            .where(
                RagAnswerFeedback.department_id == request_scope.department.value,
                RagAnswerFeedback.expires_at <= func.clock_timestamp(),
            )
            .order_by(RagAnswerFeedback.expires_at, RagAnswerFeedback.id)
            .limit(limit)
        )
        if apply:
            statement = statement.with_for_update(skip_locked=True)
        rows = list(session.scalars(statement))
        oldest = rows[0].expires_at if rows else None
        newest = rows[-1].expires_at if rows else None
        if not apply:
            return PurgeResult(
                request_scope.department.value,
                len(rows),
                oldest,
                newest,
                0,
                False,
            )
        for feedback in rows:
            session.execute(
                delete(RagAnswerFeedbackSourceTarget).where(
                    RagAnswerFeedbackSourceTarget.feedback_id == feedback.id,
                    RagAnswerFeedbackSourceTarget.department_id == feedback.department_id,
                    RagAnswerFeedbackSourceTarget.run_id == feedback.run_id,
                )
            )
            session.execute(
                delete(RagAnswerFeedbackReason).where(
                    RagAnswerFeedbackReason.feedback_id == feedback.id,
                    RagAnswerFeedbackReason.department_id == feedback.department_id,
                    RagAnswerFeedbackReason.run_id == feedback.run_id,
                )
            )
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=principal.subject,
                request_scope=request_scope,
                action="rag.feedback.purge",
                resource_type="rag_answer_feedback",
                resource_id=feedback.id,
            )
            session.flush()
            session.delete(feedback)
        session.flush()
        return PurgeResult(
            request_scope.department.value,
            len(rows),
            oldest,
            newest,
            len(rows),
            True,
        )

    return _service_call(operation)
