"""One claimed extraction job from source verification through publication."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.extraction_domain import (
    CHUNKING_VERSION,
    NORMALIZATION_VERSION,
    PIPELINE_VERSION,
)
from app.models import Document
from deptslm_worker.chunking import ChunkingError, chunk_document
from deptslm_worker.domain import (
    CHUNKING_VERSION as WORKER_CHUNKING_VERSION,
)
from deptslm_worker.domain import (
    NORMALIZATION_VERSION as WORKER_NORMALIZATION_VERSION,
)
from deptslm_worker.domain import (
    PIPELINE_VERSION as WORKER_PIPELINE_VERSION,
)
from deptslm_worker.extractor import ExtractorError, run_extractor
from deptslm_worker.queue import (
    ClaimedJob,
    Publication,
    QueueError,
    fail_owned,
    finalize_success,
    heartbeat,
    requeue_owned,
)
from deptslm_worker.settings import WorkerSettings
from deptslm_worker.storage import (
    ExtractionStorage,
    ExtractionStorageError,
    SourceStorage,
)

LOGGER = logging.getLogger("deptslm.worker")


def process_job(
    factory: sessionmaker[Session],
    settings: WorkerSettings,
    job: ClaimedJob,
    should_stop: Callable[[], bool],
) -> bool:
    if (PIPELINE_VERSION, NORMALIZATION_VERSION, CHUNKING_VERSION) != (
        WORKER_PIPELINE_VERSION,
        WORKER_NORMALIZATION_VERSION,
        WORKER_CHUNKING_VERSION,
    ):
        fail_owned(factory, job, "parser_failed")
        return False
    staging = None
    try:
        if should_stop():
            requeue_owned(factory, job)
            return False
        media_type = _load_media_type(factory, job)
        department = DepartmentScope(job.department_id)
        with SourceStorage(settings.data_dir).open_verified(
            department,
            job.document_id,
            job.source_byte_size,
            job.source_sha256,
        ) as source:
            _event(job, "source_integrity", "allowed", "source_verified")
            staging = ExtractionStorage(settings.data_dir).create_staging(
                department, job.document_id, job.id, job.claim_token
            )
            result = run_extractor(
                source,
                staging,
                media_type=media_type,
                max_pages=settings.max_pdf_pages,
                max_bytes=settings.max_extracted_bytes,
                timeout_seconds=settings.extraction_timeout_seconds,
                heartbeat=lambda: heartbeat(
                    factory, job, settings.extraction_lease_seconds
                ),
                should_stop=should_stop,
            )
        _event(job, "parser", "allowed", "parser_completed")
        _event(job, "normalization", "allowed", "normalization_completed")
        if should_stop():
            raise ExtractorError("worker_shutdown")
        if not heartbeat(factory, job, settings.extraction_lease_seconds):
            raise ExtractorError("claim_lost")
        chunks = chunk_document(
            result.normalized,
            max_chars=settings.chunk_max_chars,
            overlap_chars=settings.chunk_overlap_chars,
            max_chunks=settings.max_chunks_per_document,
        )
        _event(job, "chunking", "allowed", "chunks_created")
        if should_stop():
            raise ExtractorError("worker_shutdown")
        normalized_bytes = result.normalized.text.encode("utf-8")
        normalized_sha256 = hashlib.sha256(normalized_bytes).hexdigest()
        chunks_payload = _chunks_jsonl(chunks)
        chunks_sha256 = hashlib.sha256(chunks_payload).hexdigest()
        staging.write_file("chunks.jsonl", chunks_payload)
        manifest = {
            "chunk_count": len(chunks),
            "chunking_version": CHUNKING_VERSION,
            "chunks_sha256": chunks_sha256,
            "department_id": str(job.department_id),
            "document_id": str(job.document_id),
            "extraction_id": str(job.id),
            "normalization_version": NORMALIZATION_VERSION,
            "normalized_byte_size": len(normalized_bytes),
            "normalized_sha256": normalized_sha256,
            "parser_name": result.parser_name,
            "parser_version": result.parser_version,
            "pipeline_version": PIPELINE_VERSION,
            "source_byte_size": job.source_byte_size,
            "source_sha256": job.source_sha256,
        }
        staging.write_file(
            "manifest.json",
            (
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode(),
        )
        publication = Publication(
            result.parser_name,
            result.parser_version,
            normalized_sha256,
            len(normalized_bytes),
            staging.output_size(),
            tuple(chunks),
        )
        # Detect host-side mutation that occurs after the parser received its
        # read-only descriptor but before metadata and output are finalized.
        with SourceStorage(settings.data_dir).open_verified(
            department,
            job.document_id,
            job.source_byte_size,
            job.source_sha256,
        ):
            pass
        _event(job, "source_integrity", "allowed", "source_reverified")
        if not heartbeat(factory, job, settings.extraction_lease_seconds):
            raise ExtractorError("claim_lost")
        finalize_success(
            factory,
            job,
            publication,
            staging,
            settings.department_extracted_quota_bytes,
        )
        _event(job, "finalization", "allowed", "extraction_succeeded")
        return True
    except ExtractorError as error:
        _cleanup(staging)
        if error.code == "worker_shutdown":
            requeue_owned(factory, job)
        else:
            fail_owned(factory, job, error.code)
        _event(job, "parser", "denied", error.code)
    except ChunkingError as error:
        _cleanup(staging)
        fail_owned(factory, job, error.code)
        _event(job, "chunking", "denied", error.code)
    except ExtractionStorageError as error:
        _cleanup(staging)
        fail_owned(factory, job, error.code)
        _event(job, "storage", "denied", error.code)
    except QueueError as error:
        _cleanup(staging)
        fail_owned(factory, job, error.code)
        _event(job, "finalization", "denied", error.code)
    except Exception:
        _cleanup(staging)
        fail_owned(factory, job, "parser_failed")
        _event(job, "processing", "denied", "parser_failed")
    return False


def _load_media_type(factory: sessionmaker[Session], job: ClaimedJob) -> str:
    try:
        with factory() as session:
            document = session.execute(
                select(Document).where(
                    Document.id == job.document_id,
                    Document.department_id == job.department_id,
                    Document.status == "stored",
                )
            ).scalar_one_or_none()
            if document is None:
                raise QueueError("document_unavailable")
            if (
                document.sha256 != job.source_sha256
                or document.byte_size != job.source_byte_size
            ):
                raise QueueError("source_integrity_mismatch")
            if document.media_type not in {
                "application/pdf",
                "text/plain",
                "text/markdown",
            }:
                raise QueueError("unsupported_media_type")
            return document.media_type
    except SQLAlchemyError as error:
        raise QueueError("database_unavailable") from error


def _chunks_jsonl(chunks) -> bytes:
    lines = []
    for chunk in chunks:
        payload = asdict(chunk)
        lines.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _cleanup(staging) -> None:
    if staging is None or staging.closed:
        return
    try:
        if staging.published:
            staging.compensate_final()
        else:
            staging.cleanup()
    except ExtractionStorageError:
        pass


def _event(job: ClaimedJob, action: str, result: str, reason: str) -> None:
    LOGGER.info(
        "extraction_event action=%s result=%s reason=%s department_id=%s resource_id=%s",
        action,
        result,
        reason,
        job.department_id,
        job.id,
    )
