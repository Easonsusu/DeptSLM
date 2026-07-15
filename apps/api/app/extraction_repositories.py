"""Department-scoped extraction and chunk metadata queries."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization import DepartmentScope
from app.models import DocumentChunk, DocumentExtraction


def list_scoped_extractions(
    session: Session,
    scope: DepartmentScope,
    document_id: UUID,
    *,
    limit: int,
    offset: int,
) -> list[DocumentExtraction]:
    return list(
        session.execute(
            select(DocumentExtraction)
            .where(
                DocumentExtraction.department_id == scope.value,
                DocumentExtraction.document_id == document_id,
            )
            .order_by(DocumentExtraction.created_at.desc(), DocumentExtraction.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def get_scoped_extraction(
    session: Session,
    scope: DepartmentScope,
    document_id: UUID,
    extraction_id: UUID,
    *,
    lock: bool = False,
) -> DocumentExtraction | None:
    statement = select(DocumentExtraction).where(
        DocumentExtraction.department_id == scope.value,
        DocumentExtraction.document_id == document_id,
        DocumentExtraction.id == extraction_id,
    )
    if lock:
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def list_scoped_chunks(
    session: Session,
    scope: DepartmentScope,
    document_id: UUID,
    extraction_id: UUID,
    *,
    limit: int,
    offset: int,
) -> list[DocumentChunk]:
    return list(
        session.execute(
            select(DocumentChunk)
            .where(
                DocumentChunk.department_id == scope.value,
                DocumentChunk.document_id == document_id,
                DocumentChunk.extraction_id == extraction_id,
            )
            .order_by(DocumentChunk.ordinal)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
