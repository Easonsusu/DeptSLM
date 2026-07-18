"""PostgreSQL claims and exact-attempt vector-index finalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.models import (
    Department,
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
    PersistentAuditEvent,
)
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    SAFE_VECTOR_INDEX_ERROR_CODES,
    VECTOR_SCHEMA_VERSION,
)
from deptslm_worker.artifact_reader import ArtifactExpectation, ExternalChunk
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError


class IndexQueueError(RuntimeError):
    def __init__(self, code: str = "database_unavailable") -> None:
        self.code = (
            code if code in SAFE_VECTOR_INDEX_ERROR_CODES else "database_unavailable"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ClaimedIndexJob:
    id: UUID
    department_id: UUID
    document_id: UUID
    extraction_id: UUID
    expected_chunk_count: int
    claim_token: UUID
    worker_id: UUID
    vector_attempt_id: UUID
    stale_vector_attempt_id: UUID | None


def claim_next(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int
) -> ClaimedIndexJob | None:
    try:
        with factory() as session, session.begin():
            row = session.execute(
                select(DocumentVectorIndexing)
                .where(
                    or_(
                        DocumentVectorIndexing.status == "queued",
                        (
                            (DocumentVectorIndexing.status == "running")
                            & (
                                DocumentVectorIndexing.lease_expires_at
                                <= func.clock_timestamp()
                            )
                        ),
                    )
                )
                .order_by(DocumentVectorIndexing.created_at, DocumentVectorIndexing.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            stale_attempt = row.vector_attempt_id if row.status == "running" else None
            claim_token = uuid4()
            vector_attempt_id = uuid4()
            row.status = "running"
            row.worker_id = worker_id
            row.claim_token = claim_token
            row.vector_attempt_id = vector_attempt_id
            row.claimed_at = now
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.started_at = now
            row.finished_at = None
            row.point_count = None
            row.error_code = None
            row.version += 1
            session.flush()
            return ClaimedIndexJob(
                id=row.id,
                department_id=row.department_id,
                document_id=row.document_id,
                extraction_id=row.extraction_id,
                expected_chunk_count=row.expected_chunk_count,
                claim_token=claim_token,
                worker_id=worker_id,
                vector_attempt_id=vector_attempt_id,
                stale_vector_attempt_id=stale_attempt,
            )
    except SQLAlchemyError as error:
        raise IndexQueueError() from error


def heartbeat(
    factory: sessionmaker[Session], job: ClaimedIndexJob, lease_seconds: int
) -> bool:
    try:
        with factory() as session, session.begin():
            result = session.execute(
                update(DocumentVectorIndexing)
                .where(*_owned_claim(job), _live_lease())
                .values(
                    lease_expires_at=func.clock_timestamp()
                    + timedelta(seconds=lease_seconds),
                    updated_at=func.clock_timestamp(),
                    version=DocumentVectorIndexing.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def renew_lease(
    factory: sessionmaker[Session], job: ClaimedIndexJob, lease_seconds: int
) -> None:
    """Renew an exact live claim while preserving database errors as database errors."""
    try:
        with factory() as session, session.begin():
            result = session.execute(
                update(DocumentVectorIndexing)
                .where(*_owned_claim(job), _live_lease(), *_fixed_contract(job))
                .values(
                    lease_expires_at=func.clock_timestamp()
                    + timedelta(seconds=lease_seconds),
                    updated_at=func.clock_timestamp(),
                    version=DocumentVectorIndexing.version + 1,
                )
            )
            if result.rowcount != 1:
                raise IndexQueueError("claim_lost")
    except IndexQueueError:
        raise
    except SQLAlchemyError as error:
        raise IndexQueueError("database_unavailable") from error


def require_live_claim(factory: sessionmaker[Session], job: ClaimedIndexJob) -> None:
    """Require exact PostgreSQL-server-time ownership before a Qdrant mutation."""
    try:
        with factory() as session:
            owned = session.execute(
                select(DocumentVectorIndexing.id).where(
                    *_owned_claim(job), _live_lease(), *_fixed_contract(job)
                )
            ).scalar_one_or_none()
            if owned is None:
                raise IndexQueueError("claim_lost")
    except IndexQueueError:
        raise
    except SQLAlchemyError as error:
        raise IndexQueueError("database_unavailable") from error


def requeue_owned(factory: sessionmaker[Session], job: ClaimedIndexJob) -> bool:
    try:
        with factory() as session, session.begin():
            result = session.execute(
                update(DocumentVectorIndexing)
                .where(*_owned_claim(job), _live_lease())
                .values(
                    status="queued",
                    worker_id=None,
                    claim_token=None,
                    vector_attempt_id=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    started_at=None,
                    finished_at=None,
                    point_count=None,
                    error_code=None,
                    updated_at=func.clock_timestamp(),
                    version=DocumentVectorIndexing.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def fail_owned(factory: sessionmaker[Session], job: ClaimedIndexJob, code: str) -> bool:
    if code not in SAFE_VECTOR_INDEX_ERROR_CODES:
        code = "embedding_failed"
    try:
        with factory() as session, session.begin():
            result = session.execute(
                update(DocumentVectorIndexing)
                .where(*_owned_claim(job), _live_lease())
                .values(
                    status="failed",
                    lease_expires_at=None,
                    finished_at=func.clock_timestamp(),
                    point_count=None,
                    error_code=code,
                    updated_at=func.clock_timestamp(),
                    version=DocumentVectorIndexing.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def load_artifact_expectation(
    factory: sessionmaker[Session], job: ClaimedIndexJob
) -> ArtifactExpectation:
    try:
        with factory() as session:
            document = session.execute(
                select(Document).where(
                    Document.id == job.document_id,
                    Document.department_id == job.department_id,
                    Document.status == "stored",
                )
            ).scalar_one_or_none()
            extraction = session.execute(
                select(DocumentExtraction).where(
                    DocumentExtraction.id == job.extraction_id,
                    DocumentExtraction.department_id == job.department_id,
                    DocumentExtraction.document_id == job.document_id,
                    DocumentExtraction.status == "succeeded",
                )
            ).scalar_one_or_none()
            if document is None:
                raise IndexQueueError("document_unavailable")
            if (
                extraction is None
                or extraction.chunk_count != job.expected_chunk_count
                or extraction.normalized_sha256 is None
                or extraction.normalized_byte_size is None
                or extraction.output_byte_size is None
            ):
                raise IndexQueueError("extraction_unavailable")
            return ArtifactExpectation(
                department_id=job.department_id,
                document_id=job.document_id,
                extraction_id=job.extraction_id,
                expected_chunk_count=job.expected_chunk_count,
                normalized_sha256=extraction.normalized_sha256,
                normalized_byte_size=extraction.normalized_byte_size,
                output_byte_size=extraction.output_byte_size,
            )
    except SQLAlchemyError as error:
        raise IndexQueueError() from error


def verify_chunk_batch(
    factory: sessionmaker[Session],
    job: ClaimedIndexJob,
    chunks: tuple[ExternalChunk, ...],
) -> tuple[UUID, ...]:
    if not chunks:
        raise IndexQueueError("chunk_artifact_mismatch")
    ordinals = [chunk.ordinal for chunk in chunks]
    try:
        with factory() as session:
            rows = (
                session.execute(
                    select(DocumentChunk)
                    .where(
                        DocumentChunk.department_id == job.department_id,
                        DocumentChunk.document_id == job.document_id,
                        DocumentChunk.extraction_id == job.extraction_id,
                        DocumentChunk.ordinal.in_(ordinals),
                    )
                    .order_by(DocumentChunk.ordinal)
                )
                .scalars()
                .all()
            )
            if len(rows) != len(chunks):
                raise IndexQueueError("chunk_artifact_mismatch")
            for row, chunk in zip(rows, chunks, strict=True):
                if (
                    row.ordinal != chunk.ordinal
                    or row.char_start != chunk.char_start
                    or row.char_end != chunk.char_end
                    or row.byte_size != chunk.byte_size
                    or row.content_sha256 != chunk.content_sha256
                    or row.provenance_kind != chunk.provenance_kind
                    or row.page_start != chunk.page_start
                    or row.page_end != chunk.page_end
                    or row.line_start != chunk.line_start
                    or row.line_end != chunk.line_end
                ):
                    raise IndexQueueError("chunk_artifact_mismatch")
            return tuple(row.id for row in rows)
    except SQLAlchemyError as error:
        raise IndexQueueError() from error


def finalize_success(
    factory: sessionmaker[Session],
    job: ClaimedIndexJob,
    qdrant: DepartmentQdrant,
) -> None:
    scope = DepartmentScope(job.department_id)
    try:
        with factory() as session, session.begin():
            department = session.execute(
                select(Department)
                .where(Department.id == job.department_id)
                .with_for_update()
            ).scalar_one_or_none()
            document = session.execute(
                select(Document)
                .where(
                    Document.id == job.document_id,
                    Document.department_id == job.department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            extraction = session.execute(
                select(DocumentExtraction)
                .where(
                    DocumentExtraction.id == job.extraction_id,
                    DocumentExtraction.department_id == job.department_id,
                    DocumentExtraction.document_id == job.document_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            indexing = session.execute(
                select(DocumentVectorIndexing)
                .where(
                    DocumentVectorIndexing.id == job.id,
                    DocumentVectorIndexing.department_id == job.department_id,
                    DocumentVectorIndexing.document_id == job.document_id,
                    DocumentVectorIndexing.extraction_id == job.extraction_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            if department is None or department.status != "active":
                raise IndexQueueError("document_unavailable")
            if document is None or document.status != "stored":
                raise IndexQueueError("document_unavailable")
            if extraction is None or extraction.status != "succeeded":
                raise IndexQueueError("extraction_unavailable")
            if not _valid_contract(indexing, job, now):
                raise IndexQueueError("claim_lost")
            if extraction.chunk_count != job.expected_chunk_count:
                raise IndexQueueError("chunk_artifact_mismatch")
            if (
                qdrant.count_attempt(
                    scope, job.id, job.vector_attempt_id, published=False
                )
                != job.expected_chunk_count
            ):
                raise IndexQueueError("qdrant_verification_failed")
            staged_ids = qdrant.inspect_attempt(
                scope,
                job.id,
                job.vector_attempt_id,
                published=False,
                maximum=job.expected_chunk_count,
            )
            if len(staged_ids) != job.expected_chunk_count or len(
                set(staged_ids)
            ) != len(staged_ids):
                raise IndexQueueError("qdrant_verification_failed")
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            if not _valid_contract(indexing, job, now):
                raise IndexQueueError("claim_lost")
            qdrant.activate_attempt(scope, job.id, job.vector_attempt_id)
            if (
                qdrant.count_attempt(
                    scope, job.id, job.vector_attempt_id, published=True
                )
                != job.expected_chunk_count
            ):
                raise IndexQueueError("qdrant_verification_failed")
            published_ids = qdrant.inspect_attempt(
                scope,
                job.id,
                job.vector_attempt_id,
                published=True,
                maximum=job.expected_chunk_count,
            )
            if set(published_ids) != set(staged_ids):
                raise IndexQueueError("qdrant_verification_failed")
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            if indexing.lease_expires_at is None or indexing.lease_expires_at <= now:
                raise IndexQueueError("claim_lost")
            indexing.status = "succeeded"
            indexing.lease_expires_at = None
            indexing.finished_at = now
            indexing.point_count = job.expected_chunk_count
            indexing.error_code = None
            indexing.version += 1
            session.add(
                PersistentAuditEvent(
                    actor_subject=None,
                    actor_user_id=indexing.requested_by_user_id,
                    department_id=job.department_id,
                    action="document.vector_index.complete",
                    resource_type="document_vector_indexing",
                    resource_id=str(job.id),
                    result="allowed",
                    reason_code="mutation_applied",
                )
            )
            session.flush()
    except IndexQueueError:
        raise
    except QdrantBoundaryError as error:
        raise IndexQueueError(error.code) from error
    except SQLAlchemyError as error:
        raise IndexQueueError() from error


def _owned_claim(job: ClaimedIndexJob):
    return (
        DocumentVectorIndexing.id == job.id,
        DocumentVectorIndexing.department_id == job.department_id,
        DocumentVectorIndexing.document_id == job.document_id,
        DocumentVectorIndexing.extraction_id == job.extraction_id,
        DocumentVectorIndexing.status == "running",
        DocumentVectorIndexing.worker_id == job.worker_id,
        DocumentVectorIndexing.claim_token == job.claim_token,
        DocumentVectorIndexing.vector_attempt_id == job.vector_attempt_id,
    )


def _live_lease():
    return DocumentVectorIndexing.lease_expires_at > func.clock_timestamp()


def _fixed_contract(job: ClaimedIndexJob):
    return (
        DocumentVectorIndexing.expected_chunk_count == job.expected_chunk_count,
        DocumentVectorIndexing.embedding_pipeline_version == EMBEDDING_PIPELINE_VERSION,
        DocumentVectorIndexing.embedding_model_id == EMBEDDING_MODEL_ID,
        DocumentVectorIndexing.embedding_model_revision == EMBEDDING_MODEL_REVISION,
        DocumentVectorIndexing.embedding_dimension == EMBEDDING_DIMENSION,
        DocumentVectorIndexing.distance == EMBEDDING_DISTANCE,
        DocumentVectorIndexing.vector_schema_version == VECTOR_SCHEMA_VERSION,
        DocumentVectorIndexing.qdrant_collection == QDRANT_COLLECTION,
    )


def _valid_contract(indexing, job: ClaimedIndexJob, now) -> bool:
    return bool(
        indexing is not None
        and indexing.id == job.id
        and indexing.department_id == job.department_id
        and indexing.document_id == job.document_id
        and indexing.extraction_id == job.extraction_id
        and indexing.status == "running"
        and indexing.worker_id == job.worker_id
        and indexing.claim_token == job.claim_token
        and indexing.vector_attempt_id == job.vector_attempt_id
        and indexing.lease_expires_at is not None
        and indexing.lease_expires_at > now
        and indexing.expected_chunk_count == job.expected_chunk_count
        and indexing.embedding_pipeline_version == EMBEDDING_PIPELINE_VERSION
        and indexing.embedding_model_id == EMBEDDING_MODEL_ID
        and indexing.embedding_model_revision == EMBEDDING_MODEL_REVISION
        and indexing.embedding_dimension == EMBEDDING_DIMENSION
        and indexing.distance == EMBEDDING_DISTANCE
        and indexing.vector_schema_version == VECTOR_SCHEMA_VERSION
        and indexing.qdrant_collection == QDRANT_COLLECTION
    )
