"""PostgreSQL 16 coverage for the Phase 14.1 execution control plane."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import sessionmaker
from test_phase11_postgres import _succeeded_training_job, _unique_code_revision

from alembic import command
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine
from app.models import (
    PersistentAuditEvent,
    TrainingExecution,
    TrainingExecutionAttempt,
    TrainingJob,
    TrainingJobArtifactOperation,
    TrainingJobPurgeReservation,
)
from app.schemas import TrainingExecutionCreateRequest
from app.services import ServiceError
from app.training_execution_queue import (
    _finalize,
    claim_next_training_execution,
    renew_execution_lease,
)
from app.training_execution_services import (
    cancel_training_execution,
    enqueue_training_execution,
    retry_training_execution,
)
from app.training_job_maintenance import (
    _assert_no_active_execution_before_bytes,
    _register_candidates,
    archive_training_job,
)
from app.training_job_services import review_training_job

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped in CI")
    pytest.skip("PostgreSQL integration database is unavailable")


@pytest.fixture(scope="module")
def engine():
    value = create_database_engine(_database_url())
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    yield value
    value.dispose()


_RACE_WAIT_SECONDS = 20


@contextmanager
def _bounded_race_database(engine):
    """Apply finite PostgreSQL lock/query clocks to every race connection."""

    def on_checkout(dbapi_connection, _connection_record, _connection_proxy):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET lock_timeout = '3s'")
            cursor.execute("SET statement_timeout = '15s'")
        finally:
            cursor.close()

    event.listen(engine, "checkout", on_checkout)
    try:
        yield
    finally:
        event.remove(engine, "checkout", on_checkout)


class _ExecutionLockHooks:
    """Test-only after-lock hooks used to force race schedules."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.local = threading.local()
        self.first_lock: dict[str, str] = {}
        self.first_advisory: set[str] = set()
        self.callbacks: dict[tuple[str, str], Callable[[], None]] = {}
        self.advisory_callbacks: dict[str, Callable[[], None]] = {}
        self._guard = threading.Lock()

    def install(self) -> None:
        event.listen(self.engine, "after_cursor_execute", self._after_cursor_execute)

    def uninstall(self) -> None:
        event.remove(self.engine, "after_cursor_execute", self._after_cursor_execute)

    def participant(self, label: str) -> None:
        self.local.participant = label

    def clear_participant(self) -> None:
        self.local.participant = None

    @staticmethod
    def _lock_kind(statement: str) -> str | None:
        normalized = " ".join(statement.lower().split())
        if "for update" not in normalized:
            return None
        for name, kind in (
            ("training_jobs", "job"),
            ("training_executions", "execution"),
            ("training_execution_attempts", "attempt"),
        ):
            if f"from {name}" in normalized:
                return kind
        return None

    def after(self, label: str, kind: str, callback: Callable[[], None]) -> None:
        self.callbacks[(label, kind)] = callback

    def after_advisory(self, label: str, callback: Callable[[], None]) -> None:
        self.advisory_callbacks[label] = callback

    def _after_cursor_execute(
        self, _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = " ".join(statement.lower().split())
        label = getattr(self.local, "participant", None)
        if label is not None and "pg_advisory_xact_lock" in normalized:
            with self._guard:
                first_advisory = label not in self.first_advisory
                if first_advisory:
                    self.first_advisory.add(label)
                callback = self.advisory_callbacks.get(label) if first_advisory else None
            if callback is not None:
                callback()
        kind = self._lock_kind(statement)
        if kind is None or label is None:
            return
        with self._guard:
            if label not in self.first_lock:
                self.first_lock[label] = kind
                callback = self.callbacks.get((label, kind))
            else:
                callback = None
        if callback is not None:
            callback()


def _run_forced_race(
    engine,
    first_label: str,
    operations: dict[str, Callable[[], object]],
) -> tuple[dict[str, object], dict[str, BaseException], _ExecutionLockHooks]:
    """Run real production operations with one deterministic first lock."""

    hooks = _ExecutionLockHooks(engine)
    first_lock_seen = threading.Event()
    competing_job_lock_started = threading.Event()
    outcomes: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def target(label: str) -> None:
        hooks.participant(label)
        try:
            outcomes[label] = operations[label]()
        except BaseException as error:  # noqa: BLE001 - surfaced by assertions below
            errors[label] = error
        finally:
            hooks.clear_participant()

    def hold_first_job_lock() -> None:
        # The first transaction retains TrainingJob while every competing
        # participant has reached its own FOR UPDATE statement. This forces
        # real contention without sleeps or arbitrary timing assumptions.
        first_lock_seen.set()
        if not competing_job_lock_started.wait(_RACE_WAIT_SECONDS):
            raise AssertionError("timed out waiting for the competing job lock")

    hooks.after(first_label, "job", hold_first_job_lock)

    def before_cursor_execute(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        label = getattr(hooks.local, "participant", None)
        if label != first_label and hooks._lock_kind(statement) == "job":
            competing_job_lock_started.set()

    hooks.install()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    threads: list[threading.Thread] = []
    try:
        with _bounded_race_database(engine):
            first = threading.Thread(
                target=target, args=(first_label,), name=f"phase14-1-{first_label}", daemon=True
            )
            first.start()
            if not first_lock_seen.wait(_RACE_WAIT_SECONDS):
                pytest.fail(f"timed out waiting for {first_label} to lock TrainingJob")
            threads.append(first)
            for label in operations:
                if label == first_label:
                    continue
                thread = threading.Thread(
                    target=target, args=(label,), name=f"phase14-1-{label}", daemon=True
                )
                thread.start()
                threads.append(thread)
            for thread in threads:
                thread.join(_RACE_WAIT_SECONDS + 5)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        hooks.uninstall()
    assert all(not thread.is_alive() for thread in threads), "race worker remained alive"
    return outcomes, errors, hooks


def _run_pre_row_advisory_race(
    engine,
    first_label: str,
    operations: dict[str, Callable[[], object]],
) -> tuple[dict[str, object], dict[str, BaseException], _ExecutionLockHooks, bool]:
    """Force the second transaction to reach the advisory fence first.

    The first transaction pauses from the after-advisory hook, before its
    production function can issue TrainingJob FOR UPDATE. The competing
    transaction's before-advisory hook proves it reached the same blocking
    fence while its job-row hook remains unobserved.
    """

    hooks = _ExecutionLockHooks(engine)
    first_advisory_seen = threading.Event()
    competing_advisory_started = threading.Event()
    competing_job_lock_started = threading.Event()
    outcomes: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    callback_errors: list[BaseException] = []
    labels = tuple(operations)
    competing_label = next(label for label in labels if label != first_label)
    job_lock_started_before_release = False

    def target(label: str) -> None:
        hooks.participant(label)
        try:
            outcomes[label] = operations[label]()
        except BaseException as error:  # noqa: BLE001 - surfaced by assertions below
            errors[label] = error
        finally:
            hooks.clear_participant()

    def pause_after_first_advisory() -> None:
        nonlocal job_lock_started_before_release
        first_advisory_seen.set()
        if not competing_advisory_started.wait(_RACE_WAIT_SECONDS):
            callback_errors.append(AssertionError("competing advisory lock was not reached"))
        job_lock_started_before_release = competing_job_lock_started.is_set()

    hooks.after_advisory(first_label, pause_after_first_advisory)

    def before_cursor_execute(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        label = getattr(hooks.local, "participant", None)
        if label != competing_label:
            return
        normalized = " ".join(statement.lower().split())
        if "pg_advisory_xact_lock" in normalized:
            competing_advisory_started.set()
        if hooks._lock_kind(statement) == "job":
            competing_job_lock_started.set()

    hooks.install()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    threads: list[threading.Thread] = []
    try:
        with _bounded_race_database(engine):
            first = threading.Thread(
                target=target, args=(first_label,), name=f"phase14-2-{first_label}", daemon=True
            )
            first.start()
            if not first_advisory_seen.wait(_RACE_WAIT_SECONDS):
                pytest.fail("timed out waiting for the first advisory fence")
            threads.append(first)
            competing = threading.Thread(
                target=target,
                args=(competing_label,),
                name=f"phase14-2-{competing_label}",
                daemon=True,
            )
            competing.start()
            threads.append(competing)
            for thread in threads:
                thread.join(_RACE_WAIT_SECONDS + 5)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
        hooks.uninstall()
    assert all(not thread.is_alive() for thread in threads), "pre-row race worker remained alive"
    if callback_errors:
        raise callback_errors[0]
    return outcomes, errors, hooks, job_lock_started_before_release


def _assert_no_database_race_errors(errors: dict[str, BaseException]) -> None:
    unexpected = {
        label: error
        for label, error in errors.items()
        if type(error).__name__ in {"OperationalError", "TimeoutError"}
    }
    if unexpected:
        details = "; ".join(
            f"{label}: {type(error).__name__}: {error}" for label, error in unexpected.items()
        )
        pytest.fail(
            f"PostgreSQL race failed; deadlock/timeout is not a business outcome: {details}"
        )


def _assert_only_expected_conflicts(
    errors: dict[str, BaseException], *, allowed_labels: frozenset[str] = frozenset()
) -> None:
    _assert_no_database_race_errors(errors)
    unexpected = {
        label: error
        for label, error in errors.items()
        if label not in allowed_labels
        or not isinstance(error, ServiceError)
        or error.status_code != 409
    }
    if unexpected:
        details = "; ".join(
            f"{label}: {type(error).__name__}: {error}" for label, error in unexpected.items()
        )
        pytest.fail(f"Unexpected PostgreSQL race outcome: {details}")


def test_phase14_1_migration_cycle_and_schema_contract(engine, tmp_path: Path) -> None:
    config = Config("alembic.ini")
    command.downgrade(config, "0017_phase12_adapter_runtime_routing")
    command.upgrade(config, "head")

    # Prove the downgrade from a populated Phase 14.1 schema, not only from
    # empty tables.  The queued parent and registered attempt exercise the
    # lifecycle/check/index-compatible values and all restrictive FKs.
    factory, department_id, _issuer, _subject, execution = _approved_execution(
        engine, tmp_path / "migration-runtime"
    )
    with factory.begin() as session:
        execution_row = session.get(TrainingExecution, execution.id)
        assert execution_row is not None
        session.add(
            TrainingExecutionAttempt(
                id=execution_row.current_attempt_id or uuid4(),
                execution_id=execution_row.id,
                department_id=department_id,
                attempt_number=1,
                status="registered",
                version=1,
            )
        )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM training_executions")) == 1
        assert connection.scalar(text("SELECT count(*) FROM training_execution_attempts")) == 1
        connection.execute(
            text(
                "UPDATE training_execution_attempts SET status='succeeded', "
                "worker_id='00000000-0000-0000-0000-000000000091', "
                "claim_token='00000000-0000-0000-0000-000000000092', "
                "claimed_at=clock_timestamp(), finished_at=clock_timestamp(), "
                "runtime_kind='real', runtime_contract_version='phase14-training-runtime-v1', "
                "runtime_dependency_lock_sha256=:digest, "
                "runtime_environment_profile_id="
                "'deptslm-phase14-training-runtime-linux-x86_64-cuda126-v1', "
                "runtime_environment_fingerprint=:digest, "
                "runtime_hardware_profile_id='linux-x86_64-nvidia-cuda126-bf16-v1', "
                "runtime_hardware_fingerprint=:digest, output_stage_fingerprint=:digest, "
                "output_file_count=1, output_total_bytes=1, output_retained_at=clock_timestamp(), "
                "input_snapshot_fingerprint=:digest, runtime_fingerprint=:digest, "
                "result_classification='execution_succeeded', error_code=NULL"
            ),
            {"digest": "a" * 64},
        )
        connection.commit()

    command.downgrade(config, "0018_phase14_training_execution_control_plane")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0018_phase14_training_execution_control_plane"
        )
    legacy_inspector = inspect(engine)
    legacy_attempt_columns = {
        item["name"] for item in legacy_inspector.get_columns("training_execution_attempts")
    }
    assert "runtime_kind" not in legacy_attempt_columns
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0019_phase14_training_runtime"
        )

    command.downgrade(config, "0017_phase12_adapter_runtime_routing")
    inspector = inspect(engine)
    assert not inspector.has_table("training_execution_attempts")
    assert not inspector.has_table("training_executions")

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0019_phase14_training_runtime"
        )
    inspector = inspect(engine)
    execution_columns = {item["name"] for item in inspector.get_columns("training_executions")}
    attempt_columns = {
        item["name"] for item in inspector.get_columns("training_execution_attempts")
    }
    assert {
        "training_job_id",
        "training_job_publication_manifest",
        "dataset_build_id",
        "dataset_manifest_sha256",
        "execution_code_revision",
        "authority_fingerprint",
        "status",
        "current_attempt_id",
        "claim_token",
    }.issubset(execution_columns)
    assert {
        "execution_id",
        "attempt_number",
        "worker_id",
        "claim_token",
        "input_snapshot_fingerprint",
        "runtime_fingerprint",
        "result_classification",
        "runtime_kind",
        "runtime_contract_version",
        "runtime_dependency_lock_sha256",
        "runtime_environment_profile_id",
        "runtime_environment_fingerprint",
        "runtime_hardware_profile_id",
        "runtime_hardware_fingerprint",
        "output_stage_fingerprint",
        "output_file_count",
        "output_total_bytes",
        "output_retained_at",
        "output_purged_at",
    }.issubset(attempt_columns)
    assert any(
        index["name"] == "uq_training_execution_active_job_profile" and index["unique"]
        for index in inspector.get_indexes("training_executions")
    )
    checks = {item["name"] for item in inspector.get_check_constraints("training_executions")}
    assert {
        "ck_training_execution_status",
        "ck_training_execution_source_lifecycle",
        "ck_training_execution_lifecycle",
        "ck_training_execution_model_contract",
        "ck_training_execution_error_code",
    }.issubset(checks)
    attempt_checks = {
        item["name"] for item in inspector.get_check_constraints("training_execution_attempts")
    }
    assert {
        "ck_training_execution_attempt_success_contract",
        "ck_training_execution_attempt_runtime_kind",
        "ck_training_execution_attempt_runtime_contract",
        "ck_training_execution_attempt_runtime_hashes",
        "ck_training_execution_attempt_output_bounds",
        "ck_training_execution_attempt_real_success_contract",
        "ck_training_execution_attempt_output_retention",
    }.issubset(attempt_checks)


def _approved_execution(
    engine, tmp_path: Path
) -> tuple[sessionmaker, UUID, str, str, TrainingExecution]:
    factory = sessionmaker(engine)
    department_id, issuer, subject, job_id = _succeeded_training_job(factory, tmp_path / "runtime")
    principal = AuthenticatedPrincipal(subject, issuer)
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        review_training_job(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            job.id,
            action="approve",
            expected_version=job.version,
        )
    with factory.begin() as session:
        job = session.get(TrainingJob, job_id)
        assert job is not None
        execution_code_revision = _unique_code_revision()
        execution = enqueue_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            TrainingExecutionCreateRequest(
                training_job_id=job.id,
                expected_training_job_version=job.version,
            ),
            execution_code_revision=execution_code_revision,
        )
        execution_id = execution.id
    with factory() as session:
        row = session.get(TrainingExecution, execution_id)
        assert row is not None
        return factory, department_id, issuer, subject, row


def test_phase14_1_captures_immutable_authority_and_enforces_active_uniqueness(
    engine, tmp_path: Path
) -> None:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    assert execution.status == "queued"
    assert execution.training_job_publication_manifest
    assert execution.authority_fingerprint and len(execution.authority_fingerprint) == 64
    principal = AuthenticatedPrincipal(subject, issuer)
    with factory.begin() as session:
        job = session.get(TrainingJob, execution.training_job_id)
        assert job is not None
        with pytest.raises(ServiceError, match="Training execution conflict"):
            enqueue_training_execution(
                session,
                principal,
                DepartmentRequestScope(DepartmentScope(department_id)),
                TrainingExecutionCreateRequest(
                    training_job_id=job.id,
                    expected_training_job_version=job.version,
                ),
                execution_code_revision=execution.execution_code_revision,
            )


def test_phase14_1_cancel_then_retry_creates_a_new_attempt_surface(engine, tmp_path: Path) -> None:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    principal = AuthenticatedPrincipal(subject, issuer)
    execution_id = execution.id
    with factory.begin() as session:
        cancelled = cancel_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            execution_id,
            expected_version=execution.version,
        )
        assert cancelled.status == "cancelled"
        cancelled_version = cancelled.version
    with factory.begin() as session:
        retried = retry_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            execution_id,
            expected_version=cancelled_version,
        )
        assert retried.status == "queued"
        assert retried.current_attempt_number == 2
    with factory() as session:
        attempts = session.scalars(
            select(TrainingExecutionAttempt).where(
                TrainingExecutionAttempt.execution_id == execution_id
            )
        ).all()
        assert attempts == []


def test_phase14_1_heartbeat_renewal_vs_cancel_has_one_job_first_order(
    engine, tmp_path: Path
) -> None:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    claim = claim_next_training_execution(factory, uuid4(), 120, execution.execution_code_revision)
    assert claim is not None
    principal = AuthenticatedPrincipal(subject, issuer)
    with factory() as session:
        current = session.get(TrainingExecution, execution.id)
        assert current is not None
        expected_version = current.version

    def renew() -> object:
        return renew_execution_lease(factory, claim, 120)

    def cancel() -> object:
        with factory.begin() as session:
            return cancel_training_execution(
                session,
                principal,
                DepartmentRequestScope(DepartmentScope(department_id)),
                execution.id,
                expected_version=expected_version,
            ).status

    outcomes, errors, hooks = _run_forced_race(engine, "renew", {"renew": renew, "cancel": cancel})
    _assert_only_expected_conflicts(errors, allowed_labels=frozenset({"cancel"}))
    assert hooks.first_lock == {"renew": "job", "cancel": "job"}
    assert outcomes.get("renew") is True
    if "cancel" in errors:
        assert isinstance(errors["cancel"], ServiceError)
        assert errors["cancel"].status_code == 409
    else:
        assert outcomes.get("cancel") in {"cancel_requested", "cancelled"}
    with factory() as session:
        row = session.get(TrainingExecution, execution.id)
        assert row is not None and row.status in {"running", "cancel_requested", "cancelled"}


def _run_success_cancel_race(
    engine, tmp_path: Path, *, first_label: str
) -> tuple[
    dict[str, object],
    dict[str, BaseException],
    _ExecutionLockHooks,
    sessionmaker,
    TrainingExecution,
]:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    claim = claim_next_training_execution(factory, uuid4(), 120, execution.execution_code_revision)
    assert claim is not None
    principal = AuthenticatedPrincipal(subject, issuer)
    with factory() as session:
        current = session.get(TrainingExecution, execution.id)
        assert current is not None
        expected_version = current.version

    def success() -> object:
        return _finalize(
            factory,
            claim,
            status="succeeded",
            error_code=None,
            classification="execution_succeeded",
            runtime_fp="a" * 64,
            input_fp="b" * 64,
        )

    def cancel() -> object:
        with factory.begin() as session:
            return cancel_training_execution(
                session,
                principal,
                DepartmentRequestScope(DepartmentScope(department_id)),
                execution.id,
                expected_version=expected_version,
            ).status

    operations = {"success": success, "cancel": cancel}
    outcomes, errors, hooks = _run_forced_race(engine, first_label, operations)
    return outcomes, errors, hooks, factory, execution


def _run_pre_row_success_cancel_race(
    engine, tmp_path: Path, *, first_label: str
) -> tuple[
    dict[str, object],
    dict[str, BaseException],
    _ExecutionLockHooks,
    bool,
    sessionmaker,
    TrainingExecution,
]:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    claim = claim_next_training_execution(factory, uuid4(), 120, execution.execution_code_revision)
    assert claim is not None
    principal = AuthenticatedPrincipal(subject, issuer)
    with factory() as session:
        current = session.get(TrainingExecution, execution.id)
        assert current is not None
        expected_version = current.version

    def success() -> object:
        return _finalize(
            factory,
            claim,
            status="succeeded",
            error_code=None,
            classification="execution_succeeded",
            runtime_fp="a" * 64,
            input_fp="b" * 64,
        )

    def cancel() -> object:
        with factory.begin() as session:
            return cancel_training_execution(
                session,
                principal,
                DepartmentRequestScope(DepartmentScope(department_id)),
                execution.id,
                expected_version=expected_version,
            ).status

    operations = {"success": success, "cancel": cancel}
    outcomes, errors, hooks, job_lock_started = _run_pre_row_advisory_race(
        engine, first_label, operations
    )
    return outcomes, errors, hooks, job_lock_started, factory, execution


@pytest.mark.parametrize("first_label", ["success", "cancel"])
def test_phase14_2_pre_row_advisory_race_has_canonical_lock_order(
    engine, tmp_path: Path, first_label: str
) -> None:
    outcomes, errors, hooks, job_lock_started, factory, execution = (
        _run_pre_row_success_cancel_race(engine, tmp_path, first_label=first_label)
    )
    _assert_only_expected_conflicts(
        errors, allowed_labels=frozenset({"cancel"}) if first_label == "success" else frozenset()
    )
    assert hooks.first_advisory == {"success", "cancel"}
    assert not job_lock_started, (
        "competing transaction acquired TrainingJob before advisory release"
    )
    if first_label == "success":
        assert outcomes.get("success") is True
        with factory() as session:
            row = session.get(TrainingExecution, execution.id)
            assert row is not None and row.status == "succeeded"
    else:
        assert outcomes.get("cancel") == "cancel_requested"
        assert outcomes.get("success") is True
        with factory() as session:
            row = session.get(TrainingExecution, execution.id)
            assert row is not None and row.status == "cancelled"


def test_phase14_1_fake_success_vs_cancel_success_first_is_terminal(engine, tmp_path: Path) -> None:
    outcomes, errors, hooks, factory, execution = _run_success_cancel_race(
        engine, tmp_path, first_label="success"
    )
    _assert_only_expected_conflicts(errors, allowed_labels=frozenset({"cancel"}))
    assert hooks.first_lock == {"success": "job", "cancel": "job"}
    assert outcomes.get("success") is True
    with factory() as session:
        row = session.get(TrainingExecution, execution.id)
        assert row is not None and row.status == "succeeded"


def test_phase14_1_fake_success_vs_cancel_cancel_first_cannot_succeed(
    engine, tmp_path: Path
) -> None:
    outcomes, errors, hooks, factory, execution = _run_success_cancel_race(
        engine, tmp_path, first_label="cancel"
    )
    _assert_only_expected_conflicts(errors)
    assert hooks.first_lock == {"cancel": "job", "success": "job"}
    assert outcomes.get("cancel") == "cancel_requested"
    assert outcomes.get("success") is True
    with factory() as session:
        row = session.get(TrainingExecution, execution.id)
        assert row is not None and row.status == "cancelled"


def test_phase14_1_expired_reclaim_fences_stale_owner_finalize(engine, tmp_path: Path) -> None:
    factory, _department_id, _issuer, _subject, execution = _approved_execution(engine, tmp_path)
    worker_a = uuid4()
    claim_a = claim_next_training_execution(
        factory, worker_a, 120, execution.execution_code_revision
    )
    assert claim_a is not None
    with factory.begin() as session:
        session.execute(
            text(
                "UPDATE training_executions "
                "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": str(execution.id)},
        )
    claim_b = claim_next_training_execution(
        factory, uuid4(), 120, execution.execution_code_revision
    )
    assert claim_b is not None and claim_b.attempt_id != claim_a.attempt_id
    assert (
        _finalize(
            factory,
            claim_a,
            status="succeeded",
            error_code=None,
            classification="execution_succeeded",
            runtime_fp="a" * 64,
            input_fp="b" * 64,
        )
        is False
    )
    assert (
        _finalize(
            factory,
            claim_b,
            status="succeeded",
            error_code=None,
            classification="execution_succeeded",
            runtime_fp="c" * 64,
            input_fp="d" * 64,
        )
        is True
    )
    with factory() as session:
        row = session.get(TrainingExecution, execution.id)
        attempts = session.scalars(
            select(TrainingExecutionAttempt).where(
                TrainingExecutionAttempt.execution_id == execution.id
            )
        ).all()
        assert row is not None and row.status == "succeeded"
        assert {attempt.status for attempt in attempts} == {"reclaimed", "succeeded"}


def test_phase14_2_real_success_retains_phase11_fence(engine, tmp_path: Path) -> None:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    claim = claim_next_training_execution(factory, uuid4(), 120, execution.execution_code_revision)
    assert claim is not None
    runtime_details = {
        "runtime_kind": "real",
        "runtime_contract_version": "phase14-training-runtime-v1",
        "dependency_lock_sha256": "a" * 64,
        "environment_profile_id": "deptslm-phase14-training-runtime-linux-x86_64-cuda126-v1",
        "environment_fingerprint": "b" * 64,
        "hardware_profile_id": "linux-x86_64-nvidia-one-gpu-bf16",
        "hardware_fingerprint": "c" * 64,
        "output_stage_fingerprint": "d" * 64,
        "output_file_count": 1,
        "output_total_bytes": 1,
    }
    assert _finalize(
        factory,
        claim,
        status="succeeded",
        error_code=None,
        classification="execution_succeeded",
        runtime_fp="e" * 64,
        input_fp="f" * 64,
        runtime_details=runtime_details,
    )
    with factory() as session:
        attempt = session.scalar(
            select(TrainingExecutionAttempt).where(
                TrainingExecutionAttempt.id == claim.attempt_id,
                TrainingExecutionAttempt.department_id == department_id,
            )
        )
        assert attempt is not None
        assert attempt.runtime_kind == "real"
        assert attempt.output_retained_at is not None
        assert attempt.output_purged_at is None
    with pytest.raises(ServiceError, match="active execution"):
        archive_training_job(
            factory,
            department_id=department_id,
            training_job_id=execution.training_job_id,
            actor_issuer=issuer,
            actor_subject=subject,
            apply=True,
        )


def test_phase14_1_active_execution_blocks_legacy_pre_byte_delete_fence(
    engine, tmp_path: Path
) -> None:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    runtime_root = tmp_path / "runtime"
    deleting_before = set(runtime_root.rglob(".deleting"))
    with factory.begin() as session:
        job = session.get(TrainingJob, execution.training_job_id)
        assert job is not None and job.archived_at is None
        # Construct the narrow impossible legacy state explicitly: an active
        # queued execution survived a direct archive and an already-authorized
        # purge reservation. The production pre-byte fence must still win.
        job.review_status = "archived"
        job.archived_at = datetime.now(UTC)
        job.version += 1
        operation = TrainingJobArtifactOperation(
            id=uuid4(),
            department_id=department_id,
            requested_by_user_id=job.requested_by_user_id,
            limit_value=1,
            retention_days=30,
            operation_type="purge",
            status="registered",
            version=1,
        )
        session.add(operation)
        session.flush()
        session.add(
            TrainingJobPurgeReservation(
                id=uuid4(),
                operation_id=operation.id,
                department_id=department_id,
                training_job_id=job.id,
                expected_job_version=job.version,
                expected_review_status="archived",
                retention_anchor_at=job.archived_at,
                retention_days=30,
                authoritative_publication_attempt_id=job.publication_attempt_id,
                authoritative_manifest=dict(job.publication_manifest),
                tombstone_operation_id=operation.id,
                status="deletion_authorized",
                deletion_authorized_at=datetime.now(UTC),
                version=1,
            )
        )
        operation_id = operation.id
    principal = AuthenticatedPrincipal(subject, issuer)
    with pytest.raises(ServiceError, match="active execution"):
        _assert_no_active_execution_before_bytes(
            factory,
            department_id=department_id,
            training_job_id=execution.training_job_id,
            actor_issuer=principal.issuer,
            actor_subject=principal.subject,
            operation_id=operation_id,
        )
    assert set(runtime_root.rglob(".deleting")) == deleting_before
    with factory() as session:
        assert (
            session.scalar(
                select(PersistentAuditEvent.id).where(
                    PersistentAuditEvent.department_id == department_id,
                    PersistentAuditEvent.action == "training.job.purge",
                )
            )
            is None
        )


def _run_enqueue_archive_race(
    engine, tmp_path: Path, *, first_label: str
) -> tuple[dict[str, object], dict[str, BaseException], _ExecutionLockHooks, sessionmaker, UUID]:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    principal = AuthenticatedPrincipal(subject, issuer)
    training_job_id = execution.training_job_id
    with factory() as session:
        job = session.get(TrainingJob, training_job_id)
        assert job is not None
        expected_job_version = job.version
        code_revision = job.code_revision
    with factory.begin() as session:
        current = session.get(TrainingExecution, execution.id)
        assert current is not None
        cancel_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            execution.id,
            expected_version=current.version,
        )
    with factory() as session:
        cancelled = session.get(TrainingExecution, execution.id)
        assert cancelled is not None and cancelled.status == "cancelled"

    def enqueue() -> object:
        with factory.begin() as session:
            return enqueue_training_execution(
                session,
                principal,
                DepartmentRequestScope(DepartmentScope(department_id)),
                TrainingExecutionCreateRequest(
                    training_job_id=training_job_id,
                    expected_training_job_version=expected_job_version,
                ),
                execution_code_revision=code_revision,
            ).status

    def archive() -> object:
        return archive_training_job(
            factory,
            department_id=department_id,
            training_job_id=training_job_id,
            actor_issuer=issuer,
            actor_subject=subject,
            apply=True,
        )

    outcomes, errors, hooks = _run_forced_race(
        engine, first_label, {"enqueue": enqueue, "archive": archive}
    )
    return outcomes, errors, hooks, factory, training_job_id


def test_phase14_1_enqueue_vs_archive_execution_first_fences_archive(
    engine, tmp_path: Path
) -> None:
    outcomes, errors, hooks, factory, execution_id = _run_enqueue_archive_race(
        engine, tmp_path, first_label="enqueue"
    )
    _assert_only_expected_conflicts(errors, allowed_labels=frozenset({"archive"}))
    assert hooks.first_lock == {"enqueue": "job", "archive": "job"}
    assert outcomes.get("enqueue") == "queued"
    assert outcomes.get("archive") is None
    with factory() as session:
        assert (
            session.scalar(
                select(TrainingExecution.id).where(
                    TrainingExecution.training_job_id == execution_id,
                    TrainingExecution.status == "queued",
                )
            )
            is not None
        )


def test_phase14_1_enqueue_vs_archive_archive_first_rejects_enqueue(engine, tmp_path: Path) -> None:
    outcomes, errors, hooks, factory, execution_id = _run_enqueue_archive_race(
        engine, tmp_path, first_label="archive"
    )
    _assert_only_expected_conflicts(errors, allowed_labels=frozenset({"enqueue"}))
    assert hooks.first_lock == {"archive": "job", "enqueue": "job"}
    assert outcomes.get("archive") is True
    with factory() as session:
        assert (
            session.scalar(
                select(TrainingExecution.id).where(
                    TrainingExecution.training_job_id == execution_id,
                    TrainingExecution.status.in_(("queued", "running", "cancel_requested")),
                )
            )
            is None
        )
        assert (
            session.scalar(select(TrainingJob.review_status).where(TrainingJob.id == execution_id))
            == "archived"
        )


def _run_enqueue_purge_registration_race(
    engine, tmp_path: Path, *, first_label: str
) -> tuple[dict[str, object], dict[str, BaseException], _ExecutionLockHooks, sessionmaker, UUID]:
    factory, department_id, issuer, subject, execution = _approved_execution(engine, tmp_path)
    principal = AuthenticatedPrincipal(subject, issuer)
    training_job_id = execution.training_job_id
    with factory() as session:
        job = session.get(TrainingJob, training_job_id)
        assert job is not None
        expected_job_version = job.version
        code_revision = job.code_revision
    with factory.begin() as session:
        current = session.get(TrainingExecution, execution.id)
        assert current is not None
        cancel_training_execution(
            session,
            principal,
            DepartmentRequestScope(DepartmentScope(department_id)),
            execution.id,
            expected_version=current.version,
        )

    def enqueue() -> object:
        with factory.begin() as session:
            return enqueue_training_execution(
                session,
                principal,
                DepartmentRequestScope(DepartmentScope(department_id)),
                TrainingExecutionCreateRequest(
                    training_job_id=training_job_id,
                    expected_training_job_version=expected_job_version,
                ),
                execution_code_revision=code_revision,
            ).status

    def purge() -> object:
        # Purge registration is only eligible after the exact job is archived;
        # archive and registration remain real Phase 11 maintenance paths.
        archive_training_job(
            factory,
            department_id=department_id,
            training_job_id=training_job_id,
            actor_issuer=issuer,
            actor_subject=subject,
            apply=True,
        )
        # Make the real archived job eligible for the reviewed retention
        # window.  The production registration path must still observe this
        # server-authoritative lifecycle state while the enqueue race is
        # fenced by the job lock and version.
        with factory.begin() as session:
            job = session.get(TrainingJob, training_job_id)
            assert job is not None and job.review_status == "archived"
            job.archived_at = datetime.now(UTC) - timedelta(days=31)
            job.version += 1
        return _register_candidates(
            factory,
            department_id=department_id,
            actor_issuer=issuer,
            actor_subject=subject,
            operation_type="purge",
            retention_days=30,
            limit=1,
            apply=True,
        )

    outcomes, errors, hooks = _run_forced_race(
        engine, first_label, {"enqueue": enqueue, "purge": purge}
    )
    return outcomes, errors, hooks, factory, training_job_id


def test_phase14_1_enqueue_vs_purge_execution_first_has_no_reservation(
    engine, tmp_path: Path
) -> None:
    outcomes, errors, hooks, factory, execution_id = _run_enqueue_purge_registration_race(
        engine, tmp_path, first_label="enqueue"
    )
    _assert_only_expected_conflicts(errors, allowed_labels=frozenset({"purge"}))
    assert hooks.first_lock == {"enqueue": "job", "purge": "job"}
    assert outcomes.get("enqueue") == "queued"
    with factory() as session:
        assert (
            session.scalar(
                select(TrainingJobPurgeReservation.id).where(
                    TrainingJobPurgeReservation.training_job_id == execution_id
                )
            )
            is None
        )


def test_phase14_1_enqueue_vs_purge_purge_first_reservation_fences_enqueue(
    engine, tmp_path: Path
) -> None:
    outcomes, errors, hooks, factory, execution_id = _run_enqueue_purge_registration_race(
        engine, tmp_path, first_label="purge"
    )
    _assert_only_expected_conflicts(errors, allowed_labels=frozenset({"enqueue"}))
    assert hooks.first_lock == {"purge": "job", "enqueue": "job"}
    assert isinstance(outcomes.get("purge"), tuple)
    with factory() as session:
        assert (
            session.scalar(
                select(TrainingJobPurgeReservation.id).where(
                    TrainingJobPurgeReservation.training_job_id == execution_id
                )
            )
            is not None
        )
