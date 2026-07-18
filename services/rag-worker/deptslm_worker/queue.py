"""PostgreSQL queue claims, leases, and transactional publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.extraction_domain import PIPELINE_VERSION, SAFE_EXTRACTION_ERROR_CODES
from app.models import (
    Department,
    Document,
    DocumentChunk,
    DocumentExtraction,
    PersistentAuditEvent,
)
from deptslm_worker.chunking import Chunk
from deptslm_worker.storage import ExtractionStaging, ExtractionStorageError


class QueueError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = (
            code if code in SAFE_EXTRACTION_ERROR_CODES else "database_unavailable"
        )
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: UUID
    department_id: UUID
    document_id: UUID
    source_sha256: str
    source_byte_size: int
    claim_token: UUID
    worker_id: UUID
    pipeline_version: str
    stale_claim_token: UUID | None = None


@dataclass(frozen=True, slots=True)
class Publication:
    parser_name: str
    parser_version: str
    normalized_sha256: str
    normalized_byte_size: int
    output_byte_size: int
    chunks: tuple[Chunk, ...]


def claim_next(
    factory: sessionmaker[Session], worker_id: UUID, lease_seconds: int
) -> ClaimedJob | None:
    try:
        with factory() as session, session.begin():
            row = session.execute(
                select(DocumentExtraction)
                .where(
                    or_(
                        DocumentExtraction.status == "queued",
                        (
                            (DocumentExtraction.status == "running")
                            & (
                                DocumentExtraction.lease_expires_at
                                <= func.clock_timestamp()
                            )
                        ),
                    )
                )
                .order_by(DocumentExtraction.created_at, DocumentExtraction.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            stale_claim_token = row.claim_token if row.status == "running" else None
            token = uuid4()
            row.status = "running"
            row.worker_id = worker_id
            row.claim_token = token
            row.claimed_at = now
            row.started_at = now
            row.lease_expires_at = now + timedelta(seconds=lease_seconds)
            row.finished_at = None
            row.error_code = None
            row.parser_name = None
            row.parser_version = None
            row.normalized_sha256 = None
            row.normalized_byte_size = None
            row.output_byte_size = None
            row.chunk_count = None
            row.version += 1
            session.flush()
            return ClaimedJob(
                row.id,
                row.department_id,
                row.document_id,
                row.source_sha256,
                row.source_byte_size,
                token,
                worker_id,
                row.pipeline_version,
                stale_claim_token,
            )
    except SQLAlchemyError as error:
        raise QueueError("database_unavailable") from error


def heartbeat(
    factory: sessionmaker[Session], job: ClaimedJob, lease_seconds: int
) -> bool:
    try:
        with factory() as session, session.begin():
            result = session.execute(
                update(DocumentExtraction)
                .where(
                    DocumentExtraction.id == job.id,
                    DocumentExtraction.status == "running",
                    DocumentExtraction.worker_id == job.worker_id,
                    DocumentExtraction.claim_token == job.claim_token,
                    DocumentExtraction.lease_expires_at > func.clock_timestamp(),
                )
                .values(
                    lease_expires_at=func.clock_timestamp()
                    + timedelta(seconds=lease_seconds),
                    updated_at=func.clock_timestamp(),
                    version=DocumentExtraction.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def requeue_owned(factory: sessionmaker[Session], job: ClaimedJob) -> bool:
    try:
        with factory() as session, session.begin():
            result = session.execute(
                update(DocumentExtraction)
                .where(
                    DocumentExtraction.id == job.id,
                    DocumentExtraction.status == "running",
                    DocumentExtraction.worker_id == job.worker_id,
                    DocumentExtraction.claim_token == job.claim_token,
                    DocumentExtraction.lease_expires_at > func.clock_timestamp(),
                )
                .values(
                    status="queued",
                    worker_id=None,
                    claim_token=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    started_at=None,
                    finished_at=None,
                    parser_name=None,
                    parser_version=None,
                    error_code=None,
                    updated_at=func.clock_timestamp(),
                    version=DocumentExtraction.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def fail_owned(factory: sessionmaker[Session], job: ClaimedJob, code: str) -> bool:
    if code not in SAFE_EXTRACTION_ERROR_CODES:
        code = "parser_failed"
    try:
        with factory() as session, session.begin():
            result = session.execute(
                update(DocumentExtraction)
                .where(
                    DocumentExtraction.id == job.id,
                    DocumentExtraction.status == "running",
                    DocumentExtraction.worker_id == job.worker_id,
                    DocumentExtraction.claim_token == job.claim_token,
                    DocumentExtraction.lease_expires_at > func.clock_timestamp(),
                )
                .values(
                    status="failed",
                    lease_expires_at=None,
                    finished_at=func.clock_timestamp(),
                    error_code=code,
                    normalized_sha256=None,
                    normalized_byte_size=None,
                    output_byte_size=None,
                    chunk_count=None,
                    updated_at=func.clock_timestamp(),
                    version=DocumentExtraction.version + 1,
                )
            )
            return result.rowcount == 1
    except SQLAlchemyError:
        return False


def finalize_success(
    factory: sessionmaker[Session],
    job: ClaimedJob,
    publication: Publication,
    staging: ExtractionStaging,
    quota_bytes: int,
) -> None:
    published = False
    try:
        with factory() as session, session.begin():
            department = session.execute(
                select(Department)
                .where(Department.id == job.department_id)
                .with_for_update()
            ).scalar_one_or_none()
            if department is None or department.status != "active":
                raise QueueError("document_unavailable")
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
                    DocumentExtraction.id == job.id,
                    DocumentExtraction.department_id == job.department_id,
                    DocumentExtraction.document_id == job.document_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            now = session.execute(select(func.clock_timestamp())).scalar_one()
            if document is None or document.status != "stored":
                raise QueueError("document_unavailable")
            if document.sha256 != job.source_sha256:
                raise QueueError("source_integrity_mismatch")
            if (
                extraction is None
                or extraction.status != "running"
                or extraction.claim_token != job.claim_token
                or extraction.worker_id != job.worker_id
                or extraction.lease_expires_at is None
                or extraction.lease_expires_at <= now
                or extraction.pipeline_version != job.pipeline_version
                or extraction.pipeline_version != PIPELINE_VERSION
            ):
                raise QueueError("claim_lost")
            retained = session.execute(
                select(
                    func.coalesce(func.sum(DocumentExtraction.output_byte_size), 0)
                ).where(
                    DocumentExtraction.department_id == job.department_id,
                    DocumentExtraction.status == "succeeded",
                )
            ).scalar_one()
            if int(retained) + publication.output_byte_size > quota_bytes:
                raise QueueError("extraction_quota_exceeded")
            for chunk in publication.chunks:
                session.add(
                    DocumentChunk(
                        department_id=job.department_id,
                        document_id=job.document_id,
                        extraction_id=job.id,
                        ordinal=chunk.ordinal,
                        char_start=chunk.char_start,
                        char_end=chunk.char_end,
                        byte_size=chunk.byte_size,
                        content_sha256=chunk.content_sha256,
                        provenance_kind=chunk.provenance_kind,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                    )
                )
            session.flush()
            if (
                extraction.lease_expires_at
                <= session.execute(select(func.clock_timestamp())).scalar_one()
            ):
                raise QueueError("claim_lost")
            staging.publish()
            published = True
            if (
                extraction.lease_expires_at
                <= session.execute(select(func.clock_timestamp())).scalar_one()
            ):
                raise QueueError("claim_lost")
            extraction.status = "succeeded"
            extraction.parser_name = publication.parser_name
            extraction.parser_version = publication.parser_version
            extraction.normalized_sha256 = publication.normalized_sha256
            extraction.normalized_byte_size = publication.normalized_byte_size
            extraction.output_byte_size = publication.output_byte_size
            extraction.chunk_count = len(publication.chunks)
            extraction.lease_expires_at = None
            extraction.finished_at = now
            extraction.error_code = None
            extraction.version += 1
            session.add(
                PersistentAuditEvent(
                    actor_subject=None,
                    actor_user_id=extraction.requested_by_user_id,
                    department_id=job.department_id,
                    action="document.extraction.complete",
                    resource_type="document_extraction",
                    resource_id=str(job.id),
                    result="allowed",
                    reason_code="mutation_applied",
                )
            )
            session.flush()
        staging.close()
    except QueueError:
        if published or staging.published:
            _compensate(staging)
        else:
            _cleanup(staging)
        raise
    except (SQLAlchemyError, ExtractionStorageError) as error:
        if published or staging.published:
            _compensate(staging)
        else:
            _cleanup(staging)
        code = (
            "storage_unavailable"
            if isinstance(error, ExtractionStorageError)
            else "database_unavailable"
        )
        raise QueueError(code) from error


def _cleanup(staging: ExtractionStaging) -> None:
    try:
        staging.cleanup()
    except ExtractionStorageError:
        pass


def _compensate(staging: ExtractionStaging) -> None:
    try:
        staging.compensate_final()
    except ExtractionStorageError:
        pass
