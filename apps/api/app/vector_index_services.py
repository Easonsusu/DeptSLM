"""Department-authorized vector-indexing queue and metadata services."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.document_repositories import get_scoped_document
from app.extraction_repositories import get_scoped_extraction
from app.models import DocumentVectorIndexing
from app.services import (
    ALL_ROLES,
    ServiceError,
    append_mutation_audit,
    authorize_transaction,
    database_call,
)
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    VECTOR_SCHEMA_VERSION,
)
from app.vector_index_repositories import get_scoped_indexing, list_scoped_indexings

ENQUEUE_ROLES = frozenset(
    (DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN, DepartmentRole.INSTRUCTOR)
)


def _stored_document_and_extraction(session, scope, document_id, extraction_id, *, lock=False):
    document = get_scoped_document(session, scope, document_id, lock=lock)
    extraction = get_scoped_extraction(session, scope, document_id, extraction_id, lock=lock)
    if document is None:
        raise ServiceError(404, "Document not found")
    if extraction is None or extraction.status != "succeeded" or not extraction.chunk_count:
        raise ServiceError(404, "Extraction not found")
    return document, extraction


def _conflict_exists(session: Session, department_id: UUID, extraction_id: UUID) -> bool:
    return (
        session.execute(
            select(DocumentVectorIndexing.id).where(
                DocumentVectorIndexing.department_id == department_id,
                DocumentVectorIndexing.extraction_id == extraction_id,
                (
                    DocumentVectorIndexing.status.in_(("queued", "running"))
                    | (
                        (DocumentVectorIndexing.status == "succeeded")
                        & (
                            DocumentVectorIndexing.embedding_model_revision
                            == EMBEDDING_MODEL_REVISION
                        )
                        & (DocumentVectorIndexing.embedding_dimension == EMBEDDING_DIMENSION)
                        & (DocumentVectorIndexing.vector_schema_version == VECTOR_SCHEMA_VERSION)
                    )
                ),
            )
        ).first()
        is not None
    )


def _new_indexing(*, authorization, request_scope, extraction, retry_of=None):
    return DocumentVectorIndexing(
        department_id=request_scope.department.value,
        document_id=extraction.document_id,
        extraction_id=extraction.id,
        requested_by_user_id=authorization.identity.id,
        retry_of_id=retry_of.id if retry_of else None,
        status="queued",
        embedding_pipeline_version=EMBEDDING_PIPELINE_VERSION,
        embedding_model_id=EMBEDDING_MODEL_ID,
        embedding_model_revision=EMBEDDING_MODEL_REVISION,
        embedding_dimension=EMBEDDING_DIMENSION,
        distance=EMBEDDING_DISTANCE,
        vector_schema_version=VECTOR_SCHEMA_VERSION,
        qdrant_collection=QDRANT_COLLECTION,
        expected_chunk_count=extraction.chunk_count,
        attempt_number=(retry_of.attempt_number + 1) if retry_of else 1,
        version=1,
    )


def enqueue_indexing(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    extraction_id: UUID,
) -> DocumentVectorIndexing:
    def operation() -> DocumentVectorIndexing:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ENQUEUE_ROLES,
            lock=True,
            audit_action="document.vector_index.enqueue.authorization",
        )
        _document, extraction = _stored_document_and_extraction(
            session, request_scope.department, document_id, extraction_id, lock=True
        )
        if _conflict_exists(session, request_scope.department.value, extraction.id):
            raise ServiceError(409, "Document vector indexing already exists")
        indexing = _new_indexing(
            authorization=authorization, request_scope=request_scope, extraction=extraction
        )
        session.add(indexing)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="document.vector_index.enqueue",
            resource_type="document_vector_indexing",
            resource_id=indexing.id,
        )
        session.flush()
        return indexing

    return database_call(operation)


def list_indexings(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    extraction_id: UUID,
    limit: int,
    offset: int,
) -> list[DocumentVectorIndexing]:
    def operation() -> list[DocumentVectorIndexing]:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="document.vector_index.list",
        )
        _stored_document_and_extraction(
            session, request_scope.department, document_id, extraction_id
        )
        return list_scoped_indexings(
            session,
            request_scope.department,
            document_id,
            extraction_id,
            limit=limit,
            offset=offset,
        )

    return database_call(operation)


def read_indexing(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    extraction_id: UUID,
    indexing_id: UUID,
) -> DocumentVectorIndexing:
    def operation() -> DocumentVectorIndexing:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="document.vector_index.read",
        )
        _stored_document_and_extraction(
            session, request_scope.department, document_id, extraction_id
        )
        indexing = get_scoped_indexing(
            session,
            request_scope.department,
            document_id,
            extraction_id,
            indexing_id,
        )
        if indexing is None:
            raise ServiceError(404, "Vector indexing not found")
        return indexing

    return database_call(operation)


def retry_indexing(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    extraction_id: UUID,
    indexing_id: UUID,
) -> DocumentVectorIndexing:
    def operation() -> DocumentVectorIndexing:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ENQUEUE_ROLES,
            lock=True,
            audit_action="document.vector_index.retry.authorization",
        )
        _document, extraction = _stored_document_and_extraction(
            session, request_scope.department, document_id, extraction_id, lock=True
        )
        previous = get_scoped_indexing(
            session,
            request_scope.department,
            document_id,
            extraction_id,
            indexing_id,
            lock=True,
        )
        if previous is None or previous.status != "failed":
            raise ServiceError(409, "Vector indexing cannot be retried")
        if _conflict_exists(session, request_scope.department.value, extraction.id):
            raise ServiceError(409, "Document vector indexing already exists")
        indexing = _new_indexing(
            authorization=authorization,
            request_scope=request_scope,
            extraction=extraction,
            retry_of=previous,
        )
        session.add(indexing)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="document.vector_index.retry",
            resource_type="document_vector_indexing",
            resource_id=indexing.id,
        )
        session.flush()
        return indexing

    return database_call(operation)
