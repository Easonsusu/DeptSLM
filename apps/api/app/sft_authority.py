"""Exact, content-free Phase 10 authority snapshots.

The SFT worker never keeps source text or a complete source bundle in its
parent process.  It streams child-produced, sorted UUID selectors through this
module in bounded database batches.  The external authority mapping contains
only the five IDs permitted in final provenance; the larger immutable contract
is folded into a private SHA-256 fingerprint only.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentExtraction, DocumentVectorIndexing

AUTHORITY_FILE = ".deptslm-authority.jsonl"
SELECTOR_BATCH_SIZE = 512
_SELECTOR_LINE_LIMIT = 64
_Checkpoint = Callable[[], None]


class SftSourceAuthorityError(RuntimeError):
    """Content-free denial when a source reference no longer has exact authority."""


@dataclass(frozen=True, slots=True)
class SftAuthorityReference:
    department_id: UUID
    document_id: UUID
    document_version: int
    document_status: str
    document_byte_size: int
    document_sha256: str
    extraction_id: UUID
    extraction_version: int
    extraction_status: str
    extraction_attempt_number: int
    extraction_pipeline_version: str
    extraction_parser_name: str
    extraction_parser_version: str
    extraction_normalization_version: str
    extraction_chunking_version: str
    extraction_source_sha256: str
    extraction_normalized_sha256: str
    extraction_normalized_byte_size: int
    extraction_chunk_count: int
    extraction_finished_at: datetime
    indexing_id: UUID
    indexing_version: int
    indexing_status: str
    indexing_attempt_number: int
    vector_attempt_id: UUID
    embedding_pipeline_version: str
    embedding_model_id: str
    embedding_model_revision: str
    embedding_dimension: int
    embedding_distance: str
    vector_schema_version: str
    collection_identity: str
    indexing_expected_chunk_count: int
    indexing_point_count: int
    indexing_finished_at: datetime
    chunk_id: UUID
    chunk_created_at: datetime
    chunk_ordinal: int
    chunk_content_sha256: str
    chunk_byte_size: int
    chunk_char_start: int
    chunk_char_end: int
    provenance_kind: str
    provenance_start: int
    provenance_end: int

    def private_value(self) -> dict[str, object]:
        """Canonical immutable authority fields; never expose this structure."""

        return {
            "department_id": str(self.department_id),
            "document_id": str(self.document_id),
            "document_version": self.document_version,
            "document_status": self.document_status,
            "document_byte_size": self.document_byte_size,
            "document_sha256": self.document_sha256,
            "extraction_id": str(self.extraction_id),
            "extraction_version": self.extraction_version,
            "extraction_status": self.extraction_status,
            "extraction_attempt_number": self.extraction_attempt_number,
            "extraction_pipeline_version": self.extraction_pipeline_version,
            "extraction_parser_name": self.extraction_parser_name,
            "extraction_parser_version": self.extraction_parser_version,
            "extraction_normalization_version": self.extraction_normalization_version,
            "extraction_chunking_version": self.extraction_chunking_version,
            "extraction_source_sha256": self.extraction_source_sha256,
            "extraction_normalized_sha256": self.extraction_normalized_sha256,
            "extraction_normalized_byte_size": self.extraction_normalized_byte_size,
            "extraction_chunk_count": self.extraction_chunk_count,
            "extraction_finished_at": _timestamp(self.extraction_finished_at),
            "indexing_id": str(self.indexing_id),
            "indexing_version": self.indexing_version,
            "indexing_status": self.indexing_status,
            "indexing_attempt_number": self.indexing_attempt_number,
            "vector_attempt_id": str(self.vector_attempt_id),
            "embedding_pipeline_version": self.embedding_pipeline_version,
            "embedding_model_id": self.embedding_model_id,
            "embedding_model_revision": self.embedding_model_revision,
            "embedding_dimension": self.embedding_dimension,
            "embedding_distance": self.embedding_distance,
            "vector_schema_version": self.vector_schema_version,
            "collection_identity": self.collection_identity,
            "indexing_expected_chunk_count": self.indexing_expected_chunk_count,
            "indexing_point_count": self.indexing_point_count,
            "indexing_finished_at": _timestamp(self.indexing_finished_at),
            "chunk_id": str(self.chunk_id),
            "chunk_created_at": _timestamp(self.chunk_created_at),
            "chunk_ordinal": self.chunk_ordinal,
            "chunk_content_sha256": self.chunk_content_sha256,
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
    selector_count: int


def capture_source_authority(
    session: Session,
    department_id: UUID,
    source_chunk_ids: set[UUID],
    *,
    lock: bool = False,
) -> SftAuthoritySnapshot:
    """Capture a small in-process source set for source-import code paths."""

    if not source_chunk_ids:
        raise SftSourceAuthorityError()
    references = _references_for_chunk_ids(
        session,
        department_id,
        tuple(sorted(source_chunk_ids, key=lambda item: item.bytes)),
        lock=lock,
    )
    return SftAuthoritySnapshot(
        references=references,
        fingerprint=_fingerprint(references),
        selector_count=len(references),
    )


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


def write_authority_mapping(
    session: Session,
    department_id: UUID,
    selector_fd: int,
    stage_fd: int,
    *,
    checkpoint: _Checkpoint,
) -> SftAuthoritySnapshot:
    """Stream a sorted selector into bounded DB batches and a private mapping.

    ``selector_fd`` stays open with the caller so its exact descriptor can be
    re-used for final authority validation after the scratch directory has been
    renamed.  No selector or authority set is materialized in parent memory.
    """

    # All bounded selector lookups must observe one PostgreSQL logical
    # snapshot.  This statement is deliberately the first database operation
    # in the caller's read-only authority session; it takes no long-lived row
    # locks while the selector is streamed.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
    _private_directory(stage_fd, writable=True)
    output = _open_new_private_file(stage_fd, AUTHORITY_FILE)
    try:
        digest = hashlib.sha256()
        count = 0
        previous: UUID | None = None
        for batch in _selector_batches(selector_fd, checkpoint=checkpoint):
            if previous is not None and batch[0].bytes <= previous.bytes:
                raise SftSourceAuthorityError()
            references = _references_for_chunk_ids(session, department_id, batch, lock=False)
            for reference in references:
                if previous is not None and reference.chunk_id.bytes <= previous.bytes:
                    raise SftSourceAuthorityError()
                private = _canonical(reference.private_value())
                digest.update(private + b"\n")
                _write_all(output, _canonical(reference.provenance_value()) + b"\n", checkpoint)
                previous = reference.chunk_id
                count += 1
            checkpoint()
        if count < 1:
            raise SftSourceAuthorityError()
        os.fsync(output)
        _entry_stable(stage_fd, AUTHORITY_FILE, output)
        return SftAuthoritySnapshot((), digest.hexdigest(), count)
    except Exception:
        try:
            os.unlink(AUTHORITY_FILE, dir_fd=stage_fd)
            os.fsync(stage_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(output)


def validate_authority_selector(
    session: Session,
    department_id: UUID,
    selector_fd: int,
    *,
    expected_fingerprint: str,
    expected_count: int,
    lock: bool,
    checkpoint: _Checkpoint | None = None,
) -> SftAuthoritySnapshot:
    """Re-read the retained selector and compare the exact private snapshot."""

    check = checkpoint or _noop
    digest = hashlib.sha256()
    count = 0
    previous: UUID | None = None
    for batch in _selector_batches(selector_fd, checkpoint=check, allow_unlinked=True):
        if previous is not None and batch[0].bytes <= previous.bytes:
            raise SftSourceAuthorityError()
        references = _references_for_chunk_ids(session, department_id, batch, lock=lock)
        for reference in references:
            if previous is not None and reference.chunk_id.bytes <= previous.bytes:
                raise SftSourceAuthorityError()
            digest.update(_canonical(reference.private_value()) + b"\n")
            previous = reference.chunk_id
            count += 1
        check()
    snapshot = SftAuthoritySnapshot((), digest.hexdigest(), count)
    if count != expected_count or snapshot.fingerprint != expected_fingerprint:
        raise SftSourceAuthorityError()
    return snapshot


def _references_for_chunk_ids(
    session: Session,
    department_id: UUID,
    chunk_ids: tuple[UUID, ...],
    *,
    lock: bool,
) -> tuple[SftAuthorityReference, ...]:
    if not chunk_ids or len(chunk_ids) > SELECTOR_BATCH_SIZE:
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
            DocumentChunk.id.in_(chunk_ids),
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
    by_chunk: dict[UUID, list[SftAuthorityReference]] = {}
    for document, extraction, indexing, chunk in session.execute(statement).all():
        reference = _reference(document, extraction, indexing, chunk)
        by_chunk.setdefault(reference.chunk_id, []).append(reference)
    if set(by_chunk) != set(chunk_ids) or any(len(value) != 1 for value in by_chunk.values()):
        raise SftSourceAuthorityError()
    return tuple(by_chunk[chunk_id][0] for chunk_id in chunk_ids)


def _reference(
    document: Document,
    extraction: DocumentExtraction,
    indexing: DocumentVectorIndexing,
    chunk: DocumentChunk,
) -> SftAuthorityReference:
    provenance_start = chunk.page_start if chunk.provenance_kind == "page" else chunk.line_start
    provenance_end = chunk.page_end if chunk.provenance_kind == "page" else chunk.line_end
    values = (
        provenance_start,
        provenance_end,
        extraction.parser_name,
        extraction.parser_version,
        extraction.normalized_sha256,
        extraction.normalized_byte_size,
        extraction.chunk_count,
        extraction.finished_at,
        indexing.vector_attempt_id,
        indexing.point_count,
        indexing.finished_at,
    )
    if any(value is None for value in values):
        raise SftSourceAuthorityError()
    return SftAuthorityReference(
        department_id=document.department_id,
        document_id=document.id,
        document_version=document.version,
        document_status=document.status,
        document_byte_size=document.byte_size,
        document_sha256=document.sha256,
        extraction_id=extraction.id,
        extraction_version=extraction.version,
        extraction_status=extraction.status,
        extraction_attempt_number=extraction.attempt_number,
        extraction_pipeline_version=extraction.pipeline_version,
        extraction_parser_name=extraction.parser_name,
        extraction_parser_version=extraction.parser_version,
        extraction_normalization_version=extraction.normalization_version,
        extraction_chunking_version=extraction.chunking_version,
        extraction_source_sha256=extraction.source_sha256,
        extraction_normalized_sha256=extraction.normalized_sha256,
        extraction_normalized_byte_size=extraction.normalized_byte_size,
        extraction_chunk_count=extraction.chunk_count,
        extraction_finished_at=extraction.finished_at,
        indexing_id=indexing.id,
        indexing_version=indexing.version,
        indexing_status=indexing.status,
        indexing_attempt_number=indexing.attempt_number,
        vector_attempt_id=indexing.vector_attempt_id,
        embedding_pipeline_version=indexing.embedding_pipeline_version,
        embedding_model_id=indexing.embedding_model_id,
        embedding_model_revision=indexing.embedding_model_revision,
        embedding_dimension=indexing.embedding_dimension,
        embedding_distance=indexing.distance,
        vector_schema_version=indexing.vector_schema_version,
        collection_identity=getattr(indexing, "q" + "drant_collection"),
        indexing_expected_chunk_count=indexing.expected_chunk_count,
        indexing_point_count=indexing.point_count,
        indexing_finished_at=indexing.finished_at,
        chunk_id=chunk.id,
        chunk_created_at=chunk.created_at,
        chunk_ordinal=chunk.ordinal,
        chunk_content_sha256=chunk.content_sha256,
        chunk_byte_size=chunk.byte_size,
        chunk_char_start=chunk.char_start,
        chunk_char_end=chunk.char_end,
        provenance_kind=chunk.provenance_kind,
        provenance_start=provenance_start,
        provenance_end=provenance_end,
    )


def _selector_batches(
    selector_fd: int,
    *,
    checkpoint: _Checkpoint,
    allow_unlinked: bool = False,
) -> Iterable[tuple[UUID, ...]]:
    try:
        os.lseek(selector_fd, 0, os.SEEK_SET)
        metadata = os.fstat(selector_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink not in ({0, 1} if allow_unlinked else {1})
            or metadata.st_mode & 0o077
            or metadata.st_size < 1
        ):
            raise SftSourceAuthorityError()
        buffered = bytearray()
        batch: list[UUID] = []
        while True:
            checkpoint()
            block = os.read(selector_fd, 64 * 1024)
            if not block:
                break
            buffered.extend(block)
            while True:
                marker = buffered.find(b"\n")
                if marker < 0:
                    if len(buffered) > _SELECTOR_LINE_LIMIT:
                        raise SftSourceAuthorityError()
                    break
                raw = bytes(buffered[:marker])
                del buffered[: marker + 1]
                if not 1 <= len(raw) <= 36:
                    raise SftSourceAuthorityError()
                try:
                    value = UUID(raw.decode("ascii"))
                except (UnicodeDecodeError, ValueError) as error:
                    raise SftSourceAuthorityError() from error
                if value.int == 0:
                    raise SftSourceAuthorityError()
                batch.append(value)
                if len(batch) == SELECTOR_BATCH_SIZE:
                    yield tuple(batch)
                    batch.clear()
        if buffered:
            raise SftSourceAuthorityError()
        if batch:
            yield tuple(batch)
        after = os.fstat(selector_fd)
        if not _same_selector_file(metadata, after, allow_unlinked=allow_unlinked):
            raise SftSourceAuthorityError()
    except OSError as error:
        raise SftSourceAuthorityError() from error


def _fingerprint(references: tuple[SftAuthorityReference, ...]) -> str:
    if not references:
        raise SftSourceAuthorityError()
    return hashlib.sha256(
        b"".join(_canonical(reference.private_value()) + b"\n" for reference in references)
    ).hexdigest()


def _canonical(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise SftSourceAuthorityError()
    return value.isoformat(timespec="microseconds")


def _private_directory(descriptor: int, *, writable: bool) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or (writable and not metadata.st_mode & stat.S_IWUSR)
    ):
        raise SftSourceAuthorityError()


def _open_new_private_file(directory_fd: int, name: str) -> int:
    try:
        return os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise SftSourceAuthorityError() from error


def _write_all(descriptor: int, value: bytes, checkpoint: _Checkpoint) -> None:
    total = 0
    while total < len(value):
        checkpoint()
        total += os.write(descriptor, value[total:])


def _entry_stable(directory_fd: int, name: str, descriptor: int) -> None:
    before = os.fstat(descriptor)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not _same_regular_file(before, current):
        raise SftSourceAuthorityError()


def _same_regular_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_uid == second.st_uid
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_nlink == second.st_nlink == 1
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _same_selector_file(
    first: os.stat_result, second: os.stat_result, *, allow_unlinked: bool
) -> bool:
    return (
        stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_uid == second.st_uid
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_nlink == second.st_nlink
        and (first.st_nlink == 1 or (allow_unlinked and first.st_nlink == 0))
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _noop() -> None:
    return None
