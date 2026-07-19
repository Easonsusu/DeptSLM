"""Unit coverage for Phase 8 feedback contracts and isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app import admin
from app.admin import _purge_limit
from app.models import (
    RagAnswerFeedback,
    RagAnswerFeedbackReason,
    RagAnswerFeedbackSourceTarget,
)
from app.rag_feedback_domain import (
    FeedbackContractError,
    FeedbackSentiment,
    FeedbackStatus,
    canonicalize_feedback,
    decode_feedback_cursor,
    encode_feedback_cursor,
    validate_review_transition,
)
from app.rag_feedback_services import PurgeResult
from app.settings import (
    DEFAULT_RAG_FEEDBACK_RETENTION_DAYS,
    ConfigurationError,
    _bounded_ascii_decimal,
)

pytestmark = pytest.mark.unit


def test_retention_default_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS", raising=False)
    assert (
        _bounded_ascii_decimal("DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS", 180, minimum=30, maximum=730)
        == DEFAULT_RAG_FEEDBACK_RETENTION_DAYS
    )
    for value in ("30", "730"):
        monkeypatch.setenv("DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS", value)
        assert _bounded_ascii_decimal(
            "DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS", 180, minimum=30, maximum=730
        ) == int(value)


@pytest.mark.parametrize("value", ["", "29", "731", "+30", "30.0", " 30", "30 ", "٣٠"])
def test_retention_rejects_malformed_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS", value)
    with pytest.raises(ConfigurationError):
        _bounded_ascii_decimal("DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS", 180, minimum=30, maximum=730)


@pytest.mark.parametrize(
    ("sentiment", "reasons", "sources", "answer_status", "expected_reasons", "expected_sources"),
    [
        (FeedbackSentiment.HELPFUL, [], [], "answered", (), ()),
        (
            FeedbackSentiment.HELPFUL,
            ["useful_citations", "clear"],
            [],
            "answered",
            ("clear", "useful_citations"),
            (),
        ),
        (
            FeedbackSentiment.UNHELPFUL,
            ["wrong_citation", "incorrect"],
            ["S2", "S1"],
            "answered",
            ("incorrect", "wrong_citation"),
            ("S1", "S2"),
        ),
        (
            FeedbackSentiment.REPORT,
            ["insufficient_when_expected"],
            [],
            "insufficient_information",
            ("insufficient_when_expected",),
            (),
        ),
    ],
)
def test_feedback_canonicalization(
    sentiment,
    reasons,
    sources,
    answer_status,
    expected_reasons,
    expected_sources,
) -> None:
    result = canonicalize_feedback(
        answer_status=answer_status,
        sentiment=sentiment,
        reason_codes=reasons,
        source_ids=sources,
        available_source_ids=("S1", "S2"),
    )
    assert result.reason_codes == expected_reasons
    assert result.source_ids == expected_sources


@pytest.mark.parametrize(
    ("sentiment", "reasons", "sources", "answer_status"),
    [
        (FeedbackSentiment.HELPFUL, ["incorrect"], [], "answered"),
        (FeedbackSentiment.HELPFUL, [], ["S1"], "answered"),
        (FeedbackSentiment.UNHELPFUL, [], [], "answered"),
        (FeedbackSentiment.REPORT, ["incorrect", "incorrect"], [], "answered"),
        (FeedbackSentiment.UNHELPFUL, ["wrong_citation"], [], "answered"),
        (FeedbackSentiment.UNHELPFUL, ["incorrect"], ["S1"], "answered"),
        (FeedbackSentiment.UNHELPFUL, ["wrong_citation"], ["S9"], "answered"),
        (
            FeedbackSentiment.UNHELPFUL,
            ["insufficient_when_expected"],
            [],
            "answered",
        ),
        (
            FeedbackSentiment.REPORT,
            ["wrong_citation"],
            ["S1"],
            "insufficient_information",
        ),
    ],
)
def test_feedback_contract_rejects_incompatible_inputs(
    sentiment, reasons, sources, answer_status
) -> None:
    with pytest.raises(FeedbackContractError):
        canonicalize_feedback(
            answer_status=answer_status,
            sentiment=sentiment,
            reason_codes=reasons,
            source_ids=sources,
            available_source_ids=("S1",),
        )


@pytest.mark.parametrize(
    ("current", "target", "resolution"),
    [
        ("open", FeedbackStatus.TRIAGED, None),
        ("open", FeedbackStatus.RESOLVED, "confirmed_quality_issue"),
        ("open", FeedbackStatus.DISMISSED, "duplicate"),
        ("triaged", FeedbackStatus.RESOLVED, "no_action_required"),
        ("triaged", FeedbackStatus.DISMISSED, "no_issue_found"),
    ],
)
def test_review_transition_contract_accepts_only_reviewed_paths(
    current, target, resolution
) -> None:
    validate_review_transition(
        current_status=current, new_status=target, resolution_code=resolution
    )


@pytest.mark.parametrize(
    ("current", "target", "resolution"),
    [
        ("open", FeedbackStatus.OPEN, None),
        ("triaged", FeedbackStatus.TRIAGED, None),
        ("resolved", FeedbackStatus.DISMISSED, "duplicate"),
        ("dismissed", FeedbackStatus.RESOLVED, "confirmed_quality_issue"),
        ("open", FeedbackStatus.TRIAGED, "duplicate"),
        ("open", FeedbackStatus.RESOLVED, "duplicate"),
        ("open", FeedbackStatus.DISMISSED, "confirmed_quality_issue"),
    ],
)
def test_review_transition_contract_rejects_backward_noop_or_mismatched_paths(
    current, target, resolution
) -> None:
    with pytest.raises(FeedbackContractError):
        validate_review_transition(
            current_status=current, new_status=target, resolution_code=resolution
        )


def test_feedback_cursor_is_opaque_and_bound_to_scope_and_filters() -> None:
    department_id = uuid4()
    created_at = datetime.now(UTC)
    feedback_id = uuid4()
    cursor = encode_feedback_cursor(
        department_id=department_id,
        status="open",
        sentiment="report",
        created_at=created_at,
        feedback_id=feedback_id,
    )
    assert str(department_id) not in cursor
    value = decode_feedback_cursor(
        cursor,
        department_id=department_id,
        status="open",
        sentiment="report",
    )
    assert value.feedback_id == feedback_id
    with pytest.raises(FeedbackContractError):
        decode_feedback_cursor(
            cursor,
            department_id=uuid4(),
            status="open",
            sentiment="report",
        )
    with pytest.raises(FeedbackContractError):
        decode_feedback_cursor(
            cursor,
            department_id=department_id,
            status="resolved",
            sentiment="report",
        )


@pytest.mark.parametrize("cursor", ["%%%", "not-json", "", "Ａ"])
def test_feedback_cursor_rejects_malformed_encoding(cursor: str) -> None:
    with pytest.raises(FeedbackContractError):
        decode_feedback_cursor(
            cursor,
            department_id=uuid4(),
            status=None,
            sentiment=None,
        )


@pytest.mark.parametrize("value", ["0", "1001", "+1", "1.0", " 1", "１"])
def test_purge_limit_is_strict_ascii(value: str) -> None:
    with pytest.raises(Exception):
        _purge_limit(value)
    assert _purge_limit("1") == 1
    assert _purge_limit("1000") == 1000


def test_feedback_models_have_no_prohibited_content_columns() -> None:
    columns = {
        column.name
        for table in (
            RagAnswerFeedback.__table__,
            RagAnswerFeedbackReason.__table__,
            RagAnswerFeedbackSourceTarget.__table__,
        )
        for column in table.columns
    }
    prohibited = {
        "question",
        "answer",
        "prompt",
        "comment",
        "note",
        "text",
        "evidence",
        "excerpt",
        "vector",
        "model_output",
        "filename",
        "path",
        "token",
        "qdrant_url",
    }
    assert columns.isdisjoint(prohibited)


def test_feedback_modules_have_no_external_retrieval_or_model_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "app"
    source = "\n".join(
        (root / name).read_text() for name in ("rag_feedback_domain.py", "rag_feedback_services.py")
    )
    for forbidden in (
        "qdrant_client",
        "DepartmentQdrant",
        "RagRuntimeClient",
        "Phase5ArtifactReader",
        "selected_chunk_reader",
        "sentence_transformers",
        "transformers",
        "torch",
        "model_store",
    ):
        assert forbidden not in source


def test_purge_cli_dry_run_output_is_content_free(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    department_id = uuid4()
    now = datetime.now(UTC)
    monkeypatch.setattr(admin.Settings, "from_environment", lambda: object())
    monkeypatch.setattr(
        admin,
        "purge_rag_feedback",
        lambda *args, **kwargs: PurgeResult(department_id, 2, now, now, 0, False),
    )
    assert (
        admin.main(
            [
                "purge-rag-feedback",
                "--department-id",
                str(department_id),
                "--actor-issuer",
                "https://issuer.invalid",
                "--actor-subject",
                "opaque-subject",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Eligible count: 2" in output
    for forbidden in ("reason", "source", "run", "question", "answer", "evidence"):
        assert forbidden not in output.lower()
