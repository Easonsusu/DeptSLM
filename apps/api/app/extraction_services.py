"""Department-authorized extraction queue and metadata services."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.document_repositories import get_scoped_document
from app.extraction_domain import CHUNKING_VERSION, NORMALIZATION_VERSION, PIPELINE_VERSION
from app.extraction_repositories import (
    get_scoped_extraction,
    list_scoped_chunks,
    list_scoped_extractions,
)
from app.models import DocumentExtraction
from app.services import (
    ALL_ROLES,
    ServiceError,
    append_mutation_audit,
    authorize_transaction,
    database_call,
)

ENQUEUE_ROLES = frozenset(
    (DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN, DepartmentRole.INSTRUCTOR)
)


def _stored_document(session, scope, document_id, *, lock=False):
    document = get_scoped_document(session, scope, document_id, lock=lock)
    if document is None:
        raise ServiceError(404, "Document not found")
    return document


def enqueue_extraction(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
) -> DocumentExtraction:
    def operation() -> DocumentExtraction:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ENQUEUE_ROLES,
            lock=True,
            audit_action="document.extraction.enqueue.authorization",
        )
        document = _stored_document(session, request_scope.department, document_id, lock=True)
        existing = session.execute(
            select(DocumentExtraction.id).where(
                DocumentExtraction.department_id == request_scope.department.value,
                DocumentExtraction.document_id == document_id,
                (
                    DocumentExtraction.status.in_(("queued", "running"))
                    | (
                        (DocumentExtraction.status == "succeeded")
                        & (DocumentExtraction.source_sha256 == document.sha256)
                        & (DocumentExtraction.pipeline_version == PIPELINE_VERSION)
                    )
                ),
            )
        ).first()
        if existing is not None:
            raise ServiceError(409, "Document extraction already exists")
        extraction = DocumentExtraction(
            department_id=request_scope.department.value,
            document_id=document.id,
            requested_by_user_id=authorization.identity.id,
            status="queued",
            pipeline_version=PIPELINE_VERSION,
            normalization_version=NORMALIZATION_VERSION,
            chunking_version=CHUNKING_VERSION,
            source_sha256=document.sha256,
            source_byte_size=document.byte_size,
            attempt_number=1,
            version=1,
        )
        session.add(extraction)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="document.extraction.enqueue",
            resource_type="document_extraction",
            resource_id=extraction.id,
        )
        session.flush()
        return extraction

    return database_call(operation)


def list_extractions(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    limit: int,
    offset: int,
) -> list[DocumentExtraction]:
    def operation() -> list[DocumentExtraction]:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="document.extraction.list",
        )
        _stored_document(session, request_scope.department, document_id)
        return list_scoped_extractions(
            session, request_scope.department, document_id, limit=limit, offset=offset
        )

    return database_call(operation)


def read_extraction(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    extraction_id: UUID,
) -> DocumentExtraction:
    def operation() -> DocumentExtraction:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="document.extraction.read",
        )
        _stored_document(session, request_scope.department, document_id)
        extraction = get_scoped_extraction(
            session, request_scope.department, document_id, extraction_id
        )
        if extraction is None:
            raise ServiceError(404, "Extraction not found")
        return extraction

    return database_call(operation)


def list_chunks(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    extraction_id: UUID,
    limit: int,
    offset: int,
):
    def operation():
        authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="document.extraction.chunks",
        )
        _stored_document(session, request_scope.department, document_id)
        extraction = get_scoped_extraction(
            session, request_scope.department, document_id, extraction_id
        )
        if extraction is None or extraction.status != "succeeded":
            raise ServiceError(404, "Extraction not found")
        return list_scoped_chunks(
            session,
            request_scope.department,
            document_id,
            extraction_id,
            limit=limit,
            offset=offset,
        )

    return database_call(operation)


def retry_extraction(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
    extraction_id: UUID,
) -> DocumentExtraction:
    def operation() -> DocumentExtraction:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            ENQUEUE_ROLES,
            lock=True,
            audit_action="document.extraction.retry.authorization",
        )
        document = _stored_document(session, request_scope.department, document_id, lock=True)
        previous = get_scoped_extraction(
            session, request_scope.department, document_id, extraction_id, lock=True
        )
        if previous is None or previous.status != "failed":
            raise ServiceError(409, "Extraction cannot be retried")
        if previous.source_sha256 != document.sha256:
            raise ServiceError(409, "Extraction source is no longer current")
        conflict = session.execute(
            select(DocumentExtraction.id).where(
                DocumentExtraction.department_id == request_scope.department.value,
                DocumentExtraction.document_id == document_id,
                (
                    DocumentExtraction.status.in_(("queued", "running"))
                    | (
                        (DocumentExtraction.status == "succeeded")
                        & (DocumentExtraction.source_sha256 == document.sha256)
                        & (DocumentExtraction.pipeline_version == PIPELINE_VERSION)
                    )
                ),
            )
        ).first()
        if conflict is not None:
            raise ServiceError(409, "Document extraction already exists")
        extraction = DocumentExtraction(
            department_id=request_scope.department.value,
            document_id=document.id,
            requested_by_user_id=authorization.identity.id,
            retry_of_id=previous.id,
            status="queued",
            pipeline_version=PIPELINE_VERSION,
            normalization_version=NORMALIZATION_VERSION,
            chunking_version=CHUNKING_VERSION,
            source_sha256=previous.source_sha256,
            source_byte_size=document.byte_size,
            attempt_number=previous.attempt_number + 1,
            version=1,
        )
        session.add(extraction)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="document.extraction.retry",
            resource_type="document_extraction",
            resource_id=extraction.id,
        )
        session.flush()
        return extraction

    return database_call(operation)
