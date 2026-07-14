"""Department-scoped SQLAlchemy repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentScope
from app.models import Department, Membership, UserIdentity


def find_active_identity(
    session: Session, principal: AuthenticatedPrincipal
) -> UserIdentity | None:
    return session.execute(
        select(UserIdentity).where(
            UserIdentity.issuer == principal.issuer,
            UserIdentity.subject == principal.subject,
            UserIdentity.status == "active",
        )
    ).scalar_one_or_none()


def list_principal_departments(
    session: Session, principal: AuthenticatedPrincipal, *, limit: int, offset: int
) -> list[Department]:
    now = datetime.now(UTC)
    return list(
        session.execute(
            select(Department)
            .join(Membership, Membership.department_id == Department.id)
            .join(UserIdentity, Membership.user_id == UserIdentity.id)
            .where(
                UserIdentity.issuer == principal.issuer,
                UserIdentity.subject == principal.subject,
                UserIdentity.status == "active",
                Department.status == "active",
                Membership.status == "active",
                or_(Membership.expires_at.is_(None), Membership.expires_at > now),
            )
            .order_by(Department.slug)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def get_scoped_department(session: Session, scope: DepartmentScope) -> Department | None:
    return session.execute(
        select(Department).where(Department.id == scope.value)
    ).scalar_one_or_none()


def list_scoped_memberships(
    session: Session, scope: DepartmentScope, *, limit: int, offset: int
) -> list[tuple[Membership, UserIdentity]]:
    return list(
        session.execute(
            select(Membership, UserIdentity)
            .join(UserIdentity, Membership.user_id == UserIdentity.id)
            .where(Membership.department_id == scope.value)
            .order_by(Membership.created_at, Membership.id)
            .limit(limit)
            .offset(offset)
        ).all()
    )


def get_scoped_membership(
    session: Session, scope: DepartmentScope, membership_id: UUID, *, lock: bool = False
) -> tuple[Membership, UserIdentity] | None:
    statement = (
        select(Membership, UserIdentity)
        .join(UserIdentity, Membership.user_id == UserIdentity.id)
        .where(Membership.department_id == scope.value, Membership.id == membership_id)
    )
    if lock:
        statement = statement.with_for_update(of=Membership)
    return session.execute(statement).one_or_none()
