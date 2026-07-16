"""Department-scoped vector-indexing metadata queries."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization import DepartmentScope
from app.models import DocumentVectorIndexing


def list_scoped_indexings(
    session: Session,
    scope: DepartmentScope,
    document_id: UUID,
    extraction_id: UUID,
    *,
    limit: int,
    offset: int,
) -> list[DocumentVectorIndexing]:
    return list(
        session.execute(
            select(DocumentVectorIndexing)
            .where(
                DocumentVectorIndexing.department_id == scope.value,
                DocumentVectorIndexing.document_id == document_id,
                DocumentVectorIndexing.extraction_id == extraction_id,
            )
            .order_by(DocumentVectorIndexing.created_at.desc(), DocumentVectorIndexing.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def get_scoped_indexing(
    session: Session,
    scope: DepartmentScope,
    document_id: UUID,
    extraction_id: UUID,
    indexing_id: UUID,
    *,
    lock: bool = False,
) -> DocumentVectorIndexing | None:
    statement = select(DocumentVectorIndexing).where(
        DocumentVectorIndexing.department_id == scope.value,
        DocumentVectorIndexing.document_id == document_id,
        DocumentVectorIndexing.extraction_id == extraction_id,
        DocumentVectorIndexing.id == indexing_id,
    )
    if lock:
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()
