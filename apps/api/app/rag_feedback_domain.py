"""Reviewed Phase 8 feedback contracts with no free-text content."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class FeedbackSentiment(StrEnum):
    HELPFUL = "helpful"
    UNHELPFUL = "unhelpful"
    REPORT = "report"


class FeedbackStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class FeedbackReasonCode(StrEnum):
    CLEAR = "clear"
    COMPLETE = "complete"
    WELL_SUPPORTED = "well_supported"
    USEFUL_CITATIONS = "useful_citations"
    INCORRECT = "incorrect"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    MISSING_INFORMATION = "missing_information"
    WRONG_CITATION = "wrong_citation"
    IRRELEVANT_SOURCE = "irrelevant_source"
    UNSAFE_CONTENT = "unsafe_content"
    FORMATTING_PROBLEM = "formatting_problem"
    INSUFFICIENT_WHEN_EXPECTED = "insufficient_when_expected"
    OTHER_UNSPECIFIED = "other_unspecified"


class FeedbackSourceId(StrEnum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"
    S8 = "S8"


class FeedbackResolutionCode(StrEnum):
    CONFIRMED_QUALITY_ISSUE = "confirmed_quality_issue"
    CONFIRMED_SAFETY_ISSUE = "confirmed_safety_issue"
    ADDRESSED_EXTERNALLY = "addressed_externally"
    NO_ACTION_REQUIRED = "no_action_required"
    DUPLICATE = "duplicate"
    NOT_REPRODUCIBLE = "not_reproducible"
    OUT_OF_SCOPE = "out_of_scope"
    NO_ISSUE_FOUND = "no_issue_found"


HELPFUL_REASON_ORDER = (
    FeedbackReasonCode.CLEAR.value,
    FeedbackReasonCode.COMPLETE.value,
    FeedbackReasonCode.WELL_SUPPORTED.value,
    FeedbackReasonCode.USEFUL_CITATIONS.value,
)
NEGATIVE_REASON_ORDER = (
    FeedbackReasonCode.INCORRECT.value,
    FeedbackReasonCode.UNSUPPORTED_CLAIM.value,
    FeedbackReasonCode.MISSING_INFORMATION.value,
    FeedbackReasonCode.WRONG_CITATION.value,
    FeedbackReasonCode.IRRELEVANT_SOURCE.value,
    FeedbackReasonCode.UNSAFE_CONTENT.value,
    FeedbackReasonCode.FORMATTING_PROBLEM.value,
    FeedbackReasonCode.INSUFFICIENT_WHEN_EXPECTED.value,
    FeedbackReasonCode.OTHER_UNSPECIFIED.value,
)
REASON_ORDER = HELPFUL_REASON_ORDER + NEGATIVE_REASON_ORDER
RESOLVED_CODES = (
    FeedbackResolutionCode.CONFIRMED_QUALITY_ISSUE.value,
    FeedbackResolutionCode.CONFIRMED_SAFETY_ISSUE.value,
    FeedbackResolutionCode.ADDRESSED_EXTERNALLY.value,
    FeedbackResolutionCode.NO_ACTION_REQUIRED.value,
)
DISMISSED_CODES = (
    FeedbackResolutionCode.DUPLICATE.value,
    FeedbackResolutionCode.NOT_REPRODUCIBLE.value,
    FeedbackResolutionCode.OUT_OF_SCOPE.value,
    FeedbackResolutionCode.NO_ISSUE_FOUND.value,
)
RESOLUTION_CODES = RESOLVED_CODES + DISMISSED_CODES
TARGETING_REASONS = frozenset({"wrong_citation", "irrelevant_source"})
SOURCE_LABEL = re.compile(r"^S[1-8]$")


class FeedbackContractError(ValueError):
    """A safe request-contract failure without user content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CanonicalFeedback:
    sentiment: str
    reason_codes: tuple[str, ...]
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeedbackCursor:
    created_at: datetime
    feedback_id: UUID


def canonicalize_feedback(
    *,
    answer_status: str,
    sentiment: FeedbackSentiment,
    reason_codes: Sequence[str],
    source_ids: Sequence[str],
    available_source_ids: Sequence[str],
) -> CanonicalFeedback:
    """Validate and order one immutable submission using server-owned orderings."""

    if answer_status not in {"answered", "insufficient_information"}:
        raise FeedbackContractError("run_unavailable")
    if len(reason_codes) != len(set(reason_codes)):
        raise FeedbackContractError("duplicate_reason")
    if len(source_ids) != len(set(source_ids)):
        raise FeedbackContractError("duplicate_source")
    reason_set = set(reason_codes)
    source_set = set(source_ids)
    if not reason_set <= set(REASON_ORDER):
        raise FeedbackContractError("invalid_reason")
    if any(SOURCE_LABEL.fullmatch(item) is None for item in source_set):
        raise FeedbackContractError("invalid_source")

    if sentiment is FeedbackSentiment.HELPFUL:
        if len(reason_set) > 4 or not reason_set <= set(HELPFUL_REASON_ORDER):
            raise FeedbackContractError("invalid_reason")
        if source_set:
            raise FeedbackContractError("source_not_allowed")
    else:
        if not 1 <= len(reason_set) <= 5 or not reason_set <= set(NEGATIVE_REASON_ORDER):
            raise FeedbackContractError("invalid_reason")

    if "insufficient_when_expected" in reason_set and answer_status != "insufficient_information":
        raise FeedbackContractError("reason_run_mismatch")
    if reason_set & TARGETING_REASONS and answer_status != "answered":
        raise FeedbackContractError("reason_run_mismatch")
    requires_targets = bool(reason_set & TARGETING_REASONS)
    if requires_targets != bool(source_set):
        raise FeedbackContractError("source_required" if requires_targets else "source_not_allowed")
    available = set(available_source_ids)
    if not source_set <= available:
        raise FeedbackContractError("invalid_source")

    ordered_reasons = tuple(item for item in REASON_ORDER if item in reason_set)
    ordered_sources = tuple(sorted(source_set, key=lambda item: int(item[1:])))
    return CanonicalFeedback(sentiment.value, ordered_reasons, ordered_sources)


def validate_review_transition(
    *,
    current_status: str,
    new_status: FeedbackStatus,
    resolution_code: str | None,
) -> None:
    allowed = {
        "open": {"triaged", "resolved", "dismissed"},
        "triaged": {"resolved", "dismissed"},
    }
    if new_status.value not in allowed.get(current_status, set()):
        raise FeedbackContractError("invalid_transition")
    if new_status is FeedbackStatus.TRIAGED:
        if resolution_code is not None:
            raise FeedbackContractError("invalid_resolution")
    elif new_status is FeedbackStatus.RESOLVED:
        if resolution_code not in RESOLVED_CODES:
            raise FeedbackContractError("invalid_resolution")
    elif new_status is FeedbackStatus.DISMISSED and resolution_code not in DISMISSED_CODES:
        raise FeedbackContractError("invalid_resolution")


def encode_feedback_cursor(
    *,
    department_id: UUID,
    status: str | None,
    sentiment: str | None,
    created_at: datetime,
    feedback_id: UUID,
) -> str:
    payload = {
        "v": 1,
        "department_id": str(department_id),
        "status": status,
        "sentiment": sentiment,
        "order": "created_at_asc_id_asc",
        "created_at": created_at.isoformat(),
        "id": str(feedback_id),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_feedback_cursor(
    raw: str,
    *,
    department_id: UUID,
    status: str | None,
    sentiment: str | None,
) -> FeedbackCursor:
    if not raw or len(raw) > 1024 or not raw.isascii():
        raise FeedbackContractError("invalid_cursor")
    try:
        padding = "=" * (-len(raw) % 4)
        value: Any = json.loads(base64.b64decode(raw + padding, altchars=b"-_", validate=True))
        expected = {
            "v",
            "department_id",
            "status",
            "sentiment",
            "order",
            "created_at",
            "id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError
        if (
            type(value["v"]) is not int
            or value["v"] != 1
            or value["department_id"] != str(department_id)
            or value["status"] != status
            or value["sentiment"] != sentiment
            or value["order"] != "created_at_asc_id_asc"
        ):
            raise ValueError
        created_at = datetime.fromisoformat(value["created_at"])
        feedback_id = UUID(value["id"])
        if created_at.utcoffset() is None or feedback_id.int == 0:
            raise ValueError
        return FeedbackCursor(created_at, feedback_id)
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ) as error:
        raise FeedbackContractError("invalid_cursor") from error
