"""Deterministic Phase 5 text normalization and provenance mapping."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from deptslm_worker.domain import NORMALIZATION_VERSION


class NormalizationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProvenanceSpan:
    char_start: int
    char_end: int
    number: int


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    text: str
    provenance_kind: str
    spans: tuple[ProvenanceSpan, ...]
    version: str = NORMALIZATION_VERSION


def normalize_text_source(raw: bytes) -> NormalizedDocument:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NormalizationError("invalid_utf8") from error
    normalized = _normalize(text)
    return NormalizedDocument(normalized, "line", _line_spans(normalized))


def normalize_pdf_pages(pages: list[str]) -> NormalizedDocument:
    output: list[str] = []
    spans: list[ProvenanceSpan] = []
    offset = 0
    for page_number, page_text in enumerate(pages, start=1):
        normalized = _normalize(page_text, require_content=False)
        if page_number > 1:
            separator = "\n\f\n"
            output.append(separator)
            offset += len(separator)
        page_start = offset
        output.append(normalized)
        offset += len(normalized)
        if normalized:
            spans.append(ProvenanceSpan(page_start, offset, page_number))
    text = "".join(output)
    if not text.strip():
        raise NormalizationError("no_extractable_text")
    return NormalizedDocument(text, "page", tuple(spans))


def _normalize(text: str, *, require_content: bool = True) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    if "\x00" in text:
        raise NormalizationError("invalid_utf8")
    if require_content and not text.strip():
        raise NormalizationError("no_extractable_text")
    return text


def _line_spans(text: str) -> tuple[ProvenanceSpan, ...]:
    spans: list[ProvenanceSpan] = []
    offset = 0
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        end = offset + len(line)
        spans.append(ProvenanceSpan(offset, end, number))
        offset = end
    if offset < len(text) or not spans:
        spans.append(ProvenanceSpan(offset, len(text), len(spans) + 1))
    return tuple(spans)
