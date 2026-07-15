"""Department-scoped control-plane routes through Phase 5."""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.audit import AuditResult
from app.auth import AuthenticatedPrincipal
from app.authorization import (
    DepartmentRequestScope,
    require_authenticated_principal,
    require_path_department_selector,
)
from app.database import DatabaseSession
from app.document_services import (
    admit_document_upload,
    delete_document,
    emit_document_event,
    finalize_document_upload,
    get_document,
    list_documents,
)
from app.document_storage import DocumentStorageError
from app.document_upload import UploadError, parse_upload_metadata, stream_upload
from app.extraction_services import (
    enqueue_extraction,
    list_chunks,
    list_extractions,
    read_extraction,
    retry_extraction,
)
from app.schemas import (
    ChunkListResponse,
    ChunkResponse,
    DepartmentArchive,
    DepartmentListResponse,
    DepartmentResponse,
    DepartmentUpdate,
    DocumentListResponse,
    DocumentResponse,
    ExtractionListResponse,
    ExtractionResponse,
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


def _raise_upload(error: UploadError) -> None:
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


@router.get(
    "/departments/{department_id}/documents",
    response_model=DocumentListResponse,
    tags=["documents"],
)
def get_documents(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    try:
        items = list_documents(session, principal, request_scope, limit, offset)
        return DocumentListResponse(
            items=[DocumentResponse.model_validate(item) for item in items],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/documents/{document_id}",
    response_model=DocumentResponse,
    tags=["documents"],
)
def read_document(
    document_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> DocumentResponse:
    try:
        return DocumentResponse.model_validate(
            get_document(session, principal, request_scope, document_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/documents",
    response_model=DocumentResponse,
    status_code=201,
    tags=["documents"],
)
async def post_document(
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> DocumentResponse:
    settings = request.app.state.settings
    factory = request.app.state.session_factory
    try:
        await asyncio.to_thread(admit_document_upload, factory, principal, request_scope)
    except ServiceError as error:
        _raise(error)

    try:
        metadata = parse_upload_metadata(request.headers, settings.document_max_bytes)
    except UploadError as error:
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.validation",
            result=AuditResult.DENIED,
            reason_code=error.reason_code,
        )
        _raise_upload(error)

    try:
        staged = await asyncio.to_thread(
            request.app.state.document_storage.create_staging,
            request_scope.department,
            uuid4(),
        )
    except DocumentStorageError:
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.storage",
            result=AuditResult.DENIED,
            reason_code="storage_unavailable",
        )
        raise HTTPException(503, "Document storage unavailable") from None

    try:
        streamed = await stream_upload(request, staged, metadata, settings.document_max_bytes)
    except DocumentStorageError:
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.storage",
            result=AuditResult.DENIED,
            reason_code="storage_unavailable",
        )
        raise HTTPException(503, "Document storage unavailable") from None
    except UploadError as error:
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.validation",
            result=AuditResult.DENIED,
            reason_code=error.reason_code,
        )
        _raise_upload(error)

    try:
        document = await asyncio.to_thread(
            finalize_document_upload,
            factory,
            principal,
            request_scope,
            metadata,
            streamed,
            staged,
            settings.department_document_quota_bytes,
        )
        return DocumentResponse.model_validate(document)
    except ServiceError as error:
        _raise(error)


@router.delete(
    "/departments/{department_id}/documents/{document_id}",
    response_model=DocumentResponse,
    tags=["documents"],
)
def remove_document(
    document_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> DocumentResponse:
    try:
        return DocumentResponse.model_validate(
            delete_document(session, principal, request_scope, document_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/documents/{document_id}/extractions",
    response_model=ExtractionResponse,
    status_code=202,
    tags=["document-extractions"],
)
def post_extraction(
    document_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> ExtractionResponse:
    try:
        return ExtractionResponse.model_validate(
            enqueue_extraction(session, principal, request_scope, document_id)
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/documents/{document_id}/extractions",
    response_model=ExtractionListResponse,
    tags=["document-extractions"],
)
def get_extractions(
    document_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExtractionListResponse:
    try:
        rows = list_extractions(session, principal, request_scope, document_id, limit, offset)
        return ExtractionListResponse(
            items=[ExtractionResponse.model_validate(row) for row in rows],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/documents/{document_id}/extractions/{extraction_id}",
    response_model=ExtractionResponse,
    tags=["document-extractions"],
)
def get_extraction(
    document_id: UUID,
    extraction_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> ExtractionResponse:
    try:
        return ExtractionResponse.model_validate(
            read_extraction(session, principal, request_scope, document_id, extraction_id)
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/chunks",
    response_model=ChunkListResponse,
    tags=["document-extractions"],
)
def get_extraction_chunks(
    document_id: UUID,
    extraction_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChunkListResponse:
    try:
        rows = list_chunks(
            session,
            principal,
            request_scope,
            document_id,
            extraction_id,
            limit,
            offset,
        )
        return ChunkListResponse(
            items=[ChunkResponse.model_validate(row) for row in rows],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/retry",
    response_model=ExtractionResponse,
    status_code=202,
    tags=["document-extractions"],
)
def post_extraction_retry(
    document_id: UUID,
    extraction_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> ExtractionResponse:
    try:
        return ExtractionResponse.model_validate(
            retry_extraction(session, principal, request_scope, document_id, extraction_id)
        )
    except ServiceError as error:
        _raise(error)
