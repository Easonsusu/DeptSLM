"""Fixed Phase 9 evaluation contracts and deterministic metric functions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any
from uuid import UUID

from app.rag_domain import (
    ANSWER_CONTRACT_VERSION,
    GENERATION_MODEL_ID,
    GENERATION_MODEL_REVISION,
    PROMPT_VERSION,
)
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    QDRANT_COLLECTION,
    QUERY_EMBEDDING_PIPELINE_VERSION,
    VECTOR_SCHEMA_VERSION,
)

SUITE_CONTRACT_VERSION = "phase9-evaluation-suite-v1"
ARTIFACT_CONTRACT_VERSION = "phase9-evaluation-artifact-v1"
METRIC_CONTRACT_VERSION = "phase9-deterministic-metrics-v1"
ANSWER_NORMALIZATION_VERSION = "phase9-answer-normalization-v1"
GATE_POLICY_VERSION = "phase9-quality-gates-v1"
RUNNER_CONTRACT_VERSION = "phase9-evaluation-runner-v1"
SEED_DERIVATION_VERSION = "phase9-case-seed-v1"

MAX_SUITE_CASES = 500
MAX_SUITE_INPUT_BYTES = 16 * 1024 * 1024
MAX_ACCEPTED_ANSWER_CHARS = 4000
MAX_CASE_JSONL_LINE_BYTES = 128 * 1024
MAX_RUN_BODY_BYTES = 128
MAX_CANCEL_BODY_BYTES = 1024
MAX_BASE_SEED = (1 << 63) - 1

GATE_NAMES = (
    "retrieval_recall_at_5_min",
    "retrieval_mrr_at_20_min",
    "answer_status_accuracy_min",
    "citation_precision_min",
    "citation_recall_min",
    "normalized_exact_match_min",
    "character_f1_min",
    "invalid_contract_rate_max",
)

SAFE_EVALUATION_ERROR_CODES = frozenset(
    {
        "suite_artifact_missing",
        "suite_artifact_mismatch",
        "suite_contract_invalid",
        "suite_source_stale",
        "department_unavailable",
        "requester_unauthorized",
        "database_unavailable",
        "qdrant_unavailable",
        "retrieval_authority_failed",
        "source_artifact_missing",
        "source_artifact_mismatch",
        "runtime_unavailable",
        "runtime_timeout",
        "invalid_query_embedding",
        "generation_failed",
        "invalid_generation_response",
        "invalid_citation",
        "result_publication_failed",
        "claim_lost",
        "cancelled",
    }
)

_ASCII_DECIMAL = re.compile(r"^(?:0|1)(?:\.[0-9]{1,4})?$")


class EvaluationContractError(RuntimeError):
    """A content-free reviewed evaluation contract failure."""

    def __init__(self, code: str = "suite_contract_invalid") -> None:
        self.code = code if code in SAFE_EVALUATION_ERROR_CODES else "suite_contract_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class QualityGates:
    retrieval_recall_at_5_min: Decimal
    retrieval_mrr_at_20_min: Decimal
    answer_status_accuracy_min: Decimal
    citation_precision_min: Decimal
    citation_recall_min: Decimal
    normalized_exact_match_min: Decimal
    character_f1_min: Decimal
    invalid_contract_rate_max: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return {name: getattr(self, name) for name in GATE_NAMES}


@dataclass(frozen=True, slots=True)
class EvaluationCaseScore:
    case_id: UUID
    expected_status: str
    actual_status: str
    relevant_chunk_count: int
    retrieved_relevant_at_5: int
    retrieved_relevant_at_10: int
    retrieved_relevant_at_20: int
    reciprocal_rank_at_20: Decimal
    status_correct: bool
    cited_count: int
    cited_relevant_count: int
    citation_precision: Decimal
    citation_recall: Decimal
    normalized_exact_match: Decimal
    character_f1: Decimal
    answer_contract_valid: bool
    case_gate_passed: bool
    error_code: str | None


@dataclass(frozen=True, slots=True)
class AggregateMetrics:
    retrieval_recall_at_5: Decimal
    retrieval_recall_at_10: Decimal
    retrieval_recall_at_20: Decimal
    retrieval_mrr_at_20: Decimal
    answer_status_accuracy: Decimal
    citation_precision: Decimal
    citation_recall: Decimal
    normalized_exact_match: Decimal
    character_f1: Decimal
    invalid_contract_rate: Decimal

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "retrieval_recall_at_5": self.retrieval_recall_at_5,
            "retrieval_recall_at_10": self.retrieval_recall_at_10,
            "retrieval_recall_at_20": self.retrieval_recall_at_20,
            "retrieval_mrr_at_20": self.retrieval_mrr_at_20,
            "answer_status_accuracy": self.answer_status_accuracy,
            "citation_precision": self.citation_precision,
            "citation_recall": self.citation_recall,
            "normalized_exact_match": self.normalized_exact_match,
            "character_f1": self.character_f1,
            "invalid_contract_rate": self.invalid_contract_rate,
        }


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    passed: bool
    failed_count: int
    results: dict[str, bool]


@dataclass(frozen=True, slots=True)
class EvaluationCursor:
    created_at: datetime
    resource_id: UUID


def encode_evaluation_cursor(
    *,
    department_id: UUID,
    resource: str,
    created_at: datetime,
    resource_id: UUID,
) -> str:
    if resource not in {"suite", "run"} or created_at.utcoffset() is None:
        raise EvaluationContractError()
    payload = {
        "v": 1,
        "department_id": str(department_id),
        "resource": resource,
        "order": "created_at_desc_id_asc",
        "created_at": created_at.isoformat(),
        "id": str(resource_id),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_evaluation_cursor(
    raw: str,
    *,
    department_id: UUID,
    resource: str,
) -> EvaluationCursor:
    if not raw or len(raw) > 1024 or not raw.isascii() or resource not in {"suite", "run"}:
        raise EvaluationContractError()
    try:
        padding = "=" * (-len(raw) % 4)
        value: Any = json.loads(base64.b64decode(raw + padding, altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {
            "v",
            "department_id",
            "resource",
            "order",
            "created_at",
            "id",
        }:
            raise ValueError
        if (
            type(value["v"]) is not int
            or value["v"] != 1
            or value["department_id"] != str(department_id)
            or value["resource"] != resource
            or value["order"] != "created_at_desc_id_asc"
        ):
            raise ValueError
        created_at = datetime.fromisoformat(value["created_at"])
        resource_id = UUID(value["id"])
        if created_at.utcoffset() is None or resource_id.int == 0:
            raise ValueError
        return EvaluationCursor(created_at, resource_id)
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ) as error:
        raise EvaluationContractError() from error


def parse_quality_gates(value: object) -> QualityGates:
    if not isinstance(value, dict) or set(value) != set(GATE_NAMES):
        raise EvaluationContractError()
    parsed: dict[str, Decimal] = {}
    for name in GATE_NAMES:
        raw = value[name]
        if not isinstance(raw, str) or _ASCII_DECIMAL.fullmatch(raw) is None:
            raise EvaluationContractError()
        try:
            threshold = Decimal(raw)
        except InvalidOperation as error:
            raise EvaluationContractError() from error
        if not threshold.is_finite() or not Decimal(0) <= threshold <= Decimal(1):
            raise EvaluationContractError()
        parsed[name] = threshold
    return QualityGates(**parsed)


def normalize_answer(value: str) -> str:
    if not isinstance(value, str):
        raise EvaluationContractError("invalid_generation_response")
    normalized = unicodedata.normalize("NFC", value).casefold().strip()
    return " ".join(normalized.split())


def character_f1(actual: str, expected: str) -> Decimal:
    actual_counts = Counter(
        character for character in normalize_answer(actual) if not character.isspace()
    )
    expected_counts = Counter(
        character for character in normalize_answer(expected) if not character.isspace()
    )
    if not actual_counts and not expected_counts:
        return Decimal(1)
    if not actual_counts or not expected_counts:
        return Decimal(0)
    overlap = sum((actual_counts & expected_counts).values())
    with localcontext() as context:
        context.prec = 40
        precision = Decimal(overlap) / Decimal(sum(actual_counts.values()))
        recall = Decimal(overlap) / Decimal(sum(expected_counts.values()))
        if precision + recall == 0:
            return Decimal(0)
        return (Decimal(2) * precision * recall) / (precision + recall)


def score_case(
    *,
    case_id: UUID,
    expected_status: str,
    relevant_chunk_ids: Sequence[UUID],
    accepted_answers: Sequence[str],
    actual_status: str,
    generated_answer: str,
    authorized_candidate_ids: Sequence[UUID],
    cited_chunk_ids: Sequence[UUID],
    answer_contract_valid: bool,
    error_code: str | None = None,
) -> EvaluationCaseScore:
    if expected_status not in {"answered", "insufficient_information"}:
        raise EvaluationContractError()
    candidates = tuple(authorized_candidate_ids)
    citations = tuple(cited_chunk_ids)
    if len(candidates) != len(set(candidates)) or len(citations) != len(set(citations)):
        answer_contract_valid = False
        error_code = "retrieval_authority_failed"
    relevant = set(relevant_chunk_ids)
    top5 = sum(item in relevant for item in candidates[:5])
    top10 = sum(item in relevant for item in candidates[:10])
    top20 = sum(item in relevant for item in candidates[:20])
    reciprocal_rank = Decimal(0)
    for rank, item in enumerate(candidates[:20], 1):
        if item in relevant:
            reciprocal_rank = Decimal(1) / Decimal(rank)
            break
    cited_relevant = sum(item in relevant for item in citations)
    if citations:
        citation_precision = Decimal(cited_relevant) / Decimal(len(citations))
    else:
        citation_precision = Decimal(1) if not relevant else Decimal(0)
    citation_recall = Decimal(cited_relevant) / Decimal(len(relevant)) if relevant else Decimal(1)
    exact = Decimal(0)
    f1 = Decimal(0)
    if expected_status == "answered" and actual_status == "answered" and answer_contract_valid:
        normalized_actual = normalize_answer(generated_answer)
        exact = max(
            (
                Decimal(normalized_actual == normalize_answer(expected))
                for expected in accepted_answers
            ),
            default=Decimal(0),
        )
        f1 = max(
            (character_f1(generated_answer, expected) for expected in accepted_answers),
            default=Decimal(0),
        )
    status_correct = actual_status == expected_status and answer_contract_valid
    case_gate = status_correct and answer_contract_valid
    return EvaluationCaseScore(
        case_id=case_id,
        expected_status=expected_status,
        actual_status=actual_status,
        relevant_chunk_count=len(relevant),
        retrieved_relevant_at_5=top5,
        retrieved_relevant_at_10=top10,
        retrieved_relevant_at_20=top20,
        reciprocal_rank_at_20=reciprocal_rank,
        status_correct=status_correct,
        cited_count=len(citations),
        cited_relevant_count=cited_relevant,
        citation_precision=citation_precision,
        citation_recall=citation_recall,
        normalized_exact_match=exact,
        character_f1=f1,
        answer_contract_valid=answer_contract_valid,
        case_gate_passed=case_gate,
        error_code=error_code,
    )


def aggregate_metrics(scores: Sequence[EvaluationCaseScore]) -> AggregateMetrics:
    if not scores:
        raise EvaluationContractError()
    answered = [item for item in scores if item.expected_status == "answered"]
    if not answered:
        raise EvaluationContractError()

    def average(values: Sequence[Decimal]) -> Decimal:
        if not values:
            raise EvaluationContractError()
        with localcontext() as context:
            context.prec = 40
            return sum(values, Decimal(0)) / Decimal(len(values))

    return AggregateMetrics(
        retrieval_recall_at_5=average(
            [
                Decimal(item.retrieved_relevant_at_5) / Decimal(item.relevant_chunk_count)
                for item in answered
            ]
        ),
        retrieval_recall_at_10=average(
            [
                Decimal(item.retrieved_relevant_at_10) / Decimal(item.relevant_chunk_count)
                for item in answered
            ]
        ),
        retrieval_recall_at_20=average(
            [
                Decimal(item.retrieved_relevant_at_20) / Decimal(item.relevant_chunk_count)
                for item in answered
            ]
        ),
        retrieval_mrr_at_20=average([item.reciprocal_rank_at_20 for item in answered]),
        answer_status_accuracy=average([Decimal(int(item.status_correct)) for item in scores]),
        citation_precision=average([item.citation_precision for item in answered]),
        citation_recall=average([item.citation_recall for item in answered]),
        normalized_exact_match=average([item.normalized_exact_match for item in answered]),
        character_f1=average([item.character_f1 for item in answered]),
        invalid_contract_rate=average(
            [Decimal(int(not item.answer_contract_valid)) for item in scores]
        ),
    )


def evaluate_gates(metrics: AggregateMetrics, gates: QualityGates) -> GateEvaluation:
    results = {
        "retrieval_recall_at_5_min": (
            metrics.retrieval_recall_at_5 >= gates.retrieval_recall_at_5_min
        ),
        "retrieval_mrr_at_20_min": (metrics.retrieval_mrr_at_20 >= gates.retrieval_mrr_at_20_min),
        "answer_status_accuracy_min": (
            metrics.answer_status_accuracy >= gates.answer_status_accuracy_min
        ),
        "citation_precision_min": (metrics.citation_precision >= gates.citation_precision_min),
        "citation_recall_min": metrics.citation_recall >= gates.citation_recall_min,
        "normalized_exact_match_min": (
            metrics.normalized_exact_match >= gates.normalized_exact_match_min
        ),
        "character_f1_min": metrics.character_f1 >= gates.character_f1_min,
        "invalid_contract_rate_max": (
            metrics.invalid_contract_rate <= gates.invalid_contract_rate_max
        ),
    }
    failed = sum(not result for result in results.values())
    return GateEvaluation(failed == 0, failed, results)


def derive_base_seed(run_id: UUID) -> int:
    digest = hashlib.sha256(f"{RUNNER_CONTRACT_VERSION}:{run_id}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") & MAX_BASE_SEED


def derive_case_seed(base_seed: int, case_id: UUID) -> int:
    if (
        isinstance(base_seed, bool)
        or not isinstance(base_seed, int)
        or not 0 <= base_seed <= MAX_BASE_SEED
    ):
        raise EvaluationContractError()
    payload = f"{SEED_DERIVATION_VERSION}:{base_seed}:{case_id}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & MAX_BASE_SEED


def production_contract() -> Mapping[str, object]:
    return {
        "query_embedding_pipeline_version": QUERY_EMBEDDING_PIPELINE_VERSION,
        "query_embedding_model_id": EMBEDDING_MODEL_ID,
        "query_embedding_model_revision": EMBEDDING_MODEL_REVISION,
        "query_embedding_dimension": EMBEDDING_DIMENSION,
        "query_embedding_distance": EMBEDDING_DISTANCE,
        "generation_model_id": GENERATION_MODEL_ID,
        "generation_model_revision": GENERATION_MODEL_REVISION,
        "prompt_version": PROMPT_VERSION,
        "answer_contract_version": ANSWER_CONTRACT_VERSION,
        "qdrant_collection": QDRANT_COLLECTION,
        "vector_schema_version": VECTOR_SCHEMA_VERSION,
    }
