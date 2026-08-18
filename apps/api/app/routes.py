"""Department-scoped control-plane routes through Phase 8."""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError

from app.adapter_evaluation_services import (
    cancel_adapter_evaluation,
    enqueue_adapter_evaluation,
    list_adapter_evaluations,
    read_adapter_evaluation,
)
from app.adapter_governance_services import (
    cancel_operation,
    enqueue_promotion,
    enqueue_rollback,
    list_events,
    list_operations,
    list_reviews,
    read_deployment,
    read_operation,
    read_review,
    release_rollback_retention,
    start_review,
    transition_review,
)
from app.adapter_registry_read_services import list_adapters, read_adapter
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
from app.evaluation_domain import MAX_CANCEL_BODY_BYTES, MAX_RUN_BODY_BYTES
from app.evaluation_request_body import (
    EvaluationBodyError,
    read_bounded_evaluation_object,
)
from app.evaluation_services import (
    cancel_evaluation_run,
    enqueue_evaluation_run,
    list_evaluation_runs,
    list_evaluation_suites,
    read_evaluation_run,
    read_evaluation_suite,
)
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
    AdapterDeploymentCancelRequest,
    AdapterDeploymentEventListResponse,
    AdapterDeploymentOperationListResponse,
    AdapterDeploymentOperationResponse,
    AdapterDeploymentResponse,
    AdapterEvaluationCancelRequest,
    AdapterEvaluationCreateRequest,
    AdapterEvaluationListResponse,
    AdapterEvaluationResponse,
    AdapterMetadataListResponse,
    AdapterMetadataResponse,
    AdapterPromotionRequest,
    AdapterReviewListResponse,
    AdapterReviewRequest,
    AdapterReviewResponse,
    AdapterRollbackRequest,
    AdapterRollbackRetentionReleaseRequest,
    AdapterRollbackRetentionResponse,
    ChunkListResponse,
    ChunkResponse,
    DepartmentArchive,
    DepartmentListResponse,
    DepartmentResponse,
    DepartmentUpdate,
    DocumentListResponse,
    DocumentResponse,
    EvaluationRunCancelRequest,
    EvaluationRunCreateRequest,
    EvaluationRunListResponse,
    EvaluationRunResponse,
    EvaluationSuiteListResponse,
    EvaluationSuiteResponse,
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
    SftDatasetBuildCancelRequest,
    SftDatasetBuildListResponse,
    SftDatasetBuildResponse,
    SftDatasetBuildReviewRequest,
    SftSourceListResponse,
    SftSourceResponse,
    TrainingJobCancelRequest,
    TrainingJobCreateRequest,
    TrainingJobListResponse,
    TrainingJobResponse,
    TrainingJobReviewRequest,
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
from app.sft_request_body import (
    SFT_CANCEL_BODY_MAX_BYTES,
    SFT_REVIEW_BODY_MAX_BYTES,
    SftBodyError,
    read_bounded_sft_object,
)
from app.sft_services import (
    cancel_sft_build,
    enqueue_sft_build,
    list_sft_builds,
    list_sft_sources,
    read_sft_build,
    read_sft_source,
    review_sft_build,
)
from app.training_job_request_body import (
    TRAINING_JOB_BODY_MAX_BYTES,
    TRAINING_JOB_MUTATION_BODY_MAX_BYTES,
    TrainingJobBodyError,
    read_bounded_training_job_object,
)
from app.training_job_services import (
    cancel_training_job,
    enqueue_training_job,
    list_training_jobs,
    read_training_job,
    review_training_job,
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


async def _validated_evaluation_body(request: Request, model, *, maximum_bytes: int):
    try:
        payload = await read_bounded_evaluation_object(request, maximum_bytes=maximum_bytes)
    except EvaluationBodyError as error:
        raise HTTPException(error.status_code, error.detail) from None
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise HTTPException(422, "Invalid evaluation request") from None


async def _validated_sft_body(request: Request, model, *, maximum_bytes: int):
    try:
        payload = await read_bounded_sft_object(request, maximum_bytes=maximum_bytes)
    except SftBodyError as error:
        raise HTTPException(error.status_code, error.detail) from None
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise HTTPException(422, "Invalid SFT request") from None


async def _validated_training_job_body(request: Request, model, *, maximum_bytes: int):
    try:
        payload = await read_bounded_training_job_object(request, maximum_bytes=maximum_bytes)
    except TrainingJobBodyError as error:
        raise HTTPException(error.status_code, error.detail) from None
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise HTTPException(422, "Invalid training job request") from None


async def _require_empty_sft_body(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length is not None and (
        not content_length.isascii() or not content_length.isdecimal() or int(content_length) != 0
    ):
        raise HTTPException(400, "Invalid SFT request")
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(400, "Invalid SFT request")


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
            adapter_runtime=getattr(request.app.state, "adapter_runtime_client", None),
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


@router.get(
    "/departments/{department_id}/evaluation-suites",
    response_model=EvaluationSuiteListResponse,
    tags=["evaluations"],
)
def get_evaluation_suites(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> EvaluationSuiteListResponse:
    try:
        page = list_evaluation_suites(session, principal, request_scope, limit=limit, cursor=cursor)
        return EvaluationSuiteListResponse(items=list(page.items), next_cursor=page.next_cursor)
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/evaluation-suites/{suite_id}",
    response_model=EvaluationSuiteResponse,
    tags=["evaluations"],
)
def get_evaluation_suite(
    suite_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> EvaluationSuiteResponse:
    try:
        return EvaluationSuiteResponse.model_validate(
            read_evaluation_suite(session, principal, request_scope, suite_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/evaluation-suites/{suite_id}/runs",
    response_model=EvaluationRunResponse,
    status_code=202,
    tags=["evaluations"],
)
async def post_evaluation_run(
    suite_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> EvaluationRunResponse:
    await _validated_evaluation_body(
        request, EvaluationRunCreateRequest, maximum_bytes=MAX_RUN_BODY_BYTES
    )
    try:
        return EvaluationRunResponse.model_validate(
            enqueue_evaluation_run(
                session,
                principal,
                request_scope,
                suite_id,
                code_revision=request.app.state.settings.evaluation_code_revision,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/evaluation-runs",
    response_model=EvaluationRunListResponse,
    tags=["evaluations"],
)
def get_evaluation_runs(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> EvaluationRunListResponse:
    try:
        page = list_evaluation_runs(session, principal, request_scope, limit=limit, cursor=cursor)
        return EvaluationRunListResponse(items=list(page.items), next_cursor=page.next_cursor)
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/evaluation-runs/{run_id}",
    response_model=EvaluationRunResponse,
    tags=["evaluations"],
)
def get_evaluation_run(
    run_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> EvaluationRunResponse:
    try:
        return EvaluationRunResponse.model_validate(
            read_evaluation_run(session, principal, request_scope, run_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/evaluation-runs/{run_id}/cancel",
    response_model=EvaluationRunResponse,
    tags=["evaluations"],
)
async def post_evaluation_run_cancel(
    run_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> EvaluationRunResponse:
    body = await _validated_evaluation_body(
        request, EvaluationRunCancelRequest, maximum_bytes=MAX_CANCEL_BODY_BYTES
    )
    try:
        return EvaluationRunResponse.model_validate(
            cancel_evaluation_run(
                session,
                principal,
                request_scope,
                run_id,
                expected_version=body.expected_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/sft/sources",
    response_model=SftSourceListResponse,
    tags=["sft-datasets"],
)
def get_sft_sources(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SftSourceListResponse:
    try:
        rows = list_sft_sources(session, principal, request_scope, limit=limit, offset=offset)
        return SftSourceListResponse(
            items=[SftSourceResponse.model_validate(row) for row in rows],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/sft/sources/{source_bundle_id}",
    response_model=SftSourceResponse,
    tags=["sft-datasets"],
)
def get_sft_source(
    source_bundle_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> SftSourceResponse:
    try:
        return SftSourceResponse.model_validate(
            read_sft_source(session, principal, request_scope, source_bundle_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/sft/sources/{source_bundle_id}/builds",
    response_model=SftDatasetBuildResponse,
    status_code=202,
    tags=["sft-datasets"],
)
async def post_sft_build(
    source_bundle_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> SftDatasetBuildResponse:
    await _require_empty_sft_body(request)
    try:
        return SftDatasetBuildResponse.model_validate(
            enqueue_sft_build(
                session,
                principal,
                request_scope,
                source_bundle_id,
                code_revision=request.app.state.settings.sft_code_revision,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/sft/builds",
    response_model=SftDatasetBuildListResponse,
    tags=["sft-datasets"],
)
def get_sft_builds(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SftDatasetBuildListResponse:
    try:
        rows = list_sft_builds(session, principal, request_scope, limit=limit, offset=offset)
        return SftDatasetBuildListResponse(
            items=[SftDatasetBuildResponse.model_validate(row) for row in rows],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/sft/builds/{build_id}",
    response_model=SftDatasetBuildResponse,
    tags=["sft-datasets"],
)
def get_sft_build(
    build_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> SftDatasetBuildResponse:
    try:
        return SftDatasetBuildResponse.model_validate(
            read_sft_build(session, principal, request_scope, build_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/sft/builds/{build_id}/cancel",
    response_model=SftDatasetBuildResponse,
    tags=["sft-datasets"],
)
async def post_sft_build_cancel(
    build_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> SftDatasetBuildResponse:
    body = await _validated_sft_body(
        request, SftDatasetBuildCancelRequest, maximum_bytes=SFT_CANCEL_BODY_MAX_BYTES
    )
    try:
        return SftDatasetBuildResponse.model_validate(
            cancel_sft_build(
                session, principal, request_scope, build_id, expected_version=body.expected_version
            )
        )
    except ServiceError as error:
        _raise(error)


@router.patch(
    "/departments/{department_id}/sft/builds/{build_id}/review",
    response_model=SftDatasetBuildResponse,
    tags=["sft-datasets"],
)
async def patch_sft_build_review(
    build_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> SftDatasetBuildResponse:
    body = await _validated_sft_body(
        request, SftDatasetBuildReviewRequest, maximum_bytes=SFT_REVIEW_BODY_MAX_BYTES
    )
    try:
        return SftDatasetBuildResponse.model_validate(
            review_sft_build(
                session,
                principal,
                request_scope,
                build_id,
                action=body.action,
                expected_version=body.expected_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapters",
    response_model=AdapterMetadataListResponse,
    tags=["adapters"],
)
def get_adapters(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdapterMetadataListResponse:
    try:
        rows = list_adapters(session, principal, request_scope, limit=limit, offset=offset)
        return AdapterMetadataListResponse(
            items=[AdapterMetadataResponse.model_validate(row.public_data()) for row in rows],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapters/{adapter_id}",
    response_model=AdapterMetadataResponse,
    tags=["adapters"],
)
def get_adapter(
    adapter_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterMetadataResponse:
    try:
        return AdapterMetadataResponse.model_validate(
            read_adapter(session, principal, request_scope, adapter_id).public_data()
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/adapters/{adapter_id}/evaluations",
    response_model=AdapterEvaluationResponse,
    status_code=202,
    tags=["adapter-evaluations"],
)
async def post_adapter_evaluation(
    adapter_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterEvaluationResponse:
    body = await _validated_evaluation_body(
        request, AdapterEvaluationCreateRequest, maximum_bytes=MAX_RUN_BODY_BYTES
    )
    try:
        return AdapterEvaluationResponse.model_validate(
            enqueue_adapter_evaluation(
                session,
                principal,
                request_scope,
                adapter_id=adapter_id,
                suite_id=body.suite_id,
                expected_adapter_version=body.expected_adapter_version,
                code_revision=request.app.state.settings.evaluation_code_revision,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapters/{adapter_id}/evaluations",
    response_model=AdapterEvaluationListResponse,
    tags=["adapter-evaluations"],
)
def get_adapter_evaluations(
    adapter_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> AdapterEvaluationListResponse:
    try:
        page = list_adapter_evaluations(
            session,
            principal,
            request_scope,
            adapter_id=adapter_id,
            limit=limit,
            cursor=cursor,
        )
        return AdapterEvaluationListResponse(
            items=list(page.items), limit=limit, next_cursor=page.next_cursor
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapters/{adapter_id}/evaluations/{evaluation_id}",
    response_model=AdapterEvaluationResponse,
    tags=["adapter-evaluations"],
)
def get_adapter_evaluation(
    adapter_id: UUID,
    evaluation_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterEvaluationResponse:
    try:
        return AdapterEvaluationResponse.model_validate(
            read_adapter_evaluation(
                session,
                principal,
                request_scope,
                adapter_id=adapter_id,
                evaluation_id=evaluation_id,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/adapters/{adapter_id}/evaluations/{evaluation_id}/cancel",
    response_model=AdapterEvaluationResponse,
    tags=["adapter-evaluations"],
)
async def post_adapter_evaluation_cancel(
    adapter_id: UUID,
    evaluation_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterEvaluationResponse:
    body = await _validated_evaluation_body(
        request, AdapterEvaluationCancelRequest, maximum_bytes=MAX_CANCEL_BODY_BYTES
    )
    try:
        return AdapterEvaluationResponse.model_validate(
            cancel_adapter_evaluation(
                session,
                principal,
                request_scope,
                adapter_id=adapter_id,
                evaluation_id=evaluation_id,
                expected_version=body.expected_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.patch(
    "/departments/{department_id}/adapters/{adapter_id}/review",
    response_model=AdapterReviewResponse,
    tags=["adapter-governance"],
)
async def patch_adapter_review(
    adapter_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterReviewResponse:
    body = await _validated_evaluation_body(
        request, AdapterReviewRequest, maximum_bytes=MAX_RUN_BODY_BYTES
    )
    try:
        if body.action == "start":
            value = start_review(
                session,
                principal,
                request_scope,
                adapter_id=adapter_id,
                evaluation_id=body.evaluation_id,
                expected_adapter_version=body.expected_adapter_version,
                expected_evaluation_version=body.expected_evaluation_version,
            )
        else:
            value = transition_review(
                session,
                principal,
                request_scope,
                adapter_id=adapter_id,
                review_id=body.review_id,
                action=body.action,
                expected_adapter_version=body.expected_adapter_version,
                expected_review_version=body.expected_review_version,
            )
        return AdapterReviewResponse.model_validate(value)
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapters/{adapter_id}/reviews",
    response_model=AdapterReviewListResponse,
    tags=["adapter-governance"],
)
def get_adapter_reviews(
    adapter_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> AdapterReviewListResponse:
    try:
        page = list_reviews(
            session, principal, request_scope, adapter_id=adapter_id, limit=limit, cursor=cursor
        )
        return AdapterReviewListResponse(
            items=list(page.items), limit=page.limit, next_cursor=page.next_cursor
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapters/{adapter_id}/reviews/{review_id}",
    response_model=AdapterReviewResponse,
    tags=["adapter-governance"],
)
def get_adapter_review(
    adapter_id: UUID,
    review_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterReviewResponse:
    try:
        return AdapterReviewResponse.model_validate(
            read_review(
                session, principal, request_scope, adapter_id=adapter_id, review_id=review_id
            )
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/adapters/{adapter_id}/promote",
    response_model=AdapterDeploymentOperationResponse,
    status_code=202,
    tags=["adapter-governance"],
)
async def post_adapter_promote(
    adapter_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterDeploymentOperationResponse:
    body = await _validated_evaluation_body(
        request, AdapterPromotionRequest, maximum_bytes=MAX_RUN_BODY_BYTES
    )
    try:
        return AdapterDeploymentOperationResponse.model_validate(
            enqueue_promotion(
                session,
                principal,
                request_scope,
                adapter_id=adapter_id,
                review_id=body.review_id,
                expected_adapter_version=body.expected_adapter_version,
                expected_review_version=body.expected_review_version,
                expected_deployment_version=body.expected_deployment_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/adapters/rollback",
    response_model=AdapterDeploymentOperationResponse,
    status_code=202,
    tags=["adapter-governance"],
)
async def post_adapter_rollback(
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterDeploymentOperationResponse:
    body = await _validated_evaluation_body(
        request, AdapterRollbackRequest, maximum_bytes=MAX_RUN_BODY_BYTES
    )
    try:
        return AdapterDeploymentOperationResponse.model_validate(
            enqueue_rollback(
                session,
                principal,
                request_scope,
                target=body.target,
                adapter_id=body.adapter_id,
                expected_adapter_version=body.expected_adapter_version,
                retention_id=body.rollback_retention_id,
                expected_retention_version=body.expected_retention_version,
                expected_deployment_version=body.expected_deployment_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapter-deployment",
    response_model=AdapterDeploymentResponse,
    tags=["adapter-governance"],
)
def get_adapter_deployment(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterDeploymentResponse:
    try:
        return AdapterDeploymentResponse.model_validate(
            read_deployment(session, principal, request_scope)
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapter-deployment/operations",
    response_model=AdapterDeploymentOperationListResponse,
    tags=["adapter-governance"],
)
def get_adapter_deployment_operations(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdapterDeploymentOperationListResponse:
    try:
        return AdapterDeploymentOperationListResponse(
            items=list(
                list_operations(session, principal, request_scope, limit=limit, offset=offset)
            ),
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapter-deployment/operations/{operation_id}",
    response_model=AdapterDeploymentOperationResponse,
    tags=["adapter-governance"],
)
def get_adapter_deployment_operation(
    operation_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterDeploymentOperationResponse:
    try:
        return AdapterDeploymentOperationResponse.model_validate(
            read_operation(session, principal, request_scope, operation_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/adapter-deployment/operations/{operation_id}/cancel",
    response_model=AdapterDeploymentOperationResponse,
    tags=["adapter-governance"],
)
async def post_adapter_deployment_operation_cancel(
    operation_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterDeploymentOperationResponse:
    body = await _validated_evaluation_body(
        request, AdapterDeploymentCancelRequest, maximum_bytes=MAX_CANCEL_BODY_BYTES
    )
    try:
        return AdapterDeploymentOperationResponse.model_validate(
            cancel_operation(
                session,
                principal,
                request_scope,
                operation_id=operation_id,
                expected_version=body.expected_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/adapter-deployment/events",
    response_model=AdapterDeploymentEventListResponse,
    tags=["adapter-governance"],
)
def get_adapter_deployment_events(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdapterDeploymentEventListResponse:
    try:
        return AdapterDeploymentEventListResponse(
            items=list(list_events(session, principal, request_scope, limit=limit, offset=offset)),
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/adapters/{adapter_id}/rollback-retention/release",
    response_model=AdapterRollbackRetentionResponse,
    tags=["adapter-governance"],
)
async def post_adapter_rollback_retention_release(
    adapter_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> AdapterRollbackRetentionResponse:
    body = await _validated_evaluation_body(
        request, AdapterRollbackRetentionReleaseRequest, maximum_bytes=MAX_CANCEL_BODY_BYTES
    )
    try:
        return AdapterRollbackRetentionResponse.model_validate(
            release_rollback_retention(
                session,
                principal,
                request_scope,
                adapter_id=adapter_id,
                retention_id=body.rollback_retention_id,
                expected_adapter_version=body.expected_adapter_version,
                expected_retention_version=body.expected_retention_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/training/jobs",
    response_model=TrainingJobResponse,
    status_code=202,
    tags=["training-jobs"],
)
async def post_training_job(
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> TrainingJobResponse:
    body = await _validated_training_job_body(
        request, TrainingJobCreateRequest, maximum_bytes=TRAINING_JOB_BODY_MAX_BYTES
    )
    try:
        return TrainingJobResponse.model_validate(
            enqueue_training_job(
                session,
                principal,
                request_scope,
                body,
                code_revision=request.app.state.settings.training_job_code_revision,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/training/jobs",
    response_model=TrainingJobListResponse,
    tags=["training-jobs"],
)
def get_training_jobs(
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TrainingJobListResponse:
    try:
        rows = list_training_jobs(session, principal, request_scope, limit=limit, offset=offset)
        return TrainingJobListResponse(
            items=[TrainingJobResponse.model_validate(row) for row in rows],
            limit=limit,
            offset=offset,
        )
    except ServiceError as error:
        _raise(error)


@router.get(
    "/departments/{department_id}/training/jobs/{training_job_id}",
    response_model=TrainingJobResponse,
    tags=["training-jobs"],
)
def get_training_job(
    training_job_id: UUID,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> TrainingJobResponse:
    try:
        return TrainingJobResponse.model_validate(
            read_training_job(session, principal, request_scope, training_job_id)
        )
    except ServiceError as error:
        _raise(error)


@router.post(
    "/departments/{department_id}/training/jobs/{training_job_id}/cancel",
    response_model=TrainingJobResponse,
    tags=["training-jobs"],
)
async def post_training_job_cancel(
    training_job_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> TrainingJobResponse:
    body = await _validated_training_job_body(
        request, TrainingJobCancelRequest, maximum_bytes=TRAINING_JOB_MUTATION_BODY_MAX_BYTES
    )
    try:
        return TrainingJobResponse.model_validate(
            cancel_training_job(
                session,
                principal,
                request_scope,
                training_job_id,
                expected_version=body.expected_version,
            )
        )
    except ServiceError as error:
        _raise(error)


@router.patch(
    "/departments/{department_id}/training/jobs/{training_job_id}/review",
    response_model=TrainingJobResponse,
    tags=["training-jobs"],
)
async def patch_training_job_review(
    training_job_id: UUID,
    request: Request,
    session: DatabaseSession,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_authenticated_principal)],
    request_scope: Annotated[DepartmentRequestScope, Depends(require_path_department_selector)],
) -> TrainingJobResponse:
    body = await _validated_training_job_body(
        request, TrainingJobReviewRequest, maximum_bytes=TRAINING_JOB_MUTATION_BODY_MAX_BYTES
    )
    try:
        return TrainingJobResponse.model_validate(
            review_training_job(
                session,
                principal,
                request_scope,
                training_job_id,
                action=body.action,
                expected_version=body.expected_version,
            )
        )
    except ServiceError as error:
        _raise(error)
