"""Narrow no-follow reader for selected, PostgreSQL-authorized Phase 5 chunks."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from deptslm_worker.artifact_reader import (
    ArtifactError,
    ArtifactExpectation,
    ExternalChunk,
    Phase5ArtifactReader,
)
from deptslm_worker.vector_retrieval import AuthorizedVectorHit

from app.authorization import DepartmentScope
from app.rag_domain import (
    MAX_SOURCE_CHARS,
    EvidenceSource,
    RagContractError,
    validate_safe_text,
)


@dataclass(frozen=True, slots=True)
class LoadedEvidence:
    hit: AuthorizedVectorHit
    source: EvidenceSource


def load_selected_chunks(
    data_dir: Path,
    scope: DepartmentScope,
    hits: tuple[AuthorizedVectorHit, ...],
    *,
    max_evidence_chars: int,
) -> tuple[LoadedEvidence, ...]:
    if not isinstance(scope, DepartmentScope) or not hits:
        raise RagContractError("source_artifact_mismatch")
    by_extraction: dict[object, list[AuthorizedVectorHit]] = defaultdict(list)
    for hit in hits:
        by_extraction[hit.extraction_id].append(hit)
    text_by_chunk = {}
    try:
        for grouped in by_extraction.values():
            first = grouped[0]
            requested = {item.chunk_ordinal: item for item in grouped}
            if len(requested) != len(grouped):
                raise RagContractError("source_artifact_mismatch")
            expectation = ArtifactExpectation(
                department_id=scope.value,
                document_id=first.document_id,
                extraction_id=first.extraction_id,
                expected_chunk_count=first.extraction_chunk_count,
                normalized_sha256=first.normalized_sha256,
                normalized_byte_size=first.normalized_byte_size,
                output_byte_size=first.output_byte_size,
            )
            with Phase5ArtifactReader(data_dir, scope, expectation) as reader:
                found = set()
                for chunk in reader.iter_chunks():
                    expected = requested.get(chunk.ordinal)
                    if expected is None:
                        continue
                    _verify_chunk(chunk, expected)
                    if expected.chunk_id in text_by_chunk:
                        raise RagContractError("source_artifact_mismatch")
                    text_by_chunk[expected.chunk_id] = chunk.text
                    found.add(chunk.ordinal)
                if found != set(requested):
                    raise RagContractError("source_artifact_mismatch")
                reader.verify_unchanged()
    except ArtifactError as error:
        code = (
            "source_artifact_missing"
            if error.code == "chunk_artifact_missing"
            else ("source_artifact_mismatch")
        )
        raise RagContractError(code) from error

    loaded = []
    remaining = max_evidence_chars
    for hit in hits:
        text = text_by_chunk.get(hit.chunk_id)
        if not isinstance(text, str) or not text:
            raise RagContractError("source_artifact_mismatch")
        try:
            validate_safe_text(text, field="evidence")
        except ValueError as error:
            raise RagContractError("source_artifact_mismatch") from error
        bounded = text[: min(MAX_SOURCE_CHARS, remaining)]
        if not bounded:
            break
        label = f"S{len(loaded) + 1}"
        loaded.append(LoadedEvidence(hit, EvidenceSource(label, bounded)))
        remaining -= len(bounded)
        if remaining <= 0:
            break
    if not loaded:
        raise RagContractError("source_artifact_mismatch")
    return tuple(loaded)


def _verify_chunk(chunk: ExternalChunk, expected: AuthorizedVectorHit) -> None:
    if (
        chunk.ordinal != expected.chunk_ordinal
        or chunk.char_start != expected.chunk_char_start
        or chunk.char_end != expected.chunk_char_end
        or chunk.byte_size != expected.chunk_byte_size
        or chunk.content_sha256 != expected.chunk_content_sha256
        or chunk.provenance_kind != expected.provenance_kind
        or chunk.page_start != expected.page_start
        or chunk.page_end != expected.page_end
        or chunk.line_start != expected.line_start
        or chunk.line_end != expected.line_end
    ):
        raise RagContractError("source_artifact_mismatch")
