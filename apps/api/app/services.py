"""Transactional business rules for department and membership management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit import AuditEvent, AuditResult
from app.auth import AuthenticatedPrincipal, DepartmentRole, MembershipStatus
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import Department, Membership, PersistentAuditEvent, UserIdentity
from app.repositories import (
    get_scoped_membership,
    has_other_effective_administrator,
    list_principal_departments,
    list_scoped_memberships,
    lock_scoped_department,
    resolve_transaction_membership,
)
from app.schemas import MembershipResponse

ADMIN_ROLES = frozenset((DepartmentRole.DEPARTMENT_ADMIN, DepartmentRole.SYSTEM_ADMIN))
ALL_ROLES = frozenset(DepartmentRole)


class ServiceError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class TransactionAuthorization:
    identity: UserIdentity
    membership: Membership
    department: Department


def _db_call(operation):
    try:
        return operation()
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "Resource conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def authorize_transaction(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    allowed_roles: frozenset[DepartmentRole],
    *,
    lock: bool,
) -> TransactionAuthorization:
    """Revalidate exact authority and emit one safe decision after it is known."""

    scope = request_scope.department
    try:
        locked_department = lock_scoped_department(session, scope) if lock else None
        if lock and (locked_department is None or locked_department.status != "active"):
            _authorization_audit(request_scope, principal, AuditResult.DENIED, "membership_denied")
            raise ServiceError(403, "Department access denied")
        resolved = resolve_transaction_membership(session, principal, scope, lock=lock)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        _authorization_audit(
            request_scope, principal, AuditResult.DENIED, "membership_store_unavailable"
        )
        raise ServiceError(503, "Database unavailable") from error
    if resolved is None:
        _authorization_audit(request_scope, principal, AuditResult.DENIED, "membership_denied")
        raise ServiceError(403, "Department access denied")
    department, identity, membership = resolved
    try:
        role = DepartmentRole(membership.role)
    except ValueError as error:
        _authorization_audit(request_scope, principal, AuditResult.DENIED, "role_denied")
        raise ServiceError(403, "Department access denied") from error
    if role not in allowed_roles:
        _authorization_audit(request_scope, principal, AuditResult.DENIED, "role_denied")
        raise ServiceError(403, "Department access denied")
    _authorization_audit(request_scope, principal, AuditResult.ALLOWED, "active_membership")
    return TransactionAuthorization(identity, membership, department)


def _authorization_audit(
    request_scope: DepartmentRequestScope,
    principal: AuthenticatedPrincipal,
    result: AuditResult,
    reason_code: str,
) -> None:
    if request_scope.audit_sink is None:
        return
    request_scope.audit_sink.emit(
        AuditEvent(
            actor_subject=principal.subject,
            action="authorize_department",
            result=result,
            reason_code=reason_code,
            department_id=str(request_scope.department),
            correlation_id=request_scope.correlation_id,
        )
    )


def _audit(
    session: Session,
    *,
    actor: UserIdentity,
    actor_subject: str,
    request_scope: DepartmentRequestScope,
    action: str,
    resource_type: str,
    resource_id: UUID,
) -> None:
    session.add(
        PersistentAuditEvent(
            actor_subject=actor_subject,
            actor_user_id=actor.id,
            department_id=request_scope.department.value,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            result="allowed",
            reason_code="mutation_applied",
            correlation_id=(
                UUID(request_scope.correlation_id) if request_scope.correlation_id else None
            ),
        )
    )


def list_departments(session: Session, principal: AuthenticatedPrincipal, limit: int, offset: int):
    return _db_call(
        lambda: list_principal_departments(session, principal, limit=limit, offset=offset)
    )


def get_department(
    session: Session, principal: AuthenticatedPrincipal, request_scope: DepartmentRequestScope
) -> Department:
    return _db_call(
        lambda: (
            authorize_transaction(
                session, principal, request_scope, ALL_ROLES, lock=False
            ).department
        )
    )


def update_department(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    display_name: str,
) -> Department:
    def operation() -> Department:
        authorization = authorize_transaction(
            session, principal, request_scope, ADMIN_ROLES, lock=True
        )
        department = authorization.department
        if department.display_name == display_name:
            return department
        department.display_name = display_name
        department.version += 1
        _audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="department.update",
            resource_type="department",
            resource_id=department.id,
        )
        session.flush()
        return department

    return _db_call(operation)


def archive_department(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    confirm_slug: str,
) -> Department:
    def operation() -> Department:
        authorization = authorize_transaction(
            session, principal, request_scope, ADMIN_ROLES, lock=True
        )
        department = authorization.department
        if confirm_slug != department.slug:
            raise ServiceError(409, "Archive confirmation did not match")
        department.status = "archived"
        department.version += 1
        _audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="department.archive",
            resource_type="department",
            resource_id=department.id,
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


def list_memberships(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    limit: int,
    offset: int,
):
    def operation():
        authorize_transaction(session, principal, request_scope, ADMIN_ROLES, lock=False)
        return list_scoped_memberships(
            session, request_scope.department, limit=limit, offset=offset
        )

    return _db_call(operation)


def get_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    membership_id: UUID,
):
    def operation():
        authorize_transaction(session, principal, request_scope, ADMIN_ROLES, lock=False)
        row = get_scoped_membership(session, request_scope.department, membership_id)
        if row is None:
            raise ServiceError(404, "Membership not found")
        return row

    return _db_call(operation)


def create_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    subject: str,
    role: DepartmentRole,
    expires_at: datetime | None,
):
    if role is DepartmentRole.SYSTEM_ADMIN:
        raise ServiceError(403, "Role cannot be granted through this API")
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ServiceError(409, "Membership expiry must be in the future")

    def operation():
        authorization = authorize_transaction(
            session, principal, request_scope, ADMIN_ROLES, lock=True
        )
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
            department_id=request_scope.department.value,
            role=role.value,
            status="active",
            expires_at=expires_at,
            created_by_user_id=authorization.identity.id,
        )
        session.add(membership)
        session.flush()
        _audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="membership.create",
            resource_type="membership",
            resource_id=membership.id,
        )
        session.flush()
        return membership, identity

    return _db_call(operation)


def _is_effective_administrator(
    identity: UserIdentity,
    membership: Membership,
    *,
    role: str,
    status: str,
    expiry: datetime | None,
) -> bool:
    now = datetime.now(UTC)
    return (
        identity.status == "active"
        and role in {item.value for item in ADMIN_ROLES}
        and status == "active"
        and (expiry is None or expiry > now)
    )


def _protect_last_admin(
    session: Session,
    scope: DepartmentScope,
    target: Membership,
    target_identity: UserIdentity,
    *,
    new_role: str,
    new_status: str,
    new_expiry: datetime | None,
) -> None:
    currently_effective = _is_effective_administrator(
        target_identity,
        target,
        role=target.role,
        status=target.status,
        expiry=target.expires_at,
    )
    remains_effective = _is_effective_administrator(
        target_identity,
        target,
        role=new_role,
        status=new_status,
        expiry=new_expiry,
    )
    if (
        currently_effective
        and not remains_effective
        and not has_other_effective_administrator(session, scope, target.id)
    ):
        raise ServiceError(409, "Department must retain an active administrator")


def update_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
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
        authorization = authorize_transaction(
            session, principal, request_scope, ADMIN_ROLES, lock=True
        )
        row = get_scoped_membership(session, request_scope.department, membership_id, lock=True)
        if row is None:
            raise ServiceError(404, "Membership not found")
        membership, identity = row
        new_role = role.value if role else membership.role
        new_status = status.value if status else membership.status
        new_expiry = expires_at if expiry_supplied else membership.expires_at
        if membership.status == "revoked" and new_status != "revoked":
            raise ServiceError(409, "Revoked memberships cannot be reactivated")
        if (new_role, new_status, new_expiry) == (
            membership.role,
            membership.status,
            membership.expires_at,
        ):
            return membership, identity
        _protect_last_admin(
            session,
            request_scope.department,
            membership,
            identity,
            new_role=new_role,
            new_status=new_status,
            new_expiry=new_expiry,
        )
        membership.role = new_role
        membership.status = new_status
        membership.expires_at = new_expiry
        membership.version += 1
        _audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="membership.update",
            resource_type="membership",
            resource_id=membership.id,
        )
        session.flush()
        return membership, identity

    return _db_call(operation)


def revoke_membership(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    membership_id: UUID,
):
    def operation():
        authorization = authorize_transaction(
            session, principal, request_scope, ADMIN_ROLES, lock=True
        )
        row = get_scoped_membership(session, request_scope.department, membership_id, lock=True)
        if row is None:
            raise ServiceError(404, "Membership not found")
        membership, identity = row
        if membership.status == "revoked":
            return membership, identity
        _protect_last_admin(
            session,
            request_scope.department,
            membership,
            identity,
            new_role=membership.role,
            new_status="revoked",
            new_expiry=membership.expires_at,
        )
        membership.status = "revoked"
        membership.version += 1
        _audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="membership.revoke",
            resource_type="membership",
            resource_id=membership.id,
        )
        session.flush()
        return membership, identity

    return _db_call(operation)
