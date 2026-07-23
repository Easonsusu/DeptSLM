"""Focused unit coverage for Phase 8 hardening boundaries."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app import admin
from app.feedback_purge import (
    FeedbackPurgeConfigurationError,
    FeedbackPurgeSettings,
)
from app.feedback_request_body import (
    FEEDBACK_REVIEW_BODY_MAX_BYTES,
    FEEDBACK_SUBMIT_BODY_MAX_BYTES,
    FeedbackBodyError,
    read_bounded_json_object,
)
from app.rag_feedback_services import (
    PurgeResult,
    list_feedback_for_review,
    purge_feedback_batch,
)
from app.schemas import RagFeedbackReviewRequest, RagFeedbackSubmitRequest
from app.services import ServiceError

pytestmark = pytest.mark.unit


def _request(
    chunks: Iterable[bytes],
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    calls: list[int] | None = None,
) -> Request:
    pending = list(chunks)

    async def receive():
        if calls is not None:
            calls.append(1)
        if pending:
            return {
                "type": "http.request",
                "body": pending.pop(0),
                "more_body": bool(pending),
            }
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/feedback",
            "headers": headers or [],
        },
        receive,
    )


def _read(request: Request, *, maximum_bytes: int):
    return asyncio.run(read_bounded_json_object(request, maximum_bytes=maximum_bytes))


def test_declared_oversized_feedback_body_is_rejected_without_receive() -> None:
    calls: list[int] = []
    request = _request(
        [b'{"sentiment":"helpful"}'],
        headers=[(b"content-length", str(FEEDBACK_SUBMIT_BODY_MAX_BYTES + 1).encode())],
        calls=calls,
    )
    with pytest.raises(FeedbackBodyError) as captured:
        _read(request, maximum_bytes=FEEDBACK_SUBMIT_BODY_MAX_BYTES)
    assert captured.value.status_code == 413
    assert calls == []


@pytest.mark.parametrize(
    "maximum", [FEEDBACK_SUBMIT_BODY_MAX_BYTES, FEEDBACK_REVIEW_BODY_MAX_BYTES]
)
def test_chunked_oversized_feedback_body_stops_at_boundary(maximum: int) -> None:
    calls: list[int] = []
    request = _request([b"{" + b" " * maximum, b"ignored"], calls=calls)
    with pytest.raises(FeedbackBodyError) as captured:
        _read(request, maximum_bytes=maximum)
    assert captured.value.status_code == 413
    assert len(calls) == 1


def test_feedback_body_reader_writes_no_request_artifact(tmp_path: Path) -> None:
    before = tuple(tmp_path.iterdir())
    with pytest.raises(FeedbackBodyError):
        _read(
            _request([b"sensitive" * 1024]),
            maximum_bytes=FEEDBACK_REVIEW_BODY_MAX_BYTES,
        )
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\xff",
        b"{",
        b"[]",
        b'{} {"second":true}',
    ],
)
def test_feedback_body_rejects_empty_malformed_or_non_object_json(body: bytes) -> None:
    with pytest.raises(FeedbackBodyError) as captured:
        _read(_request([body]), maximum_bytes=FEEDBACK_REVIEW_BODY_MAX_BYTES)
    assert captured.value.status_code == 400
    decoded = body.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in captured.value.detail


def test_feedback_body_accepts_exact_limit_and_rejects_one_byte_over() -> None:
    exact = b"{}" + b" " * (FEEDBACK_REVIEW_BODY_MAX_BYTES - 2)
    assert _read(_request([exact]), maximum_bytes=FEEDBACK_REVIEW_BODY_MAX_BYTES) == {}
    with pytest.raises(FeedbackBodyError) as captured:
        _read(_request([exact + b" "]), maximum_bytes=FEEDBACK_REVIEW_BODY_MAX_BYTES)
    assert captured.value.status_code == 413


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"1"), (b"content-length", b"2")],
        [(b"content-length", b"+1")],
        [(b"content-length", b" 1")],
        [(b"content-length", "１".encode())],
    ],
)
def test_feedback_body_rejects_duplicate_conflicting_or_non_ascii_lengths(
    headers: list[tuple[bytes, bytes]],
) -> None:
    with pytest.raises(FeedbackBodyError) as captured:
        _read(_request([b"{}"], headers=headers), maximum_bytes=FEEDBACK_SUBMIT_BODY_MAX_BYTES)
    assert captured.value.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"sentiment": "helpful", "reason_codes": ["invented"], "source_ids": []},
        {"sentiment": "helpful", "reason_codes": [], "source_ids": ["S9"]},
        {
            "sentiment": "helpful",
            "reason_codes": [],
            "source_ids": [],
            "unknown": "value",
        },
    ],
)
def test_submit_schema_uses_only_reviewed_identifiers(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RagFeedbackSubmitRequest.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "resolved", "resolution_code": "invented", "expected_version": 1},
        {"status": "triaged", "resolution_code": None, "expected_version": True},
        {"status": "triaged", "resolution_code": None, "expected_version": "1"},
    ],
)
def test_review_schema_uses_reviewed_resolution_and_strict_integer(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RagFeedbackReviewRequest.model_validate(payload)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "1", 10**100])
def test_direct_purge_service_limit_fails_before_database(limit: object) -> None:
    with pytest.raises(ServiceError) as captured:
        purge_feedback_batch(None, None, None, limit=limit, apply=True)  # type: ignore[arg-type]
    assert captured.value.status_code == 422


@pytest.mark.parametrize("limit", [0, -1, 101, True, "1", 10**100])
def test_direct_review_list_limit_fails_before_cursor_or_database(limit: object) -> None:
    with pytest.raises(ServiceError) as captured:
        list_feedback_for_review(  # type: ignore[arg-type]
            None,
            None,
            None,
            status=None,
            sentiment=None,
            limit=limit,
            cursor="not-a-valid-cursor",
        )
    assert captured.value.status_code == 422


def test_feedback_purge_settings_load_only_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://db.invalid/deptslm")
    monkeypatch.delenv("DEPTSLM_DATA_DIR", raising=False)
    for name in (
        "DEPTSLM_QDRANT_URL",
        "DEPTSLM_QDRANT_API_KEY",
        "DEPTSLM_RAG_RUNTIME_URL",
        "DEPTSLM_RAG_RUNTIME_TOKEN",
        "DEPTSLM_EMBEDDING_MODEL_REVISION",
        "DEPTSLM_GENERATION_MODEL_REVISION",
        "DEPTSLM_RAG_FEEDBACK_RETENTION_DAYS",
    ):
        monkeypatch.setenv(name, "deliberately-invalid")
    settings = FeedbackPurgeSettings.from_environment()
    assert settings.database_url == "postgresql+psycopg://db.invalid/deptslm"
    assert tuple(settings.__dataclass_fields__) == ("database_url",)


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        "",
        "not a URL",
        "sqlite:///unsafe.db",
        "postgresql://db",
        "postgresql+psycopg://",
    ],
)
def test_feedback_purge_settings_fail_closed_for_database_url(
    monkeypatch: pytest.MonkeyPatch, database_url: str | None
) -> None:
    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", database_url)
    with pytest.raises(FeedbackPurgeConfigurationError, match="DATABASE_URL"):
        FeedbackPurgeSettings.from_environment()


def test_purge_cli_does_not_load_full_settings_or_touch_runtime_directories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    department_id = uuid4()
    now = datetime.now(UTC)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://db.invalid/deptslm")
    monkeypatch.delenv("DEPTSLM_DATA_DIR", raising=False)
    monkeypatch.setattr(
        admin.Settings,
        "from_environment",
        classmethod(lambda cls: pytest.fail("full settings must not load")),
    )
    monkeypatch.setattr(Path, "lstat", lambda self: pytest.fail("runtime lstat is forbidden"))
    monkeypatch.setattr(Path, "resolve", lambda self: pytest.fail("runtime resolve is forbidden"))
    monkeypatch.setattr(
        admin.os,
        "access",
        lambda *args: pytest.fail("runtime access is forbidden"),
    )
    monkeypatch.setattr(
        admin,
        "purge_rag_feedback",
        lambda *args, **kwargs: PurgeResult(department_id, 0, now, now, 0, False),
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


def test_purge_module_has_no_full_settings_or_runtime_storage_boundary() -> None:
    source = (Path(__file__).resolve().parents[1] / "app" / "feedback_purge.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "app.settings"
            assert not (
                node.module == "app" and any(item.name == "settings" for item in node.names)
            )
        if isinstance(node, ast.Import):
            assert all(item.name != "app.settings" for item in node.names)
    for forbidden in (
        "from app.settings import",
        "DEPTSLM_DATA_DIR",
        "DocumentStorage",
        "RagSettings",
        "Qdrant",
        "model_cache",
        "uploads",
        "extracted_text",
    ):
        assert forbidden not in source
