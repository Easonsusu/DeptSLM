"""Department-scoped SQLAlchemy repositories."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
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


def lock_scoped_department(session: Session, scope: DepartmentScope) -> Department | None:
    """Lock the department first to serialize security-sensitive mutations."""

    return session.execute(
        select(Department).where(Department.id == scope.value).with_for_update()
    ).scalar_one_or_none()


def resolve_transaction_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    scope: DepartmentScope,
    *,
    lock: bool,
) -> tuple[Department, UserIdentity, Membership] | None:
    """Resolve exact current membership inside the caller's transaction."""

    now = datetime.now(UTC)
    statement = (
        select(Department, UserIdentity, Membership)
        .join(Membership, Membership.user_id == UserIdentity.id)
        .join(Department, Department.id == Membership.department_id)
        .where(
            UserIdentity.issuer == principal.issuer,
            UserIdentity.subject == principal.subject,
            UserIdentity.status == "active",
            Department.id == scope.value,
            Department.status == "active",
            Membership.department_id == scope.value,
            Membership.status == "active",
            or_(Membership.expires_at.is_(None), Membership.expires_at > now),
        )
    )
    if lock:
        statement = statement.with_for_update(of=(UserIdentity, Membership))
    return session.execute(statement).one_or_none()


def has_other_effective_administrator(
    session: Session, scope: DepartmentScope, excluded_membership_id: UUID
) -> bool:
    """Check effective admins while locking their identity and membership rows."""

    now = datetime.now(UTC)
    statement = (
        select(Membership.id)
        .join(UserIdentity, Membership.user_id == UserIdentity.id)
        .where(
            Membership.department_id == scope.value,
            Membership.id != excluded_membership_id,
            Membership.role.in_(
                (DepartmentRole.DEPARTMENT_ADMIN.value, DepartmentRole.SYSTEM_ADMIN.value)
            ),
            Membership.status == "active",
            UserIdentity.status == "active",
            or_(Membership.expires_at.is_(None), Membership.expires_at > now),
        )
        .with_for_update(of=(Membership, UserIdentity))
    )
    return session.execute(statement).first() is not None


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
        statement = statement.with_for_update(of=(Membership, UserIdentity))
    return session.execute(statement).one_or_none()
