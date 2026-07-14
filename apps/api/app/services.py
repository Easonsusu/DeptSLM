"""Transactional business rules for department and membership management."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole, MembershipStatus
from app.authorization import DepartmentAuthorizationContext, DepartmentScope
from app.models import Department, Membership, PersistentAuditEvent, UserIdentity
from app.repositories import (
    find_active_identity,
    get_scoped_department,
    get_scoped_membership,
    list_principal_departments,
    list_scoped_memberships,
)
from app.schemas import MembershipResponse

ADMIN_ROLES = {DepartmentRole.DEPARTMENT_ADMIN.value, DepartmentRole.SYSTEM_ADMIN.value}


class ServiceError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _db_call(operation):
    try:
        return operation()
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Resource conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _actor(session: Session, principal: AuthenticatedPrincipal) -> UserIdentity:
    actor = find_active_identity(session, principal)
    if actor is None:
        raise ServiceError(403, "Department access denied")
    return actor


def _audit(
    session: Session,
    *,
    actor: UserIdentity | None,
    actor_subject: str | None,
    scope: DepartmentScope | None,
    action: str,
    resource_type: str,
    resource_id: UUID | None,
    correlation_id: str | None,
) -> None:
    session.add(
        PersistentAuditEvent(
            actor_subject=actor_subject,
            actor_user_id=actor.id if actor else None,
            department_id=scope.value if scope else None,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            result="allowed",
            reason_code="mutation_applied",
            correlation_id=UUID(correlation_id) if correlation_id else None,
        )
    )


def list_departments(session: Session, principal: AuthenticatedPrincipal, limit: int, offset: int):
    return _db_call(
        lambda: list_principal_departments(session, principal, limit=limit, offset=offset)
    )


def get_department(session: Session, scope: DepartmentScope) -> Department:
    department = _db_call(lambda: get_scoped_department(session, scope))
    if department is None:
        raise ServiceError(404, "Department not found")
    return department


def update_department(
    session: Session,
    principal: AuthenticatedPrincipal,
    context: DepartmentAuthorizationContext,
    display_name: str,
) -> Department:
    def operation() -> Department:
        actor = _actor(session, principal)
        department = get_scoped_department(session, context.department)
        if department is None or department.status != "active":
            raise ServiceError(404, "Department not found")
        department.display_name = display_name
        department.version += 1
        _audit(
            session,
            actor=actor,
            actor_subject=principal.subject,
            scope=context.department,
            action="department.update",
            resource_type="department",
            resource_id=department.id,
            correlation_id=context.correlation_id,
        )
        session.flush()
        return department

    return _db_call(operation)


def archive_department(
    session: Session,
    principal: AuthenticatedPrincipal,
    context: DepartmentAuthorizationContext,
    confirm_slug: str,
) -> Department:
    def operation() -> Department:
        actor = _actor(session, principal)
        department = get_scoped_department(session, context.department)
        if department is None or department.status != "active":
            raise ServiceError(404, "Department not found")
        if confirm_slug != department.slug:
            raise ServiceError(409, "Archive confirmation did not match")
        department.status = "archived"
        department.version += 1
        _audit(
            session,
            actor=actor,
            actor_subject=principal.subject,
            scope=context.department,
            action="department.archive",
            resource_type="department",
            resource_id=department.id,
            correlation_id=context.correlation_id,
        )
        session.flush()
        return department

    return _db_call(operation)


def membership_response(row: tuple[Membership, UserIdentity]) -> MembershipResponse:
    membership, identity = row
    return MembershipResponse(
        id=membership.id,
        department_id=membership.department_id,
        subject=identity.subject,
        role=DepartmentRole(membership.role),
        status=MembershipStatus(membership.status),
        expires_at=membership.expires_at,
        version=membership.version,
        created_at=membership.created_at,
        updated_at=membership.updated_at,
    )


def list_memberships(session: Session, scope: DepartmentScope, limit: int, offset: int):
    return _db_call(lambda: list_scoped_memberships(session, scope, limit=limit, offset=offset))


def get_membership(session: Session, scope: DepartmentScope, membership_id: UUID):
    row = _db_call(lambda: get_scoped_membership(session, scope, membership_id))
    if row is None:
        raise ServiceError(404, "Membership not found")
    return row


def create_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    context: DepartmentAuthorizationContext,
    subject: str,
    role: DepartmentRole,
    expires_at: datetime | None,
):
    if role is DepartmentRole.SYSTEM_ADMIN:
        raise ServiceError(403, "Role cannot be granted through this API")
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ServiceError(409, "Membership expiry must be in the future")

    def operation():
        actor = _actor(session, principal)
        identity = session.execute(
            select(UserIdentity)
            .where(UserIdentity.issuer == principal.issuer, UserIdentity.subject == subject)
            .with_for_update()
        ).scalar_one_or_none()
        if identity is None:
            identity = UserIdentity(issuer=principal.issuer, subject=subject, status="active")
            session.add(identity)
            session.flush()
        elif identity.status != "active":
            raise ServiceError(409, "Identity is not active")
        membership = Membership(
            user_id=identity.id,
            department_id=context.department.value,
            role=role.value,
            status="active",
            expires_at=expires_at,
            created_by_user_id=actor.id,
        )
        session.add(membership)
        session.flush()
        _audit(
            session,
            actor=actor,
            actor_subject=principal.subject,
            scope=context.department,
            action="membership.create",
            resource_type="membership",
            resource_id=membership.id,
            correlation_id=context.correlation_id,
        )
        session.flush()
        return membership, identity

    return _db_call(operation)


def _protect_last_admin(
    session: Session,
    scope: DepartmentScope,
    target: Membership,
    *,
    new_role: str,
    new_status: str,
    new_expiry: datetime | None,
) -> None:
    now = datetime.now(UTC)
    currently_admin = (
        target.role in ADMIN_ROLES
        and target.status == "active"
        and (target.expires_at is None or target.expires_at > now)
    )
    remains_admin = (
        new_role in ADMIN_ROLES
        and new_status == "active"
        and (new_expiry is None or new_expiry > now)
    )
    if not currently_admin or remains_admin:
        return
    administrators = (
        session.execute(
            select(Membership)
            .where(
                Membership.department_id == scope.value,
                Membership.role.in_(ADMIN_ROLES),
                Membership.status == "active",
                or_(Membership.expires_at.is_(None), Membership.expires_at > now),
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if len(administrators) <= 1:
        raise ServiceError(409, "Department must retain an active administrator")


def update_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    context: DepartmentAuthorizationContext,
    membership_id: UUID,
    *,
    role: DepartmentRole | None,
    status: MembershipStatus | None,
    expires_at: datetime | None,
    expiry_supplied: bool,
):
    if role is DepartmentRole.SYSTEM_ADMIN:
        raise ServiceError(403, "Role cannot be granted through this API")

    def operation():
        actor = _actor(session, principal)
        row = get_scoped_membership(session, context.department, membership_id, lock=True)
        if row is None:
            raise ServiceError(404, "Membership not found")
        membership, identity = row
        new_role = role.value if role else membership.role
        new_status = status.value if status else membership.status
        new_expiry = expires_at if expiry_supplied else membership.expires_at
        if membership.status == "revoked" and new_status != "revoked":
            raise ServiceError(409, "Revoked memberships cannot be reactivated")
        _protect_last_admin(
            session,
            context.department,
            membership,
            new_role=new_role,
            new_status=new_status,
            new_expiry=new_expiry,
        )
        membership.role, membership.status, membership.expires_at = new_role, new_status, new_expiry
        membership.version += 1
        _audit(
            session,
            actor=actor,
            actor_subject=principal.subject,
            scope=context.department,
            action="membership.update",
            resource_type="membership",
            resource_id=membership.id,
            correlation_id=context.correlation_id,
        )
        session.flush()
        return membership, identity

    return _db_call(operation)


def revoke_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    context: DepartmentAuthorizationContext,
    membership_id: UUID,
):
    def operation():
        actor = _actor(session, principal)
        row = get_scoped_membership(session, context.department, membership_id, lock=True)
        if row is None:
            raise ServiceError(404, "Membership not found")
        membership, identity = row
        _protect_last_admin(
            session,
            context.department,
            membership,
            new_role=membership.role,
            new_status="revoked",
            new_expiry=membership.expires_at,
        )
        membership.status = "revoked"
        membership.version += 1
        _audit(
            session,
            actor=actor,
            actor_subject=principal.subject,
            scope=context.department,
            action="membership.revoke",
            resource_type="membership",
            resource_id=membership.id,
            correlation_id=context.correlation_id,
        )
        session.flush()
        return membership, identity

    return _db_call(operation)
