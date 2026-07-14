"""Department-scoped Phase 3 API routes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import AuthenticatedPrincipal
from app.authorization import (
    DepartmentRequestScope,
    require_authenticated_principal,
    require_path_department_selector,
)
from app.database import DatabaseSession
from app.schemas import (
    DepartmentArchive,
    DepartmentListResponse,
    DepartmentResponse,
    DepartmentUpdate,
    MembershipCreate,
    MembershipListResponse,
    MembershipResponse,
    MembershipUpdate,
)
from app.services import (
    ServiceError,
    archive_department,
    create_membership,
    get_department,
    get_membership,
    list_departments,
    list_memberships,
    membership_response,
    revoke_membership,
    update_department,
    update_membership,
)

router = APIRouter()


def _raise(error: ServiceError) -> None:
    raise HTTPException(error.status_code, error.detail) from None


@router.get("/departments", response_model=DepartmentListResponse, tags=["departments"])
def get_departments(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DepartmentListResponse:
    try:
        items = list_departments(session, principal, limit, offset)
    except ServiceError as error:
        _raise(error)
    return DepartmentListResponse(items=items, limit=limit, offset=offset)


@router.get("/departments/{department_id}", response_model=DepartmentResponse, tags=["departments"])
def read_department(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> DepartmentResponse:
    try:
        return DepartmentResponse.model_validate(get_department(session, principal, request_scope))
    except ServiceError as error:
        _raise(error)


@router.patch(
    "/departments/{department_id}", response_model=DepartmentResponse, tags=["departments"]
)
def patch_department(
    body: DepartmentUpdate,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> DepartmentResponse:
    try:
        value = update_department(session, principal, request_scope, body.display_name)
        return DepartmentResponse.model_validate(value)
    except ServiceError as error:
        _raise(error)


@router.delete(
    "/departments/{department_id}", response_model=DepartmentResponse, tags=["departments"]
)
def delete_department(
    body: DepartmentArchive,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> DepartmentResponse:
    try:
        value = archive_department(session, principal, request_scope, body.confirm_slug)
        return DepartmentResponse.model_validate(value)
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/memberships",
    response_model=MembershipListResponse,
    tags=["memberships"],
)
def get_memberships(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MembershipListResponse:
    try:
        rows = list_memberships(session, principal, request_scope, limit, offset)
        return MembershipListResponse(
            items=[membership_response(row) for row in rows], limit=limit, offset=offset
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/memberships",
    response_model=MembershipResponse,
    status_code=201,
    tags=["memberships"],
)
def post_membership(
    body: MembershipCreate,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> MembershipResponse:
    try:
        return membership_response(
            create_membership(
                session,
                principal,
                request_scope,
                body.subject,
                body.role,
                body.expires_at,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/memberships/{membership_id}",
    response_model=MembershipResponse,
    tags=["memberships"],
)
def read_membership(
    membership_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> MembershipResponse:
    try:
        return membership_response(get_membership(session, principal, request_scope, membership_id))
    except ServiceError as error:
        _raise(error)


@router.patch(
    "/departments/{department_id}/memberships/{membership_id}",
    response_model=MembershipResponse,
    tags=["memberships"],
)
def patch_membership(
    membership_id: UUID,
    body: MembershipUpdate,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> MembershipResponse:
    try:
        expiry_supplied = "expires_at" in body.model_fields_set or body.clear_expiry
        expiry = None if body.clear_expiry else body.expires_at
        return membership_response(
            update_membership(
                session,
                principal,
                request_scope,
                membership_id,
                role=body.role,
                status=body.status,
                expires_at=expiry,
                expiry_supplied=expiry_supplied,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.delete(
    "/departments/{department_id}/memberships/{membership_id}",
    response_model=MembershipResponse,
    tags=["memberships"],
)
def delete_membership(
    membership_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> MembershipResponse:
    try:
        return membership_response(
            revoke_membership(session, principal, request_scope, membership_id)
        )
    except ServiceError as error:
        _raise(error)
