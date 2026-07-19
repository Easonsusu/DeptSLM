"""One-turn department-scoped grounded-answer orchestration."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from deptslm_worker.embedding import EmbeddingError, validate_vector
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError
from deptslm_worker.vector_retrieval import (
    AuthorizedVectorHit,
    RetrievalBoundaryError,
    search_authorized_result,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope
from app.extraction_domain import CHUNKING_VERSION, NORMALIZATION_VERSION, PIPELINE_VERSION
from app.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
    RagAnswerCitation,
    RagAnswerRun,
)
from app.rag_domain import (
    ANSWER_CONTRACT_VERSION,
    GENERATION_MODEL_ID,
    GENERATION_MODEL_REVISION,
    INSUFFICIENT_INFORMATION_MESSAGE,
    PROMPT_VERSION,
    RagContractError,
    safe_public_filename,
    validate_generation_response,
)
from app.rag_runtime_client import RagRuntimeClient
from app.rag_settings import RagSettings
from app.schemas import RagAnswerResponse, RagCitationResponse
from app.selected_chunk_reader import LoadedEvidence, load_selected_chunks
from app.services import (
    ALL_ROLES,
    ServiceError,
    append_mutation_audit,
    authorize_transaction,
)
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    QUERY_EMBEDDING_PIPELINE_VERSION,
    VECTOR_SCHEMA_VERSION,
)

logger = logging.getLogger("deptslm.rag")

_UNEXPECTED_STAGE_CODES = {
    "query_embedding": "query_embedding_failed",
    "retrieval": "retrieval_authority_failed",
    "artifact_loading": "source_artifact_mismatch",
    "generation": "generation_failed",
    "finalization": "database_unavailable",
}


class RagAnswerServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _StartedRun:
    id: UUID
    created_at: datetime


def answer_question(
    factory: sessionmaker[Session],
    settings: RagSettings,
    data_dir: Path,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    question: str,
    *,
    runtime: RagRuntimeClient | None = None,
    qdrant: DepartmentQdrant | None = None,
) -> RagAnswerResponse:
    """Run short transactions around external retrieval and generation operations."""

    started = _start_run(factory, settings, principal, request_scope, len(question))
    runtime_client = runtime or RagRuntimeClient(
        settings.runtime_url, settings.runtime_token, settings.request_timeout_seconds
    )
    owned_qdrant = qdrant is None
    adapter = qdrant
    candidate_count = None
    authorized_count = None
    stage = "query_embedding"
    try:
        query = runtime_client.query_embedding(question)
        try:
            query_vector = validate_vector(query)
        except EmbeddingError as error:
            raise RagContractError("invalid_query_embedding") from error
        stage = "retrieval"
        if adapter is None:
            adapter = DepartmentQdrant(
                settings.qdrant_url,
                settings.qdrant_api_key,
                settings.qdrant_timeout_seconds,
            )
        adapter.verify_collection()
        search = search_authorized_result(
            factory,
            adapter,
            request_scope.department,
            query_vector,
            limit=settings.candidate_limit,
        )
        candidate_count = search.candidate_count
        authorized_count = len(search.hits)
        selected = _select_hits(search.hits, settings)
        if not selected:
            stage = "finalization"
            return _finalize_insufficient(
                factory,
                principal,
                request_scope,
                started,
                candidate_count,
                authorized_count,
            )
        stage = "artifact_loading"
        loaded = load_selected_chunks(
            data_dir,
            request_scope.department,
            selected,
            max_evidence_chars=settings.max_evidence_chars,
        )
        stage = "generation"
        generation = validate_generation_response(
            runtime_client.generate(question, tuple(item.source for item in loaded)),
            tuple(item.source.label for item in loaded),
        )
        stage = "artifact_loading"
        reloaded = load_selected_chunks(
            data_dir,
            request_scope.department,
            selected,
            max_evidence_chars=settings.max_evidence_chars,
        )
        if tuple((item.hit.chunk_id, item.source.label, item.source.text) for item in reloaded) != (
            tuple((item.hit.chunk_id, item.source.label, item.source.text) for item in loaded)
        ):
            raise RagContractError("source_changed")
        stage = "finalization"
        if generation.status == "insufficient_information":
            return _finalize_insufficient(
                factory,
                principal,
                request_scope,
                started,
                candidate_count,
                authorized_count,
                reloaded,
            )
        return _finalize_answered(
            factory,
            principal,
            request_scope,
            started,
            candidate_count,
            authorized_count,
            generation.answer,
            reloaded,
            generation.citations,
        )
    except ServiceError:
        _fail_run(factory, started.id, request_scope.department, "department_unavailable")
        raise
    except RagContractError as error:
        _fail_run(
            factory,
            started.id,
            request_scope.department,
            error.code,
            candidate_count,
            authorized_count,
        )
        raise RagAnswerServiceError(error.code) from error
    except QdrantBoundaryError as error:
        code = (
            "qdrant_unavailable"
            if error.code == "qdrant_unavailable"
            else ("retrieval_authority_failed")
        )
        _fail_run(
            factory,
            started.id,
            request_scope.department,
            code,
            candidate_count,
            authorized_count,
        )
        raise RagAnswerServiceError(code) from error
    except RetrievalBoundaryError as error:
        _fail_run(
            factory,
            started.id,
            request_scope.department,
            "retrieval_authority_failed",
            candidate_count,
            authorized_count,
        )
        raise RagAnswerServiceError("retrieval_authority_failed") from error
    except SQLAlchemyError as error:
        _fail_run(factory, started.id, request_scope.department, "database_unavailable")
        raise RagAnswerServiceError("database_unavailable") from error
    except Exception as error:
        code = _UNEXPECTED_STAGE_CODES[stage]
        _fail_run(
            factory,
            started.id,
            request_scope.department,
            code,
            candidate_count,
            authorized_count,
        )
        raise RagAnswerServiceError(code) from error
    finally:
        if owned_qdrant and adapter is not None:
            try:
                adapter.close()
            except Exception:
                logger.warning("rag_process qdrant_close_failed")


def _start_run(factory, settings, principal, request_scope, question_chars) -> _StartedRun:
    try:
        with factory.begin() as session:
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                ALL_ROLES,
                lock=True,
                audit_action="rag.answer.start.authorization",
            )
            run = RagAnswerRun(
                department_id=request_scope.department.value,
                requested_by_user_id=authorization.identity.id,
                status="running",
                question_char_count=question_chars,
                query_embedding_pipeline_version=QUERY_EMBEDDING_PIPELINE_VERSION,
                query_embedding_model_id=EMBEDDING_MODEL_ID,
                query_embedding_model_revision=EMBEDDING_MODEL_REVISION,
                generation_model_id=GENERATION_MODEL_ID,
                generation_model_revision=GENERATION_MODEL_REVISION,
                prompt_version=PROMPT_VERSION,
                answer_contract_version=ANSWER_CONTRACT_VERSION,
                minimum_score=settings.minimum_score,
            )
            session.add(run)
            session.flush()
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=principal.subject,
                request_scope=request_scope,
                action="rag.answer.start",
                resource_type="rag_answer_run",
                resource_id=run.id,
            )
            session.flush()
            return _StartedRun(run.id, run.created_at)
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def _select_hits(
    hits: tuple[AuthorizedVectorHit, ...], settings: RagSettings
) -> tuple[AuthorizedVectorHit, ...]:
    ordered = sorted(hits, key=lambda hit: (-hit.score, str(hit.chunk_id)))
    counts: dict[UUID, int] = {}
    selected = []
    minimum = float(settings.minimum_score)
    for hit in ordered:
        if not math.isfinite(hit.score) or not -1.0 <= hit.score <= 1.0:
            raise RagContractError("retrieval_authority_failed")
        if hit.score < minimum:
            continue
        count = counts.get(hit.document_id, 0)
        if count >= settings.max_sources_per_document:
            continue
        selected.append(hit)
        counts[hit.document_id] = count + 1
        if len(selected) >= settings.max_sources:
            break
    return tuple(selected)


def _finalize_insufficient(
    factory,
    principal,
    request_scope,
    started,
    candidate_count,
    authorized_count,
    supplied: tuple[LoadedEvidence, ...] = (),
) -> RagAnswerResponse:
    try:
        with factory.begin() as session:
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                ALL_ROLES,
                lock=True,
                audit_action="rag.answer.complete.authorization",
            )
            run = _lock_running_run(session, request_scope.department, started.id)
            if run.requested_by_user_id != authorization.identity.id:
                raise RagContractError("department_unavailable")
            if supplied:
                _lock_and_revalidate_sources(session, request_scope.department, supplied)
            run.status = "insufficient_information"
            run.retrieval_candidate_count = candidate_count
            run.retrieval_authorized_count = authorized_count
            run.selected_source_count = len(supplied)
            run.finished_at = datetime.now(UTC)
            run.version += 1
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=principal.subject,
                request_scope=request_scope,
                action="rag.answer.complete",
                resource_type="rag_answer_run",
                resource_id=run.id,
            )
            session.flush()
        return RagAnswerResponse(
            id=started.id,
            status="insufficient_information",
            answer=INSUFFICIENT_INFORMATION_MESSAGE,
            citations=[],
            generation_model=GENERATION_MODEL_ID,
            created_at=started.created_at,
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise RagContractError("database_unavailable") from error


def _finalize_answered(
    factory,
    principal,
    request_scope,
    started,
    candidate_count,
    authorized_count,
    answer,
    supplied: tuple[LoadedEvidence, ...],
    citation_labels: tuple[str, ...],
) -> RagAnswerResponse:
    try:
        with factory.begin() as session:
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                ALL_ROLES,
                lock=True,
                audit_action="rag.answer.complete.authorization",
            )
            run = _lock_running_run(session, request_scope.department, started.id)
            if run.requested_by_user_id != authorization.identity.id:
                raise RagContractError("department_unavailable")
            current = _lock_and_revalidate_sources(session, request_scope.department, supplied)
            by_label = {item.source.label: item for item in supplied}
            if len(by_label) != len(supplied) or any(
                label not in by_label for label in citation_labels
            ):
                raise RagContractError("invalid_citation")
            cited = tuple(by_label[label] for label in citation_labels)
            public_citations = []
            for rank, item in enumerate(cited, 1):
                hit = item.hit
                document, _extraction, _indexing, chunk = current[hit.chunk_id]
                session.add(
                    RagAnswerCitation(
                        run_id=run.id,
                        department_id=request_scope.department.value,
                        document_id=hit.document_id,
                        extraction_id=hit.extraction_id,
                        indexing_id=hit.indexing_id,
                        chunk_id=hit.chunk_id,
                        source_label=item.source.label,
                        rank=rank,
                        ordinal=hit.chunk_ordinal,
                        retrieval_score=Decimal(str(hit.score)),
                        provenance_kind=chunk.provenance_kind,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                    )
                )
                public_citations.append(
                    RagCitationResponse(
                        source_id=item.source.label,
                        document_id=document.id,
                        original_filename=safe_public_filename(document.original_filename),
                        chunk_id=chunk.id,
                        ordinal=chunk.ordinal,
                        provenance_kind=chunk.provenance_kind,
                        page_start=chunk.page_start,
                        page_end=chunk.page_end,
                        line_start=chunk.line_start,
                        line_end=chunk.line_end,
                    )
                )
            run.status = "answered"
            run.retrieval_candidate_count = candidate_count
            run.retrieval_authorized_count = authorized_count
            run.selected_source_count = len(supplied)
            run.finished_at = datetime.now(UTC)
            run.version += 1
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=principal.subject,
                request_scope=request_scope,
                action="rag.answer.complete",
                resource_type="rag_answer_run",
                resource_id=run.id,
            )
            session.flush()
            citation_count = session.scalar(
                select(func.count())
                .select_from(RagAnswerCitation)
                .where(
                    RagAnswerCitation.run_id == run.id,
                    RagAnswerCitation.department_id == request_scope.department.value,
                )
            )
            if citation_count != len(cited):
                raise RagContractError("invalid_citation")
        return RagAnswerResponse(
            id=started.id,
            status="answered",
            answer=answer,
            citations=public_citations,
            generation_model=GENERATION_MODEL_ID,
            created_at=started.created_at,
        )
    except (ServiceError, RagContractError):
        raise
    except SQLAlchemyError as error:
        raise RagContractError("database_unavailable") from error


def _lock_running_run(session, scope, run_id):
    run = session.execute(
        select(RagAnswerRun)
        .where(RagAnswerRun.id == run_id, RagAnswerRun.department_id == scope.value)
        .with_for_update()
    ).scalar_one_or_none()
    if run is None or run.status != "running":
        raise RagContractError("source_changed")
    return run


def _lock_and_revalidate_sources(session, scope, supplied):
    document_ids = sorted({item.hit.document_id for item in supplied}, key=str)
    extraction_ids = sorted({item.hit.extraction_id for item in supplied}, key=str)
    indexing_ids = sorted({item.hit.indexing_id for item in supplied}, key=str)
    chunk_ids = sorted({item.hit.chunk_id for item in supplied}, key=str)
    documents = {
        row.id: row
        for row in session.scalars(
            select(Document)
            .where(Document.department_id == scope.value, Document.id.in_(document_ids))
            .order_by(Document.id)
            .with_for_update()
        )
    }
    extractions = {
        row.id: row
        for row in session.scalars(
            select(DocumentExtraction)
            .where(
                DocumentExtraction.department_id == scope.value,
                DocumentExtraction.id.in_(extraction_ids),
            )
            .order_by(DocumentExtraction.id)
            .with_for_update()
        )
    }
    indexings = {
        row.id: row
        for row in session.scalars(
            select(DocumentVectorIndexing)
            .where(
                DocumentVectorIndexing.department_id == scope.value,
                DocumentVectorIndexing.id.in_(indexing_ids),
            )
            .order_by(DocumentVectorIndexing.id)
            .with_for_update()
        )
    }
    chunks = {
        row.id: row
        for row in session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.department_id == scope.value, DocumentChunk.id.in_(chunk_ids))
            .order_by(DocumentChunk.id)
            .with_for_update()
        )
    }
    result = {}
    for item in supplied:
        hit = item.hit
        document = documents.get(hit.document_id)
        extraction = extractions.get(hit.extraction_id)
        indexing = indexings.get(hit.indexing_id)
        chunk = chunks.get(hit.chunk_id)
        if (
            document is None
            or document.department_id != scope.value
            or document.id != hit.document_id
            or document.status != "stored"
            or extraction is None
            or extraction.department_id != scope.value
            or extraction.id != hit.extraction_id
            or extraction.status != "succeeded"
            or extraction.document_id != document.id
            or extraction.pipeline_version != PIPELINE_VERSION
            or extraction.pipeline_version != hit.extraction_pipeline_version
            or extraction.normalization_version != NORMALIZATION_VERSION
            or extraction.normalization_version != hit.normalization_version
            or extraction.chunking_version != CHUNKING_VERSION
            or extraction.chunking_version != hit.chunking_version
            or extraction.chunk_count != hit.extraction_chunk_count
            or extraction.normalized_sha256 != hit.normalized_sha256
            or extraction.normalized_byte_size != hit.normalized_byte_size
            or extraction.output_byte_size != hit.output_byte_size
            or indexing is None
            or indexing.department_id != scope.value
            or indexing.id != hit.indexing_id
            or indexing.status != "succeeded"
            or indexing.document_id != document.id
            or indexing.extraction_id != extraction.id
            or indexing.vector_attempt_id != hit.vector_attempt_id
            or indexing.expected_chunk_count != hit.indexing_expected_chunk_count
            or indexing.point_count != hit.indexing_point_count
            or indexing.point_count != indexing.expected_chunk_count
            or indexing.expected_chunk_count != extraction.chunk_count
            or indexing.embedding_pipeline_version != EMBEDDING_PIPELINE_VERSION
            or indexing.embedding_pipeline_version != hit.embedding_pipeline_version
            or indexing.embedding_model_id != EMBEDDING_MODEL_ID
            or indexing.embedding_model_id != hit.embedding_model_id
            or indexing.embedding_model_revision != EMBEDDING_MODEL_REVISION
            or indexing.embedding_model_revision != hit.embedding_model_revision
            or indexing.embedding_dimension != EMBEDDING_DIMENSION
            or indexing.embedding_dimension != hit.embedding_dimension
            or indexing.distance != EMBEDDING_DISTANCE
            or indexing.distance != hit.distance
            or indexing.vector_schema_version != VECTOR_SCHEMA_VERSION
            or indexing.vector_schema_version != hit.vector_schema_version
            or indexing.qdrant_collection != QDRANT_COLLECTION
            or indexing.qdrant_collection != hit.qdrant_collection
            or chunk is None
            or chunk.department_id != scope.value
            or chunk.id != hit.chunk_id
            or chunk.document_id != document.id
            or chunk.extraction_id != extraction.id
            or chunk.ordinal != hit.chunk_ordinal
            or chunk.char_start != hit.chunk_char_start
            or chunk.char_end != hit.chunk_char_end
            or chunk.byte_size != hit.chunk_byte_size
            or chunk.content_sha256 != hit.chunk_content_sha256
            or chunk.provenance_kind != hit.provenance_kind
            or chunk.page_start != hit.page_start
            or chunk.page_end != hit.page_end
            or chunk.line_start != hit.line_start
            or chunk.line_end != hit.line_end
        ):
            raise RagContractError("source_changed")
        result[hit.chunk_id] = (document, extraction, indexing, chunk)
    return result


def _fail_run(
    factory,
    run_id,
    scope,
    code,
    candidate_count=None,
    authorized_count=None,
) -> None:
    try:
        with factory.begin() as session:
            run = session.execute(
                select(RagAnswerRun)
                .where(RagAnswerRun.id == run_id, RagAnswerRun.department_id == scope.value)
                .with_for_update()
            ).scalar_one_or_none()
            if run is None or run.status != "running":
                return
            run.status = "failed"
            run.retrieval_candidate_count = candidate_count
            run.retrieval_authorized_count = authorized_count
            run.error_code = code
            run.finished_at = datetime.now(UTC)
            run.version += 1
    except SQLAlchemyError:
        return
