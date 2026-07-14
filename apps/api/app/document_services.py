"""Department-scoped document metadata, upload finalization, and soft deletion."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.audit import AuditEvent, AuditResult
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope
from app.document_repositories import (
    get_scoped_document,
    list_scoped_documents,
    retained_document_bytes,
)
from app.document_storage import DocumentStorageError, StagedDocument
from app.document_upload import StreamResult, UploadMetadata
from app.models import Document
from app.services import (
    ALL_ROLES,
    ServiceError,
    append_mutation_audit,
    authorize_transaction,
    database_call,
)

UPLOAD_ROLES = frozenset(
    (DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN, DepartmentRole.INSTRUCTOR)
)
DELETE_ROLES = frozenset((DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN))


def emit_document_event(
    request_scope: DepartmentRequestScope,
    principal: AuthenticatedPrincipal,
    *,
    action: str,
    result: AuditResult,
    reason_code: str,
    resource_id: UUID | None = None,
) -> None:
    """Emit fixed-field process evidence without upload metadata or body content."""

    if request_scope.audit_sink is None:
        return
    try:
        request_scope.audit_sink.emit(
            AuditEvent(
                actor_subject=principal.subject,
                action=action,
                result=result,
                reason_code=reason_code,
                department_id=str(request_scope.department),
                correlation_id=request_scope.correlation_id,
                resource_id=str(resource_id) if resource_id else None,
            )
        )
    except Exception:
        # Process logging must not undo a committed database/filesystem result.
        return


def admit_document_upload(
    factory: sessionmaker[Session],
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
) -> None:
    """Perform a short admission check without retaining a transaction while streaming."""

    with factory() as session:
        try:
            authorize_transaction(
                session,
                principal,
                request_scope,
                UPLOAD_ROLES,
                lock=False,
                audit_action="document.upload.admission",
            )
        finally:
            session.rollback()


def finalize_document_upload(
    factory: sessionmaker[Session],
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    metadata: UploadMetadata,
    streamed: StreamResult,
    staged: StagedDocument,
    quota_bytes: int,
) -> Document:
    """Reauthorize, reserve quota, move bytes, and commit metadata plus audit atomically."""

    document_id = uuid4()
    try:
        with factory() as session:
            with session.begin():
                authorization = authorize_transaction(
                    session,
                    principal,
                    request_scope,
                    UPLOAD_ROLES,
                    lock=True,
                    audit_action="document.upload.finalization",
                )
                if (
                    retained_document_bytes(session, request_scope.department) + streamed.byte_size
                    > quota_bytes
                ):
                    emit_document_event(
                        request_scope,
                        principal,
                        action="document.upload.storage",
                        result=AuditResult.DENIED,
                        reason_code="quota_exceeded",
                    )
                    raise ServiceError(409, "Department document quota exceeded")
                staged.finalize(document_id)
                document = Document(
                    id=document_id,
                    department_id=request_scope.department.value,
                    uploaded_by_user_id=authorization.identity.id,
                    original_filename=metadata.original_filename,
                    media_type=metadata.media_type,
                    byte_size=streamed.byte_size,
                    sha256=streamed.sha256,
                    status="stored",
                    version=1,
                )
                session.add(document)
                session.flush()
                append_mutation_audit(
                    session,
                    actor=authorization.identity,
                    actor_subject=principal.subject,
                    request_scope=request_scope,
                    action="document.upload",
                    resource_type="document",
                    resource_id=document.id,
                )
                session.flush()
        staged.release()
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.storage",
            result=AuditResult.ALLOWED,
            reason_code="document_stored",
            resource_id=document_id,
        )
        return document
    except ServiceError as error:
        if not _compensate_safely(staged):
            emit_document_event(
                request_scope,
                principal,
                action="document.upload.storage",
                result=AuditResult.DENIED,
                reason_code="cleanup_failed",
            )
            raise ServiceError(503, "Document storage unavailable") from error
        raise
    except DocumentStorageError as error:
        _compensate_safely(staged)
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.storage",
            result=AuditResult.DENIED,
            reason_code="storage_unavailable",
        )
        raise ServiceError(503, "Document storage unavailable") from error
    except IntegrityError as error:
        _compensate_safely(staged)
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.database",
            result=AuditResult.DENIED,
            reason_code="database_unavailable",
        )
        raise ServiceError(503, "Database unavailable") from error
    except SQLAlchemyError as error:
        _compensate_safely(staged)
        emit_document_event(
            request_scope,
            principal,
            action="document.upload.database",
            result=AuditResult.DENIED,
            reason_code="database_unavailable",
        )
        raise ServiceError(503, "Database unavailable") from error
    except Exception:
        _compensate_safely(staged)
        raise


def _compensate_safely(staged: StagedDocument) -> bool:
    try:
        staged.compensate()
        return True
    except DocumentStorageError:
        return False


def list_documents(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    limit: int,
    offset: int,
) -> list[Document]:
    def operation() -> list[Document]:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="document.list",
        )
        return list_scoped_documents(session, request_scope.department, limit=limit, offset=offset)

    return database_call(operation)


def get_document(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
) -> Document:
    def operation() -> Document:
        authorize_transaction(
            session,
            principal,
            request_scope,
            ALL_ROLES,
            lock=False,
            audit_action="document.read",
        )
        document = get_scoped_document(session, request_scope.department, document_id)
        if document is None:
            raise ServiceError(404, "Document not found")
        return document

    return database_call(operation)


def delete_document(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    document_id: UUID,
) -> Document:
    def operation() -> Document:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            DELETE_ROLES,
            lock=True,
            audit_action="document.delete",
        )
        document = get_scoped_document(session, request_scope.department, document_id, lock=True)
        if document is None:
            raise ServiceError(404, "Document not found")
        document.status = "deleted"
        document.deleted_at = datetime.now(UTC)
        document.deleted_by_user_id = authorization.identity.id
        document.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="document.delete",
            resource_type="document",
            resource_id=document.id,
        )
        session.flush()
        return document

    return database_call(operation)
