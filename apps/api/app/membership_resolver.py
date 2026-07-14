"""PostgreSQL-backed membership resolution for authorization."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal, DepartmentRole, MembershipStatus
from app.authorization import (
    DepartmentScope,
    MembershipResolutionUnavailable,
    MembershipResult,
)
from app.models import Department, Membership, UserIdentity


class SQLAlchemyMembershipResolver:
    """Resolve exactly one current membership using server-owned records."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def resolve(
        self, principal: AuthenticatedPrincipal, department: DepartmentScope
    ) -> MembershipResult | None:
        now = datetime.now(UTC)
        statement = (
            select(Membership)
            .join(UserIdentity, Membership.user_id == UserIdentity.id)
            .join(Department, Membership.department_id == Department.id)
            .where(
                UserIdentity.issuer == principal.issuer,
                UserIdentity.subject == principal.subject,
                UserIdentity.status == "active",
                Department.id == department.value,
                Department.status == "active",
                Membership.status == MembershipStatus.ACTIVE.value,
                or_(Membership.expires_at.is_(None), Membership.expires_at > now),
            )
        )
        try:
            with self._session_factory() as session:
                membership = session.execute(statement).scalar_one_or_none()
        except SQLAlchemyError as error:
            raise MembershipResolutionUnavailable from error
        if membership is None:
            return None
        try:
            return MembershipResult(
                membership_ref=str(membership.id),
                subject=principal.subject,
                department=department,
                role=DepartmentRole(membership.role),
                status=MembershipStatus(membership.status),
            )
        except ValueError as error:
            raise MembershipResolutionUnavailable from error
