"""Fixed Phase 7 grounded-answer contracts and content validation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.vector_index_domain import (
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    QUERY_EMBEDDING_INSTRUCTION,
    QUERY_EMBEDDING_PIPELINE_VERSION,
)

GENERATION_MODEL_ID = "Qwen/Qwen3-0.6B"
GENERATION_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
PROMPT_VERSION = "phase7-grounded-answer-prompt-v1"
ANSWER_CONTRACT_VERSION = "phase7-grounded-answer-v1"
INSUFFICIENT_INFORMATION_MESSAGE = (
    "I do not have enough information in the authorized department sources to answer that question."
)
MAX_QUESTION_CHARS = 2000
MAX_PUBLIC_ANSWER_CHARS = 4000
MAX_SOURCE_CHARS = 1200
MAX_RUNTIME_BODY_BYTES = 2 * 1024 * 1024
SOURCE_LABEL = re.compile(r"^S[1-8]$")
ANSWER_REFERENCE = re.compile(r"\[S[0-9]+\]")

SAFE_RAG_ERROR_CODES = frozenset(
    {
        "runtime_unavailable",
        "runtime_timeout",
        "query_embedding_failed",
        "invalid_query_embedding",
        "qdrant_unavailable",
        "retrieval_authority_failed",
        "source_artifact_missing",
        "source_artifact_mismatch",
        "source_changed",
        "generation_failed",
        "generation_timeout",
        "invalid_generation_response",
        "invalid_citation",
        "department_unavailable",
        "database_unavailable",
    }
)


class RagContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in SAFE_RAG_ERROR_CODES:
            code = "invalid_generation_response"
        self.code = code
        super().__init__(code)


def normalize_question(value: str) -> str:
    """Normalize user text without interpreting any embedded instruction syntax."""

    if not isinstance(value, str):
        raise ValueError("question must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > MAX_QUESTION_CHARS:
        raise ValueError("question must contain between 1 and 2000 characters")
    if "\x00" in normalized or any(_disallowed_control(character) for character in normalized):
        raise ValueError("question contains a disallowed control character")
    if not any(not character.isspace() for character in normalized):
        raise ValueError("question must not be blank")
    return normalized


def _disallowed_control(character: str) -> bool:
    return unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}


@dataclass(frozen=True, slots=True)
class EvidenceSource:
    label: str
    text: str

    def runtime_value(self) -> dict[str, str]:
        if SOURCE_LABEL.fullmatch(self.label) is None or not self.text:
            raise RagContractError("source_artifact_mismatch")
        return {"source_id": self.label, "text": self.text[:MAX_SOURCE_CHARS]}


@dataclass(frozen=True, slots=True)
class GenerationResult:
    status: str
    answer: str
    citations: tuple[str, ...]


def validate_generation_response(value: Any, known_labels: tuple[str, ...]) -> GenerationResult:
    """Fail closed on malformed answers or citations not owned by the API."""

    if not isinstance(value, dict) or set(value) != {"status", "answer", "citations"}:
        raise RagContractError("invalid_generation_response")
    status = value.get("status")
    answer = value.get("answer")
    citations = value.get("citations")
    if status not in {"answered", "insufficient_information"}:
        raise RagContractError("invalid_generation_response")
    if not isinstance(answer, str) or not isinstance(citations, list):
        raise RagContractError("invalid_generation_response")
    if len(answer) > MAX_PUBLIC_ANSWER_CHARS or "\x00" in answer:
        raise RagContractError("invalid_generation_response")
    if any(_disallowed_control(character) for character in answer):
        raise RagContractError("invalid_generation_response")
    if "<think" in answer.casefold() or "</think" in answer.casefold():
        raise RagContractError("invalid_generation_response")
    if any(not isinstance(item, str) for item in citations) or len(set(citations)) != len(
        citations
    ):
        raise RagContractError("invalid_citation")
    known = set(known_labels)
    if any(SOURCE_LABEL.fullmatch(item) is None or item not in known for item in citations):
        raise RagContractError("invalid_citation")
    references = ANSWER_REFERENCE.findall(answer)
    reference_labels = [item[1:-1] for item in references]
    if any(item not in known for item in reference_labels):
        raise RagContractError("invalid_citation")
    ordered_references = tuple(dict.fromkeys(reference_labels))
    if status == "insufficient_information":
        if answer != "" or citations or references:
            raise RagContractError("invalid_generation_response")
        return GenerationResult(status, "", ())
    if not answer.strip() or not citations or ordered_references != tuple(citations):
        raise RagContractError("invalid_citation")
    return GenerationResult(status, answer, tuple(citations))


def runtime_generation_request(
    question: str, evidence: tuple[EvidenceSource, ...]
) -> dict[str, Any]:
    return {
        "question": question,
        "evidence": [source.runtime_value() for source in evidence],
        "prompt_version": PROMPT_VERSION,
        "answer_contract_version": ANSWER_CONTRACT_VERSION,
    }


MODEL_CONTRACT = {
    "query_embedding_pipeline_version": QUERY_EMBEDDING_PIPELINE_VERSION,
    "query_embedding_model_id": EMBEDDING_MODEL_ID,
    "query_embedding_model_revision": EMBEDDING_MODEL_REVISION,
    "query_embedding_instruction": QUERY_EMBEDDING_INSTRUCTION,
    "generation_model_id": GENERATION_MODEL_ID,
    "generation_model_revision": GENERATION_MODEL_REVISION,
    "prompt_version": PROMPT_VERSION,
    "answer_contract_version": ANSWER_CONTRACT_VERSION,
}


@dataclass(frozen=True, slots=True)
class SelectedSource:
    chunk_id: UUID
    label: str
