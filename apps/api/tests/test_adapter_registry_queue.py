"""Queue contract smoke tests for Phase 12.1C."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.adapter_registry_queue as queue
from app.adapter_registry_queue import AdapterRegistryQueueError


def test_unknown_queue_error_code_is_safe() -> None:
    assert AdapterRegistryQueueError("not-safe").code == "adapter_registry_publication_failed"


class _Transaction:
    def __init__(self, session: object) -> None:
        self.session = session

    def __enter__(self) -> object:
        return self.session

    def __exit__(self, *_ignored: object) -> None:
        return None


class _Factory:
    def __init__(self, session: object) -> None:
        self.session = session

    def begin(self) -> _Transaction:
        return _Transaction(self.session)


class _Rows:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> list[object]:
        return self.rows


def _adapter() -> SimpleNamespace:
    return SimpleNamespace(
        status="running",
        error_code=None,
        worker_id="worker",
        claim_token="token",
        lease_expires_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        finished_at=None,
        validated_at=None,
        purged_at=None,
        version=7,
    )


def _claim(status: str) -> SimpleNamespace:
    return SimpleNamespace(registry_attempt_status=status)


@pytest.mark.parametrize("status", ["staged", "published"])
def test_failure_preserves_staged_and_published_attempt_ownership(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    adapter = _adapter()
    manifest = {"attempt": status}
    attempt = SimpleNamespace(
        status=status,
        error_code=None,
        worker_id="historical-worker",
        claimed_at=datetime.now(UTC),
        ownership_manifest=manifest,
        staged_at=datetime.now(UTC),
        published_at=datetime.now(UTC) if status == "published" else None,
        finished_at=None,
        version=4 if status == "published" else 3,
    )
    monkeypatch.setattr(queue, "_live_claim", lambda _session, _claim: adapter)
    monkeypatch.setattr(queue, "_attempt", lambda _session, _claim: attempt)
    session = SimpleNamespace(scalar=lambda _query: datetime.now(UTC))

    queue._record_failure(
        _Factory(session), _claim(status), "adapter_file_too_large", validation=True
    )

    assert adapter.status == "validation_failed"
    assert attempt.status == "validation_failed"
    assert attempt.ownership_manifest == manifest
    assert attempt.worker_id == "historical-worker"
    assert attempt.claimed_at is not None
    assert attempt.staged_at is not None
    if status == "published":
        assert attempt.published_at is not None


def test_failure_before_staging_clears_active_attempt_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    attempt = SimpleNamespace(
        status="running",
        error_code=None,
        worker_id="worker",
        claimed_at=datetime.now(UTC),
        ownership_manifest=None,
        finished_at=None,
        version=2,
    )
    monkeypatch.setattr(queue, "_live_claim", lambda _session, _claim: adapter)
    monkeypatch.setattr(queue, "_attempt", lambda _session, _claim: attempt)
    session = SimpleNamespace(scalar=lambda _query: datetime.now(UTC))

    queue._record_failure(_Factory(session), _claim("running"), "claim_lost", validation=False)

    assert adapter.status == "failed"
    assert adapter.worker_id is None
    assert adapter.claim_token is None
    assert attempt.status == "failed"
    assert attempt.worker_id is None
    assert attempt.claimed_at is None
    assert attempt.ownership_manifest is None


def test_lost_claim_refuses_failure_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter()

    def lost(_session: object, _claim: object) -> None:
        raise AdapterRegistryQueueError("claim_lost")

    monkeypatch.setattr(queue, "_live_claim", lost)
    queue._record_failure(
        _Factory(SimpleNamespace()), _claim("running"), "claim_lost", validation=False
    )

    assert adapter.status == "running"
    assert adapter.worker_id == "worker"


@pytest.mark.parametrize("failure", ["adapter", "attempt"])
def test_failure_version_fences_refuse_mutation(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    adapter = _adapter()
    attempt = SimpleNamespace(status="running", version=2)
    if failure == "adapter":

        def fenced(_session: object, _claim: object) -> None:
            raise AdapterRegistryQueueError("adapter_registry_authority_changed")

        monkeypatch.setattr(queue, "_live_claim", fenced)
    else:
        monkeypatch.setattr(queue, "_live_claim", lambda _session, _claim: adapter)

        def fenced_attempt(_session: object, _claim: object) -> None:
            raise AdapterRegistryQueueError("adapter_registry_authority_changed")

        monkeypatch.setattr(queue, "_attempt", fenced_attempt)
    queue._record_failure(
        _Factory(SimpleNamespace()), _claim("running"), "claim_lost", validation=False
    )

    assert adapter.status == "running"
    assert adapter.worker_id == "worker"
    assert attempt.status == "running"


def test_terminal_claim_failure_preserves_published_surface() -> None:
    adapter = SimpleNamespace(
        id="adapter",
        department_id="department",
        publication_attempt_id="publication",
        execution_scope_id="scope",
        attempt_number=1,
        code_revision="a" * 40,
        status="running",
        error_code=None,
        worker_id="worker",
        claim_token="token",
        lease_expires_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        finished_at=None,
        validated_at=None,
        purged_at=None,
        version=2,
    )
    manifest = {"manifest": "published"}
    attempt = SimpleNamespace(
        id="attempt-id",
        adapter_id="adapter",
        department_id="department",
        publication_attempt_id="publication",
        execution_scope_id="scope",
        attempt_number=1,
        code_revision="a" * 40,
        status="published",
        error_code=None,
        worker_id="worker",
        claimed_at=datetime.now(UTC),
        ownership_manifest=manifest,
        staged_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        finished_at=None,
        cleanup_confirmed_at=None,
        version=4,
    )
    session = SimpleNamespace(execute=lambda _query: _Rows([attempt]))
    queue._terminal_claim_failure(
        session, adapter, datetime.now(UTC), "claim_lost", attempt=attempt
    )

    assert adapter.status == "failed"
    assert attempt.status == "failed"
    assert attempt.worker_id == "worker"
    assert attempt.claimed_at is not None
    assert attempt.ownership_manifest == manifest


def test_terminal_claim_failure_rejects_substituted_attempt() -> None:
    adapter = SimpleNamespace(
        id="adapter",
        department_id="department",
        publication_attempt_id="publication",
        execution_scope_id="scope",
        attempt_number=1,
        code_revision="a" * 40,
        status="running",
        error_code=None,
        worker_id="worker",
        claim_token="token",
        lease_expires_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        finished_at=None,
        validated_at=None,
        purged_at=None,
        version=2,
    )
    attempt = SimpleNamespace(
        id="attempt-id",
        adapter_id="other-adapter",
        department_id="department",
        publication_attempt_id="publication",
        execution_scope_id="scope",
        attempt_number=1,
        code_revision="a" * 40,
        status="published",
        error_code=None,
        worker_id="historical-worker",
        claimed_at=datetime.now(UTC),
        ownership_manifest={"manifest": "published"},
        staged_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        finished_at=None,
        cleanup_confirmed_at=None,
        version=4,
    )
    active = SimpleNamespace(
        id="actual-attempt-id",
        adapter_id="adapter",
        department_id="department",
        publication_attempt_id="publication",
        execution_scope_id="scope",
        attempt_number=1,
        code_revision="a" * 40,
        status="published",
        error_code=None,
        worker_id="worker",
        claimed_at=datetime.now(UTC),
        ownership_manifest={"manifest": "published"},
        staged_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        finished_at=None,
        cleanup_confirmed_at=None,
        version=4,
    )
    session = SimpleNamespace(execute=lambda _query: _Rows([active]))
    with pytest.raises(AdapterRegistryQueueError, match="claim_lost"):
        queue._terminal_claim_failure(
            session, adapter, datetime.now(UTC), "claim_lost", attempt=attempt
        )

    assert adapter.status == "running"
    assert attempt.status == "published"


def _terminal_surface() -> tuple[SimpleNamespace, SimpleNamespace]:
    adapter = SimpleNamespace(
        id="adapter",
        department_id="department",
        publication_attempt_id="publication",
        execution_scope_id="scope",
        attempt_number=1,
        code_revision="a" * 40,
        status="running",
        error_code=None,
        worker_id="worker",
        claim_token="token",
        lease_expires_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        finished_at=None,
        validated_at=None,
        purged_at=None,
        version=2,
    )
    attempt = SimpleNamespace(
        id="attempt-id",
        adapter_id="adapter",
        department_id="department",
        publication_attempt_id="publication",
        execution_scope_id="scope",
        attempt_number=1,
        code_revision="a" * 40,
        status="published",
        error_code=None,
        worker_id="worker",
        claimed_at=datetime.now(UTC),
        staged_at=datetime.now(UTC),
        published_at=datetime.now(UTC),
        ownership_manifest={"manifest": "published"},
        finished_at=None,
        cleanup_confirmed_at=None,
        version=4,
    )
    return adapter, attempt


@pytest.mark.parametrize(
    "mutation",
    [
        lambda attempt: setattr(attempt, "version", 3),
        lambda attempt: setattr(attempt, "worker_id", "other-worker"),
        lambda attempt: setattr(attempt, "ownership_manifest", None),
    ],
)
def test_terminal_claim_failure_refuses_malformed_active_surface(mutation) -> None:
    adapter, attempt = _terminal_surface()
    mutation(attempt)
    session = SimpleNamespace(execute=lambda _query: _Rows([attempt]))

    with pytest.raises(AdapterRegistryQueueError, match="claim_lost"):
        queue._terminal_claim_failure(session, adapter, datetime.now(UTC), "claim_lost")

    assert adapter.status == "running"
    assert adapter.error_code is None
    assert attempt.status == "published"
    assert attempt.error_code is None


def test_terminal_claim_failure_refuses_zero_or_multiple_active_rows() -> None:
    adapter, attempt = _terminal_surface()
    empty = SimpleNamespace(execute=lambda _query: _Rows([]))
    with pytest.raises(AdapterRegistryQueueError, match="claim_lost"):
        queue._terminal_claim_failure(empty, adapter, datetime.now(UTC), "claim_lost")
    assert adapter.status == "running"

    _other_adapter, other_attempt = _terminal_surface()
    other_attempt.id = "other-attempt-id"
    multiple = SimpleNamespace(execute=lambda _query: _Rows([attempt, other_attempt]))
    with pytest.raises(AdapterRegistryQueueError, match="claim_lost"):
        queue._terminal_claim_failure(multiple, adapter, datetime.now(UTC), "claim_lost")
    assert adapter.status == "running"
    assert attempt.status == "published"
    assert other_attempt.status == "published"
