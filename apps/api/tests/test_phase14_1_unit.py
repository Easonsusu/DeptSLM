"""Focused unit guardrails for the Phase 14.1 control plane."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from phase14_1_fake_runtime import FakeTrainingRuntime
from pydantic import ValidationError

from app.routes import router
from app.schemas import TrainingExecutionCreateRequest, TrainingExecutionMutationRequest
from app.training_execution_domain import (
    EXECUTION_CONTRACT_VERSION,
    authority_fingerprint,
    validate_runtime_result,
)
from app.training_execution_runtime import (
    TrainingRuntimeHandles,
    TrainingRuntimeRequest,
    validate_runtime_request,
)
from app.training_execution_storage import (
    TrainingExecutionArtifactStore,
    TrainingExecutionStorageError,
)


def test_execution_request_bodies_are_closed() -> None:
    with pytest.raises(ValidationError):
        TrainingExecutionCreateRequest(
            training_job_id=uuid4(), expected_training_job_version=1, profile_id="not-accepted"
        )
    with pytest.raises(ValidationError):
        TrainingExecutionMutationRequest(expected_version=1, path="/tmp/unsafe")


def test_authority_fingerprint_is_canonical_and_content_free() -> None:
    first = {"job": {"department_id": str(uuid4()), "version": 1}, "execution_id": str(uuid4())}
    second = {"execution_id": first["execution_id"], "job": first["job"]}
    assert authority_fingerprint(first) == authority_fingerprint(second)
    assert EXECUTION_CONTRACT_VERSION == "phase14-training-execution-v1"


def test_runtime_request_is_closed_and_descriptor_free() -> None:
    department_id = uuid4()
    execution_id = uuid4()
    attempt_id = uuid4()
    training_job_id = uuid4()
    request = TrainingRuntimeRequest(
        "phase14-training-execution-v1",
        department_id,
        execution_id,
        attempt_id,
        training_job_id,
        uuid4(),
        "a" * 64,
        "b" * 64,
        "phase11-qwen3-0.6b-lora-v1",
        "Qwen/Qwen3-0.6B",
        "c1899de289a04d12100db370d81485cdf75e47ca",
        attempt_id,
        "a" * 40,
        "b" * 40,
    )
    validate_runtime_request(request)
    assert set(request.__dataclass_fields__).isdisjoint(
        {
            "input_descriptor",
            "scratch_descriptor",
            "logs_descriptor",
            "output_stage_descriptor",
            "path",
            "argv",
            "environment",
            "configuration",
        }
    )
    with pytest.raises(Exception):
        validate_runtime_result(
            department_id=department_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
            training_job_id=training_job_id,
            authority_fingerprint_value=request.authority_fingerprint,
            input_snapshot_fingerprint=request.input_snapshot_fingerprint,
            result={
                "department_id": str(department_id),
                "execution_id": str(request.execution_id),
                "attempt_id": str(request.attempt_id),
                "training_job_id": str(training_job_id),
                "authority_fingerprint": request.authority_fingerprint,
                "input_snapshot_fingerprint": request.input_snapshot_fingerprint,
                "runtime_fingerprint": "c" * 64,
                "classification": "execution_succeeded",
                "error_code": None,
                "answer": "must not be accepted",
            },
        )


def test_execution_storage_rejects_relative_root(tmp_path: Path) -> None:
    with pytest.raises(TrainingExecutionStorageError):
        TrainingExecutionArtifactStore(Path("relative-runtime"))
    store = TrainingExecutionArtifactStore(tmp_path)
    stage = store.create_attempt(uuid4(), uuid4(), uuid4())
    assert stage.input_fd >= 0
    stage.close()
    store.close()


def test_execution_routes_are_department_scoped_and_closed() -> None:
    paths = {route.path for route in router.routes}
    assert "/departments/{department_id}/training/executions" in paths
    assert "/departments/{department_id}/training/executions/{execution_id}/cancel" in paths
    assert "/departments/{department_id}/training/executions/{execution_id}/retry" in paths
    assert all("/training/execute" not in path for path in paths)


def test_claim_worker_source_locks_job_before_execution_and_attempt() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "app" / "training_execution_queue.py"
    ).read_text(encoding="utf-8")
    assert source.index("job_query = select(TrainingJob)") < source.index(
        "execution_query = select(TrainingExecution)"
    )
    assert source.index("execution_query = select(TrainingExecution)") < source.index(
        "attempt_query = select(TrainingExecutionAttempt)"
    )


def test_fake_runtime_is_deterministic_and_content_free() -> None:
    department_id, execution_id, attempt_id, job_id = (uuid4() for _ in range(4))
    request = TrainingRuntimeRequest(
        EXECUTION_CONTRACT_VERSION,
        department_id,
        execution_id,
        attempt_id,
        job_id,
        uuid4(),
        "a" * 64,
        "b" * 64,
        "phase11-qwen3-0.6b-lora-v1",
        "Qwen/Qwen3-0.6B",
        "c1899de289a04d12100db370d81485cdf75e47ca",
        attempt_id,
        "a" * 40,
        "b" * 40,
    )
    heartbeat_calls: list[bool] = []
    result = FakeTrainingRuntime().run(
        request,
        handles=TrainingRuntimeHandles(3, 4, 5, 6),
        should_stop=lambda: False,
        heartbeat=lambda: heartbeat_calls.append(True),
    )
    assert heartbeat_calls == [True]
    assert result.classification == "execution_succeeded"
    assert result.error_code is None
    assert set(result.as_closed_mapping()) == {
        "department_id",
        "execution_id",
        "attempt_id",
        "training_job_id",
        "authority_fingerprint",
        "input_snapshot_fingerprint",
        "runtime_fingerprint",
        "classification",
        "error_code",
    }
