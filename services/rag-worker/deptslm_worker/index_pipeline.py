"""One claimed Phase 6 indexing job from artifact proof to activation."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from deptslm_worker.artifact_reader import ArtifactError, Phase5ArtifactReader
from deptslm_worker.embedding import EmbeddingError, EmbeddingProcess
from deptslm_worker.index_queue import (
    ClaimedIndexJob,
    IndexQueueError,
    fail_owned,
    finalize_success,
    heartbeat,
    load_artifact_expectation,
    requeue_owned,
    verify_chunk_batch,
)
from deptslm_worker.index_settings import IndexSettings
from deptslm_worker.model_store import ModelStoreError, validate_model_store
from deptslm_worker.qdrant_adapter import (
    DepartmentQdrant,
    QdrantBoundaryError,
    VectorPoint,
)

LOGGER = logging.getLogger("deptslm.indexer")


def process_index_job(
    factory: sessionmaker[Session],
    settings: IndexSettings,
    qdrant: DepartmentQdrant,
    job: ClaimedIndexJob,
    should_stop: Callable[[], bool],
) -> bool:
    scope = DepartmentScope(job.department_id)
    try:
        qdrant.verify_collection()
        if job.stale_vector_attempt_id is not None:
            _delete_attempt(qdrant, scope, job.id, job.stale_vector_attempt_id)
            _event(job, "stale_attempt_cleanup", "allowed", "exact_attempt_deleted")
        if should_stop():
            _shutdown(factory, qdrant, scope, job)
            return False
        expectation = load_artifact_expectation(factory, job)
        if settings.embedding_provider == "real":
            model_root = validate_model_store(settings.data_dir).path
        else:
            model_root = settings.data_dir / "model_cache"
        processed = 0
        with (
            Phase5ArtifactReader(settings.data_dir, scope, expectation) as artifacts,
            EmbeddingProcess(
                model_root,
                provider=settings.embedding_provider,
                environment=settings.environment,
                timeout_seconds=settings.embedding_timeout_seconds,
                heartbeat=lambda: heartbeat(factory, job, settings.lease_seconds),
                should_stop=should_stop,
            ) as embeddings,
        ):
            _event(job, "model_loading", "allowed", "embedding_process_started")
            batch = []
            batch_characters = 0
            for chunk in artifacts.iter_chunks():
                if batch and (
                    len(batch) >= settings.batch_size
                    or batch_characters + len(chunk.text) > settings.max_batch_chars
                ):
                    processed += _publish_batch(
                        factory, settings, qdrant, embeddings, scope, job, tuple(batch)
                    )
                    batch.clear()
                    batch_characters = 0
                if len(chunk.text) > settings.max_batch_chars:
                    raise ArtifactError("chunk_artifact_mismatch")
                batch.append(chunk)
                batch_characters += len(chunk.text)
            if batch:
                processed += _publish_batch(
                    factory, settings, qdrant, embeddings, scope, job, tuple(batch)
                )
        if processed != job.expected_chunk_count:
            raise IndexQueueError("qdrant_verification_failed")
        _event(job, "artifact_validation", "allowed", "artifact_verified")
        _event(job, "staged_verification", "allowed", "point_count_matched")
        if should_stop():
            _shutdown(factory, qdrant, scope, job)
            return False
        if not heartbeat(factory, job, settings.lease_seconds):
            raise IndexQueueError("claim_lost")
        _event(job, "activation", "allowed", "activation_requested")
        finalize_success(factory, job, qdrant)
        _event(job, "activation", "allowed", "exact_attempt_activated")
        _event(job, "finalization", "allowed", "vector_index_succeeded")
        return True
    except (
        ArtifactError,
        EmbeddingError,
        IndexQueueError,
        QdrantBoundaryError,
    ) as error:
        code = error.code
    except ModelStoreError:
        code = "embedding_model_unavailable"
    except Exception:
        code = "embedding_failed"
    cleanup_code = _cleanup_current(qdrant, scope, job)
    if cleanup_code is not None:
        code = cleanup_code
    if code == "worker_shutdown" and cleanup_code is None:
        requeue_owned(factory, job)
    else:
        fail_owned(factory, job, code)
    _event(job, "processing", "denied", code)
    return False


def _publish_batch(
    factory: sessionmaker[Session],
    settings: IndexSettings,
    qdrant: DepartmentQdrant,
    embeddings: EmbeddingProcess,
    scope: DepartmentScope,
    job: ClaimedIndexJob,
    chunks,
) -> int:
    if not heartbeat(factory, job, settings.lease_seconds):
        raise IndexQueueError("claim_lost")
    if any(not chunk.text for chunk in chunks):
        raise ArtifactError("chunk_artifact_mismatch")
    chunk_ids = verify_chunk_batch(factory, job, chunks)
    vectors = embeddings.embed([chunk.text for chunk in chunks])
    if not heartbeat(factory, job, settings.lease_seconds):
        raise IndexQueueError("claim_lost")
    _event(job, "batch_embedding", "allowed", "vectors_validated")
    points = tuple(
        VectorPoint(
            chunk_id=chunk_id,
            document_id=job.document_id,
            extraction_id=job.extraction_id,
            indexing_id=job.id,
            vector_attempt_id=job.vector_attempt_id,
            chunk_ordinal=chunk.ordinal,
            provenance_kind=chunk.provenance_kind,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            vector=vector,
        )
        for chunk_id, chunk, vector in zip(chunk_ids, chunks, vectors, strict=True)
    )
    qdrant.upsert_staging(scope, points)
    _event(job, "staging_write", "allowed", "batch_written")
    if not heartbeat(factory, job, settings.lease_seconds):
        raise IndexQueueError("claim_lost")
    return len(points)


def _shutdown(
    factory: sessionmaker[Session],
    qdrant: DepartmentQdrant,
    scope: DepartmentScope,
    job: ClaimedIndexJob,
) -> None:
    cleanup_code = _cleanup_current(qdrant, scope, job)
    if cleanup_code is not None:
        fail_owned(factory, job, cleanup_code)
        raise IndexQueueError(cleanup_code)
    requeue_owned(factory, job)


def _cleanup_current(
    qdrant: DepartmentQdrant, scope: DepartmentScope, job: ClaimedIndexJob
) -> str | None:
    try:
        _delete_attempt(qdrant, scope, job.id, job.vector_attempt_id)
        return None
    except QdrantBoundaryError:
        return "qdrant_cleanup_failed"


def _delete_attempt(
    qdrant: DepartmentQdrant,
    scope: DepartmentScope,
    indexing_id,
    vector_attempt_id,
) -> None:
    last_error = None
    for attempt in range(3):
        try:
            qdrant.delete_attempt(scope, indexing_id, vector_attempt_id)
            return
        except QdrantBoundaryError as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.05)
    raise last_error or QdrantBoundaryError("qdrant_cleanup_failed")


def _event(job: ClaimedIndexJob, action: str, result: str, reason: str) -> None:
    LOGGER.info(
        "vector_index_event action=%s result=%s reason=%s department_id=%s resource_id=%s",
        action,
        result,
        reason,
        job.department_id,
        job.id,
    )
