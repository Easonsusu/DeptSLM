"""Deterministic character chunking with page or line provenance."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from deptslm_worker.domain import CHUNKING_VERSION
from deptslm_worker.normalization import NormalizedDocument, ProvenanceSpan


class ChunkingError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    byte_size: int
    content_sha256: str
    provenance_kind: str
    page_start: int | None = None
    page_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None


def chunk_document(
    document: NormalizedDocument,
    *,
    max_chars: int,
    overlap_chars: int,
    max_chunks: int,
) -> list[Chunk]:
    if max_chars <= 0 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("invalid chunk bounds")
    chunks: list[Chunk] = []
    start = 0
    text = document.text
    while start < len(text):
        end = _choose_end(text, start, max_chars)
        if end <= start:
            end = min(len(text), start + max_chars)
        value = text[start:end]
        if value.strip():
            chunks.append(_build_chunk(len(chunks), value, start, end, document))
            if len(chunks) > max_chunks:
                raise ChunkingError("chunk_limit_exceeded")
        if end == len(text):
            break
        progress_floor = start + 1
        next_start = max(progress_floor, end - overlap_chars)
        start = _avoid_combining_start(text, next_start, end, progress_floor)
    if not chunks:
        raise ChunkingError("no_extractable_text")
    return chunks


def _choose_end(text: str, start: int, maximum: int) -> int:
    hard_end = min(len(text), start + maximum)
    if hard_end == len(text):
        return hard_end
    floor = start + max(1, maximum // 2)
    window = text[start:hard_end]
    candidates = (
        window.rfind("\n\n", floor - start),
        window.rfind("\n", floor - start),
    )
    for position in candidates:
        if position >= 0:
            return _avoid_combining_end(text, start + position + 1, hard_end)
    for position in range(hard_end - 1, floor - 1, -1):
        if text[position].isspace():
            return _avoid_combining_end(text, position + 1, hard_end)
    return _avoid_combining_end(text, hard_end, hard_end)


def _avoid_combining_end(text: str, end: int, hard_end: int) -> int:
    while end < len(text) and end < hard_end and unicodedata.combining(text[end]):
        end += 1
    while end > 0 and end < len(text) and unicodedata.combining(text[end]):
        end -= 1
    return end


def _avoid_combining_start(text: str, start: int, ceiling: int, floor: int) -> int:
    while start > floor and start < ceiling and unicodedata.combining(text[start]):
        start -= 1
    return start


def _build_chunk(
    ordinal: int,
    value: str,
    start: int,
    end: int,
    document: NormalizedDocument,
) -> Chunk:
    numbers = _provenance_numbers(document.spans, start, end)
    if not numbers:
        numbers = _nearest_provenance(document.spans, start)
    first, last = min(numbers), max(numbers)
    encoded = value.encode("utf-8")
    common = dict(
        ordinal=ordinal,
        text=value,
        char_start=start,
        char_end=end,
        byte_size=len(encoded),
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        provenance_kind=document.provenance_kind,
    )
    if document.provenance_kind == "page":
        return Chunk(**common, page_start=first, page_end=last)
    return Chunk(**common, line_start=first, line_end=last)


def _provenance_numbers(
    spans: tuple[ProvenanceSpan, ...], start: int, end: int
) -> list[int]:
    return [
        span.number for span in spans if span.char_start < end and span.char_end > start
    ]


def _nearest_provenance(spans: tuple[ProvenanceSpan, ...], offset: int) -> list[int]:
    if not spans:
        return [1]
    return [min(spans, key=lambda span: abs(span.char_start - offset)).number]


__all__ = ["CHUNKING_VERSION", "Chunk", "ChunkingError", "chunk_document"]
