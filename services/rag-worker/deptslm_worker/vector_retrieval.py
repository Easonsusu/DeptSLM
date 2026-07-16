"""Internal retrieval primitive with mandatory PostgreSQL authority cross-checks."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
)
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    VECTOR_SCHEMA_VERSION,
)
from deptslm_worker.embedding import validate_vector
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError


class RetrievalBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorizedVectorHit:
    document_id: object
    extraction_id: object
    indexing_id: object
    chunk_ordinal: int
    score: float


def search_authorized(
    factory: sessionmaker[Session],
    qdrant: DepartmentQdrant,
    scope: DepartmentScope,
    query,
    *,
    limit: int,
) -> tuple[AuthorizedVectorHit, ...]:
    """Not an API: future retrieval code must supply authenticated scope."""
    if not isinstance(scope, DepartmentScope):
        raise RetrievalBoundaryError("invalid department scope")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
        raise RetrievalBoundaryError("invalid retrieval limit")
    vector = validate_vector(query)
    try:
        candidates = qdrant.search_published(scope, vector, limit=limit)
        with factory() as session:
            return tuple(_authorize_hit(session, scope, hit) for hit in candidates)
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
            DocumentVectorIndexing.embedding_pipeline_version == EMBEDDING_PIPELINE_VERSION,
            DocumentVectorIndexing.embedding_model_revision == EMBEDDING_MODEL_REVISION,
            DocumentVectorIndexing.embedding_dimension == EMBEDDING_DIMENSION,
            DocumentVectorIndexing.vector_schema_version == VECTOR_SCHEMA_VERSION,
            Document.id == hit.document_id,
            Document.status == "stored",
            DocumentExtraction.id == hit.extraction_id,
            DocumentExtraction.status == "succeeded",
            DocumentChunk.ordinal == hit.chunk_ordinal,
            DocumentChunk.id == hit.point_id,
        )
    ).one_or_none()
    if row is None:
        raise RetrievalBoundaryError("retrieval authority validation failed")
    return AuthorizedVectorHit(
        hit.document_id,
        hit.extraction_id,
        hit.indexing_id,
        hit.chunk_ordinal,
        hit.score,
    )
