"""Internal retrieval primitive with mandatory PostgreSQL authority cross-checks."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.extraction_domain import CHUNKING_VERSION, NORMALIZATION_VERSION, PIPELINE_VERSION
from app.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
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
from deptslm_worker.embedding import validate_vector
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError


class RetrievalBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizedVectorHit:
    document_id: UUID
    extraction_id: UUID
    indexing_id: UUID
    chunk_ordinal: int
    score: float
    chunk_id: UUID
    vector_attempt_id: UUID
    original_filename: str
    extraction_pipeline_version: str
    normalization_version: str
    chunking_version: str
    extraction_chunk_count: int
    normalized_sha256: str
    normalized_byte_size: int
    output_byte_size: int
    indexing_expected_chunk_count: int
    indexing_point_count: int
    embedding_pipeline_version: str
    embedding_model_id: str
    embedding_model_revision: str
    embedding_dimension: int
    distance: str
    vector_schema_version: str
    qdrant_collection: str
    chunk_char_start: int
    chunk_char_end: int
    chunk_byte_size: int
    chunk_content_sha256: str
    provenance_kind: str
    page_start: int | None
    page_end: int | None
    line_start: int | None
    line_end: int | None


@dataclass(frozen=True, slots=True)
class AuthorizedSearchResult:
    candidate_count: int
    hits: tuple[AuthorizedVectorHit, ...]


def search_authorized(
    factory: sessionmaker[Session],
    qdrant: DepartmentQdrant,
    scope: DepartmentScope,
    query,
    *,
    limit: int,
) -> tuple[AuthorizedVectorHit, ...]:
    """Not an API: retrieval callers must supply an authenticated typed scope."""
    return search_authorized_result(factory, qdrant, scope, query, limit=limit).hits


def search_authorized_result(
    factory: sessionmaker[Session],
    qdrant: DepartmentQdrant,
    scope: DepartmentScope,
    query,
    *,
    limit: int,
) -> AuthorizedSearchResult:
    """Search the fixed Qdrant boundary and cross-check every hit in PostgreSQL."""
    if not isinstance(scope, DepartmentScope):
        raise RetrievalBoundaryError("invalid department scope")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
        raise RetrievalBoundaryError("invalid retrieval limit")
    vector = validate_vector(query)
    try:
        candidates = qdrant.search_published(scope, vector, limit=limit)
        with factory() as session:
            hits = tuple(_authorize_hit(session, scope, hit) for hit in candidates)
            return AuthorizedSearchResult(len(candidates), hits)
    except (SQLAlchemyError, QdrantBoundaryError, ValueError, TypeError) as error:
        raise RetrievalBoundaryError("retrieval authority validation failed") from error


def _authorize_hit(session, scope, hit) -> AuthorizedVectorHit:
    row = session.execute(
        select(DocumentVectorIndexing, Document, DocumentExtraction, DocumentChunk)
        .join(
            Document,
            (Document.id == DocumentVectorIndexing.document_id)
            & (Document.department_id == DocumentVectorIndexing.department_id),
        )
        .join(
            DocumentExtraction,
            (DocumentExtraction.id == DocumentVectorIndexing.extraction_id)
            & (DocumentExtraction.department_id == DocumentVectorIndexing.department_id)
            & (DocumentExtraction.document_id == DocumentVectorIndexing.document_id),
        )
        .join(
            DocumentChunk,
            (DocumentChunk.extraction_id == DocumentVectorIndexing.extraction_id)
            & (DocumentChunk.department_id == DocumentVectorIndexing.department_id)
            & (DocumentChunk.document_id == DocumentVectorIndexing.document_id),
        )
        .where(
            DocumentVectorIndexing.department_id == scope.value,
            DocumentVectorIndexing.id == hit.indexing_id,
            DocumentVectorIndexing.status == "succeeded",
            DocumentVectorIndexing.vector_attempt_id == hit.vector_attempt_id,
            DocumentVectorIndexing.point_count == DocumentVectorIndexing.expected_chunk_count,
            DocumentVectorIndexing.expected_chunk_count == DocumentExtraction.chunk_count,
            DocumentVectorIndexing.embedding_pipeline_version == EMBEDDING_PIPELINE_VERSION,
            DocumentVectorIndexing.embedding_model_id == EMBEDDING_MODEL_ID,
            DocumentVectorIndexing.embedding_model_revision == EMBEDDING_MODEL_REVISION,
            DocumentVectorIndexing.embedding_dimension == EMBEDDING_DIMENSION,
            DocumentVectorIndexing.distance == EMBEDDING_DISTANCE,
            DocumentVectorIndexing.vector_schema_version == VECTOR_SCHEMA_VERSION,
            DocumentVectorIndexing.qdrant_collection == QDRANT_COLLECTION,
            Document.id == hit.document_id,
            Document.status == "stored",
            DocumentExtraction.id == hit.extraction_id,
            DocumentExtraction.status == "succeeded",
            DocumentExtraction.pipeline_version == PIPELINE_VERSION,
            DocumentExtraction.normalization_version == NORMALIZATION_VERSION,
            DocumentExtraction.chunking_version == CHUNKING_VERSION,
            DocumentChunk.ordinal == hit.chunk_ordinal,
            DocumentChunk.id == hit.point_id,
        )
    ).one_or_none()
    if row is None:
        raise RetrievalBoundaryError("retrieval authority validation failed")
    _indexing, document, extraction, chunk = row
    return AuthorizedVectorHit(
        document_id=hit.document_id,
        extraction_id=hit.extraction_id,
        indexing_id=hit.indexing_id,
        chunk_ordinal=hit.chunk_ordinal,
        score=hit.score,
        chunk_id=hit.point_id,
        vector_attempt_id=hit.vector_attempt_id,
        original_filename=document.original_filename,
        extraction_pipeline_version=extraction.pipeline_version,
        normalization_version=extraction.normalization_version,
        chunking_version=extraction.chunking_version,
        extraction_chunk_count=extraction.chunk_count,
        normalized_sha256=extraction.normalized_sha256,
        normalized_byte_size=extraction.normalized_byte_size,
        output_byte_size=extraction.output_byte_size,
        indexing_expected_chunk_count=_indexing.expected_chunk_count,
        indexing_point_count=_indexing.point_count,
        embedding_pipeline_version=_indexing.embedding_pipeline_version,
        embedding_model_id=_indexing.embedding_model_id,
        embedding_model_revision=_indexing.embedding_model_revision,
        embedding_dimension=_indexing.embedding_dimension,
        distance=_indexing.distance,
        vector_schema_version=_indexing.vector_schema_version,
        qdrant_collection=_indexing.qdrant_collection,
        chunk_char_start=chunk.char_start,
        chunk_char_end=chunk.char_end,
        chunk_byte_size=chunk.byte_size,
        chunk_content_sha256=chunk.content_sha256,
        provenance_kind=chunk.provenance_kind,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
    )
