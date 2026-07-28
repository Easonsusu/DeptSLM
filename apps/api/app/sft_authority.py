"""Exact, content-free Phase 10 authority snapshots.

An SFT source is bound to one precise succeeded Phase 6 publication.  A later
valid indexing publication is deliberately not interchangeable with the one
captured for the source or build.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentExtraction, DocumentVectorIndexing


class SftSourceAuthorityError(RuntimeError):
    """Content-free denial when a source reference no longer has exact authority."""


@dataclass(frozen=True, slots=True)
class SftAuthorityReference:
    department_id: UUID
    document_id: UUID
    document_version: int
    extraction_id: UUID
    extraction_version: int
    extraction_pipeline_version: str
    indexing_id: UUID
    indexing_version: int
    indexing_attempt_number: int
    vector_attempt_id: UUID
    embedding_pipeline_version: str
    embedding_model_id: str
    embedding_model_revision: str
    embedding_dimension: int
    vector_schema_version: str
    collection_identity: str
    chunk_id: UUID
    chunk_ordinal: int
    chunk_byte_size: int
    chunk_char_start: int
    chunk_char_end: int
    provenance_kind: str
    provenance_start: int
    provenance_end: int

    def private_value(self) -> dict[str, object]:
        return {
            "department_id": str(self.department_id),
            "document_id": str(self.document_id),
            "document_version": self.document_version,
            "extraction_id": str(self.extraction_id),
            "extraction_version": self.extraction_version,
            "extraction_pipeline_version": self.extraction_pipeline_version,
            "indexing_id": str(self.indexing_id),
            "indexing_version": self.indexing_version,
            "indexing_attempt_number": self.indexing_attempt_number,
            "vector_attempt_id": str(self.vector_attempt_id),
            "embedding_pipeline_version": self.embedding_pipeline_version,
            "embedding_model_id": self.embedding_model_id,
            "embedding_model_revision": self.embedding_model_revision,
            "embedding_dimension": self.embedding_dimension,
            "vector_schema_version": self.vector_schema_version,
            "collection_identity": self.collection_identity,
            "chunk_id": str(self.chunk_id),
            "chunk_ordinal": self.chunk_ordinal,
            "chunk_byte_size": self.chunk_byte_size,
            "chunk_char_start": self.chunk_char_start,
            "chunk_char_end": self.chunk_char_end,
            "provenance_kind": self.provenance_kind,
            "provenance_start": self.provenance_start,
            "provenance_end": self.provenance_end,
        }

    def provenance_value(self) -> dict[str, str]:
        return {
            "document_id": str(self.document_id),
            "extraction_id": str(self.extraction_id),
            "indexing_id": str(self.indexing_id),
            "chunk_id": str(self.chunk_id),
            "vector_attempt_id": str(self.vector_attempt_id),
        }


@dataclass(frozen=True, slots=True)
class SftAuthoritySnapshot:
    references: tuple[SftAuthorityReference, ...]
    fingerprint: str


def capture_source_authority(
    session: Session,
    department_id: UUID,
    source_chunk_ids: set[UUID],
    *,
    lock: bool = False,
) -> SftAuthoritySnapshot:
    """Capture one exact current succeeded publication for every source chunk."""

    if not source_chunk_ids:
        raise SftSourceAuthorityError()
    statement = (
        select(Document, DocumentExtraction, DocumentVectorIndexing, DocumentChunk)
        .join(
            DocumentExtraction,
            (DocumentExtraction.id == DocumentChunk.extraction_id)
            & (DocumentExtraction.document_id == DocumentChunk.document_id)
            & (DocumentExtraction.department_id == DocumentChunk.department_id),
        )
        .join(
            Document,
            (Document.id == DocumentChunk.document_id)
            & (Document.department_id == DocumentChunk.department_id),
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
            DocumentVectorIndexing.point_count == DocumentVectorIndexing.expected_chunk_count,
            DocumentVectorIndexing.vector_attempt_id.is_not(None),
        )
        .order_by(
            DocumentChunk.id,
            Document.id,
            DocumentExtraction.id,
            DocumentVectorIndexing.id,
        )
    )
    if lock:
        statement = statement.with_for_update()
    rows = session.execute(statement).all()
    by_chunk: dict[UUID, list[SftAuthorityReference]] = {}
    for document, extraction, indexing, chunk in rows:
        provenance_start = chunk.page_start if chunk.provenance_kind == "page" else chunk.line_start
        provenance_end = chunk.page_end if chunk.provenance_kind == "page" else chunk.line_end
        if provenance_start is None or provenance_end is None or indexing.vector_attempt_id is None:
            raise SftSourceAuthorityError()
        reference = SftAuthorityReference(
            department_id,
            document.id,
            document.version,
            extraction.id,
            extraction.version,
            extraction.pipeline_version,
            indexing.id,
            indexing.version,
            indexing.attempt_number,
            indexing.vector_attempt_id,
            indexing.embedding_pipeline_version,
            indexing.embedding_model_id,
            indexing.embedding_model_revision,
            indexing.embedding_dimension,
            indexing.vector_schema_version,
            getattr(indexing, "q" + "drant_collection"),
            chunk.id,
            chunk.ordinal,
            chunk.byte_size,
            chunk.char_start,
            chunk.char_end,
            chunk.provenance_kind,
            provenance_start,
            provenance_end,
        )
        by_chunk.setdefault(chunk.id, []).append(reference)
    if set(by_chunk) != source_chunk_ids or any(len(values) != 1 for values in by_chunk.values()):
        raise SftSourceAuthorityError()
    references = tuple(
        by_chunk[chunk_id][0]
        for chunk_id in sorted(source_chunk_ids, key=lambda value: value.bytes)
    )
    raw = json.dumps(
        [reference.private_value() for reference in references],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return SftAuthoritySnapshot(references, hashlib.sha256(raw).hexdigest())


def validate_source_authority(
    session: Session,
    department_id: UUID,
    source_chunk_ids: set[UUID],
    *,
    expected_fingerprint: str | None = None,
    lock: bool = False,
) -> SftAuthoritySnapshot:
    snapshot = capture_source_authority(session, department_id, source_chunk_ids, lock=lock)
    if expected_fingerprint is not None and snapshot.fingerprint != expected_fingerprint:
        raise SftSourceAuthorityError()
    return snapshot
