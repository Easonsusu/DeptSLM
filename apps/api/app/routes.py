"""Department-scoped control-plane routes through Phase 8."""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError

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
from app.feedback_request_body import (
    FEEDBACK_REVIEW_BODY_MAX_BYTES,
    FEEDBACK_SUBMIT_BODY_MAX_BYTES,
    FeedbackBodyError,
    read_bounded_json_object,
)
from app.rag_answer_services import (
    RagAnswerServiceError,
    answer_question,
)
from app.rag_feedback_domain import FeedbackSentiment, FeedbackStatus
from app.rag_feedback_services import (
    list_feedback_for_review,
    read_feedback_for_review,
    read_own_feedback,
    review_feedback,
    submit_feedback,
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
    RagAnswerRequest,
    RagAnswerResponse,
    RagFeedbackListResponse,
    RagFeedbackResponse,
    RagFeedbackReviewRequest,
    RagFeedbackSubmitRequest,
    VectorIndexingListResponse,
    VectorIndexingResponse,
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
from app.vector_index_services import (
    enqueue_indexing,
    list_indexings,
    read_indexing,
    retry_indexing,
)

router = APIRouter()


def _raise(error: ServiceError) -> None:
    raise HTTPException(error.status_code, error.detail) from None


def _raise_upload(error: UploadError) -> None:
    raise HTTPException(error.status_code, error.detail) from None


async def _validated_feedback_body(request: Request, model, *, maximum_bytes: int):
    try:
        payload = await read_bounded_json_object(request, maximum_bytes=maximum_bytes)
    except FeedbackBodyError as error:
        raise HTTPException(error.status_code, error.detail) from None
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise HTTPException(422, "Invalid feedback request") from None


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


@router.post(
    "/departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings",
    response_model=VectorIndexingResponse,
    status_code=202,
    tags=["document-vector-indexings"],
)
def post_vector_indexing(
    document_id: UUID,
    extraction_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> VectorIndexingResponse:
    try:
        return VectorIndexingResponse.model_validate(
            enqueue_indexing(session, principal, request_scope, document_id, extraction_id)
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings",
    response_model=VectorIndexingListResponse,
    tags=["document-vector-indexings"],
)
def get_vector_indexings(
    document_id: UUID,
    extraction_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> VectorIndexingListResponse:
    try:
        rows = list_indexings(
            session, principal, request_scope, document_id, extraction_id, limit, offset
        )
        return VectorIndexingListResponse(
            items=[VectorIndexingResponse.model_validate(row) for row in rows],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings/{indexing_id}",
    response_model=VectorIndexingResponse,
    tags=["document-vector-indexings"],
)
def get_vector_indexing(
    document_id: UUID,
    extraction_id: UUID,
    indexing_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> VectorIndexingResponse:
    try:
        return VectorIndexingResponse.model_validate(
            read_indexing(
                session,
                principal,
                request_scope,
                document_id,
                extraction_id,
                indexing_id,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/documents/{document_id}/extractions/{extraction_id}/indexings/{indexing_id}/retry",
    response_model=VectorIndexingResponse,
    status_code=202,
    tags=["document-vector-indexings"],
)
def post_vector_indexing_retry(
    document_id: UUID,
    extraction_id: UUID,
    indexing_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> VectorIndexingResponse:
    try:
        return VectorIndexingResponse.model_validate(
            retry_indexing(
                session,
                principal,
                request_scope,
                document_id,
                extraction_id,
                indexing_id,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/rag/answers",
    response_model=RagAnswerResponse,
    tags=["grounded-answers"],
)
async def post_rag_answer(
    body: RagAnswerRequest,
    request: Request,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> RagAnswerResponse:
    """Return one non-streaming answer grounded in current authorized sources."""

    settings = request.app.state.settings
    if settings.rag is None:
        raise HTTPException(503, "Grounded answer unavailable")
    try:
        return await asyncio.to_thread(
            answer_question,
            request.app.state.session_factory,
            settings.rag,
            settings.data_dir,
            principal,
            request_scope,
            body.question,
            runtime=getattr(request.app.state, "rag_runtime_client", None),
            qdrant=getattr(request.app.state, "rag_qdrant", None),
        )
    except ServiceError as error:
        _raise(error)
    except RagAnswerServiceError:
        raise HTTPException(503, "Grounded answer unavailable") from None


@router.put(
    "/departments/{department_id}/rag/answers/{run_id}/feedback",
    response_model=RagFeedbackResponse,
    tags=["rag-feedback"],
)
async def put_rag_feedback(
    run_id: UUID,
    response: Response,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> RagFeedbackResponse:
    body = await _validated_feedback_body(
        request,
        RagFeedbackSubmitRequest,
        maximum_bytes=FEEDBACK_SUBMIT_BODY_MAX_BYTES,
    )
    try:
        result = submit_feedback(
            session,
            principal,
            request_scope,
            run_id,
            sentiment=body.sentiment,
            reason_codes=[item.value for item in body.reason_codes],
            source_ids=[item.value for item in body.source_ids],
            retention_days=request.app.state.settings.rag_feedback_retention_days,
        )
        response.status_code = 201 if result.created else 200
        return result.response
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/rag/answers/{run_id}/feedback",
    response_model=RagFeedbackResponse,
    tags=["rag-feedback"],
)
def get_own_rag_feedback(
    run_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> RagFeedbackResponse:
    try:
        return read_own_feedback(session, principal, request_scope, run_id)
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/rag/feedback",
    response_model=RagFeedbackListResponse,
    tags=["rag-feedback-review"],
)
def get_rag_feedback_queue(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    status_filter: Annotated[FeedbackStatus | None, Query(alias="status")] = None,
    sentiment: FeedbackSentiment | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> RagFeedbackListResponse:
    try:
        page = list_feedback_for_review(
            session,
            principal,
            request_scope,
            status=status_filter,
            sentiment=sentiment,
            limit=limit,
            cursor=cursor,
        )
        return RagFeedbackListResponse(items=list(page.items), next_cursor=page.next_cursor)
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/rag/feedback/{feedback_id}",
    response_model=RagFeedbackResponse,
    tags=["rag-feedback-review"],
)
def get_rag_feedback_for_review(
    feedback_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> RagFeedbackResponse:
    try:
        return read_feedback_for_review(session, principal, request_scope, feedback_id)
    except ServiceError as error:
        _raise(error)


@router.patch(
    "/departments/{department_id}/rag/feedback/{feedback_id}",
    response_model=RagFeedbackResponse,
    tags=["rag-feedback-review"],
)
async def patch_rag_feedback_for_review(
    feedback_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> RagFeedbackResponse:
    body = await _validated_feedback_body(
        request,
        RagFeedbackReviewRequest,
        maximum_bytes=FEEDBACK_REVIEW_BODY_MAX_BYTES,
    )
    try:
        return review_feedback(
            session,
            principal,
            request_scope,
            feedback_id,
            new_status=body.status,
            resolution_code=(
                body.resolution_code.value if body.resolution_code is not None else None
            ),
            expected_version=body.expected_version,
        )
    except ServiceError as error:
        _raise(error)
