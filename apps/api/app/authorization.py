"""Fail-closed department authorization primitives and dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status

from app.audit import AuditEvent, AuditResult, AuditSink
from app.auth import (
    AuthenticatedPrincipal,
    AuthenticationError,
    DepartmentRole,
    MembershipStatus,
    TokenVerifier,
)


@dataclass(frozen=True, slots=True)
class DepartmentScope:
    """Canonical immutable department identifier."""

    value: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.value, UUID) or self.value.int == 0:
            raise ValueError("department scope must be a non-zero UUID")

    @classmethod
    def parse(cls, raw: str | None) -> DepartmentScope:
        if raw is None or not raw.strip():
            raise ValueError("department scope is required")
        try:
            value = UUID(raw)
        except (ValueError, AttributeError) as error:
            raise ValueError("department scope is malformed") from error
        return cls(value=value)

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class MembershipResult:
    membership_ref: str
    subject: str
    department: DepartmentScope
    role: DepartmentRole
    status: MembershipStatus


class MembershipResolver(Protocol):
    def resolve(
        self, principal: AuthenticatedPrincipal, department: DepartmentScope
    ) -> MembershipResult | None: ...


@dataclass(frozen=True, slots=True)
class DenyAllMembershipResolver:
    """Runtime default until persistent memberships are added in Phase 3."""

    def resolve(
        self, principal: AuthenticatedPrincipal, department: DepartmentScope
    ) -> MembershipResult | None:
        del principal, department
        return None


@dataclass(frozen=True, slots=True)
class DepartmentAuthorizationContext:
    subject: str
    department: DepartmentScope
    role: DepartmentRole
    membership_ref: str
    correlation_id: str | None = None


def _audit(request: Request, event: AuditEvent) -> None:
    sink: AuditSink = request.app.state.audit_sink
    sink.emit(event)


def _correlation_id(request: Request) -> str | None:
    value = request.headers.get("x-request-id")
    if not value:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def require_authenticated_principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> AuthenticatedPrincipal:
    """Validate a strict Bearer header and return safe identity metadata."""

    correlation_id = _correlation_id(request)
    if authorization is None:
        _audit(
            request,
            AuditEvent(
                None,
                "authenticate",
                AuditResult.DENIED,
                "missing_bearer",
                correlation_id=correlation_id,
            ),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        _audit(
            request,
            AuditEvent(
                None,
                "authenticate",
                AuditResult.DENIED,
                "malformed_bearer",
                correlation_id=correlation_id,
            ),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication")

    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        principal = verifier.verify(parts[1])
    except AuthenticationError:
        _audit(
            request,
            AuditEvent(
                None,
                "authenticate",
                AuditResult.DENIED,
                "invalid_token",
                correlation_id=correlation_id,
            ),
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication") from None

    _audit(
        request,
        AuditEvent(
            principal.subject,
            "authenticate",
            AuditResult.ALLOWED,
            "token_valid",
            correlation_id=correlation_id,
        ),
    )
    return principal


def require_department_scope(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    department_id: Annotated[str | None, Header(alias="X-Department-ID")] = None,
) -> DepartmentAuthorizationContext:
    """Resolve active server-side membership for one explicit department."""

    correlation_id = _correlation_id(request)
    try:
        department = DepartmentScope.parse(department_id)
    except ValueError:
        _audit(
            request,
            AuditEvent(
                principal.subject,
                "authorize_department",
                AuditResult.DENIED,
                "invalid_scope",
                correlation_id=correlation_id,
            ),
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Department access denied") from None

    resolver: MembershipResolver = request.app.state.membership_resolver
    membership = resolver.resolve(principal, department)
    if (
        membership is None
        or membership.subject != principal.subject
        or membership.department != department
        or membership.status is not MembershipStatus.ACTIVE
    ):
        _audit(
            request,
            AuditEvent(
                principal.subject,
                "authorize_department",
                AuditResult.DENIED,
                "membership_denied",
                correlation_id=correlation_id,
            ),
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Department access denied")

    context = DepartmentAuthorizationContext(
        subject=principal.subject,
        department=department,
        role=membership.role,
        membership_ref=membership.membership_ref,
        correlation_id=correlation_id,
    )
    _audit(
        request,
        AuditEvent(
            principal.subject,
            "authorize_department",
            AuditResult.ALLOWED,
            "active_membership",
            str(department),
            correlation_id,
        ),
    )
    return context


def require_department_roles(
    *allowed_roles: DepartmentRole,
) -> Callable[[Request, DepartmentAuthorizationContext], DepartmentAuthorizationContext]:
    """Create a dependency requiring an authorized context and explicit roles."""

    allowed = frozenset(allowed_roles)
    if not allowed:
        raise ValueError("at least one department role is required")

    def dependency(
        request: Request,
        context: DepartmentAuthorizationContext = Depends(require_department_scope),
    ) -> DepartmentAuthorizationContext:
        if context.role not in allowed:
            _audit(
                request,
                AuditEvent(
                    context.subject,
                    "authorize_role",
                    AuditResult.DENIED,
                    "role_denied",
                    str(context.department),
                    context.correlation_id,
                ),
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Department access denied")
        _audit(
            request,
            AuditEvent(
                context.subject,
                "authorize_role",
                AuditResult.ALLOWED,
                "role_allowed",
                str(context.department),
                context.correlation_id,
            ),
        )
        return context

    return dependency
