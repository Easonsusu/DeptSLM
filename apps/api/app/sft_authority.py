"""Exact current source-chunk authority checks for Phase 10 SFT artifacts."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentExtraction, DocumentVectorIndexing


class SftSourceAuthorityError(RuntimeError):
    """Content-free denial when a supplied source reference is no longer authoritative."""


def validate_source_authority(
    session: Session, department_id: UUID, source_chunk_ids: set[UUID]
) -> None:
    """Require every persisted source chunk to have current exact same-department authority."""

    if not source_chunk_ids:
        raise SftSourceAuthorityError()
    rows = set(
        session.scalars(
            select(DocumentChunk.id)
            .join(
                Document,
                (Document.id == DocumentChunk.document_id)
                & (Document.department_id == DocumentChunk.department_id),
            )
            .join(
                DocumentExtraction,
                (DocumentExtraction.id == DocumentChunk.extraction_id)
                & (DocumentExtraction.document_id == DocumentChunk.document_id)
                & (DocumentExtraction.department_id == DocumentChunk.department_id),
            )
            .join(
                DocumentVectorIndexing,
                (DocumentVectorIndexing.document_id == DocumentChunk.document_id)
                & (DocumentVectorIndexing.extraction_id == DocumentChunk.extraction_id)
                & (DocumentVectorIndexing.department_id == DocumentChunk.department_id),
            )
            .where(
                DocumentChunk.id.in_(source_chunk_ids),
                DocumentChunk.department_id == department_id,
                Document.department_id == department_id,
                Document.status == "stored",
                DocumentExtraction.status == "succeeded",
                DocumentVectorIndexing.status == "succeeded",
            )
        )
    )
    if rows != source_chunk_ids:
        raise SftSourceAuthorityError()
