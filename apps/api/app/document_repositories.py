"""Department-scoped document metadata queries."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.authorization import DepartmentScope
from app.models import Document


def list_scoped_documents(
    session: Session, scope: DepartmentScope, *, limit: int, offset: int
) -> list[Document]:
    return list(
        session.execute(
            select(Document)
            .where(Document.department_id == scope.value, Document.status == "stored")
            .order_by(Document.created_at.desc(), Document.id)
            .limit(limit)
            .offset(offset)
        ).scalars()
    )


def get_scoped_document(
    session: Session,
    scope: DepartmentScope,
    document_id: UUID,
    *,
    lock: bool = False,
) -> Document | None:
    statement = select(Document).where(
        Document.department_id == scope.value,
        Document.id == document_id,
        Document.status == "stored",
    )
    if lock:
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def retained_document_bytes(session: Session, scope: DepartmentScope) -> int:
    """Count stored and soft-deleted bytes because physical sources are retained."""

    value = session.execute(
        select(func.coalesce(func.sum(Document.byte_size), 0)).where(
            Document.department_id == scope.value
        )
    ).scalar_one()
    return int(value)
