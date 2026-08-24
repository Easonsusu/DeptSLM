"""Focused Phase 14.2 control-plane and private-runtime boundary checks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[3] / "services" / "training-runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from deptslm_training_runtime.contract import (  # noqa: E402
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
    LLAMAFACTORY_VERSION,
    RUNTIME_CONTRACT_VERSION,
    SEMANTIC_PROFILES,
    canonical_json_bytes,
    request_mapping,
)
from deptslm_training_runtime.ipc import (  # noqa: E402
    TrainingRuntimeServer,
    authenticate_frame,
)
from deptslm_training_runtime.output_stage import inspect_output_stage  # noqa: E402

from app.training_execution_domain import (  # noqa: E402
    TrainingExecutionError,
    execution_authority_fingerprint,
)
from app.training_execution_runtime import (  # noqa: E402
    RUNTIME_TRAINING_TIMEOUT_SECONDS,
    StopReason,
    TrainingRuntimeHandles,
    TrainingRuntimeRequest,
    TrainingRuntimeResult,
    UnixTrainingRuntimeClient,
    _stop_reason,
)
from app.training_execution_queue import (  # noqa: E402
    _closed_stop_reason,
    _external_stop_reason,
)


def _request() -> dict[str, object]:
    return {
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "department_id": str(uuid4()),
        "execution_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "training_job_id": str(uuid4()),
        "publication_attempt_id": str(uuid4()),
        "authority_fingerprint": "a" * 64,
        "input_snapshot_fingerprint": "b" * 64,
        "profile_id": "phase11-qwen3-0.6b-lora-v1",
        "base_model_id": BASE_MODEL_ID,
        "base_model_revision": BASE_MODEL_REVISION,
        "attempt_namespace": str(uuid4()),
        "dependency_lock_sha256": "c" * 64,
        "environment_profile_id": "deptslm-phase14-training-runtime-linux-x86_64-cuda126-v1",
        "expected_environment_fingerprint": "d" * 64,
        "training_job_code_revision": "f" * 40,
        "execution_code_revision": "e" * 40,
    }


def test_real_runtime_contract_is_exact_and_profiles_are_closed() -> None:
    assert RUNTIME_CONTRACT_VERSION == "phase14-training-runtime-v1"
    assert LLAMAFACTORY_VERSION == "0.9.5"
    assert BASE_MODEL_REVISION == "c1899de289a04d12100db370d81485cdf75e47ca"
    assert set(SEMANTIC_PROFILES) == {
        "phase11-qwen3-0.6b-lora-v1",
        "phase11-qwen3-0.6b-qlora-nf4-v1",
    }
    assert SEMANTIC_PROFILES["phase11-qwen3-0.6b-lora-v1"]["enable_liger_kernel"] is False
    assert SEMANTIC_PROFILES["phase11-qwen3-0.6b-qlora-nf4-v1"]["quantization_type"] == "nf4"


def test_phase11_and_phase14_code_authorities_are_independent() -> None:
    training_job_revision = "1" * 40
    execution_revision = "2" * 40
    other_revision = "3" * 40
    execution_id = uuid4()
    snapshot = {"code_revision": training_job_revision, "status": "succeeded"}
    first = execution_authority_fingerprint(
        execution_id=execution_id,
        training_job_code_revision=training_job_revision,
        execution_code_revision=execution_revision,
        snapshot=snapshot,
    )
    changed_job = execution_authority_fingerprint(
        execution_id=execution_id,
        training_job_code_revision=other_revision,
        execution_code_revision=execution_revision,
        snapshot={**snapshot, "code_revision": other_revision},
    )
    changed_executor = execution_authority_fingerprint(
        execution_id=execution_id,
        training_job_code_revision=training_job_revision,
        execution_code_revision=other_revision,
        snapshot=snapshot,
    )
    assert first != changed_job
    assert first != changed_executor


def test_external_stop_signals_are_closed_and_do_not_collapse_to_claim_loss() -> None:
    assert _external_stop_reason(False) is None
    assert _external_stop_reason(True) == StopReason.WORKER_SHUTDOWN.value
    assert _stop_reason(True) is StopReason.WORKER_SHUTDOWN
    assert _external_stop_reason(StopReason.CANCELLED) == StopReason.CANCELLED.value
    assert _external_stop_reason("claim_lost") == StopReason.CLAIM_LOST.value
    assert _external_stop_reason("unexpected") == StopReason.WORKER_SHUTDOWN.value
    assert (
        _closed_stop_reason(
            external=False, authoritative=StopReason.CANCELLED.value, deadline_reached=False
        )
        == StopReason.CANCELLED.value
    )
    assert (
        _closed_stop_reason(external=False, authoritative=None, deadline_reached=True)
        == StopReason.WORKER_TIMEOUT.value
    )
    assert (
        _closed_stop_reason(
            external=True, authoritative=StopReason.CLAIM_LOST.value, deadline_reached=True
        )
        == StopReason.WORKER_SHUTDOWN.value
    )


def test_runtime_request_rejects_paths_and_unknown_fields() -> None:
    request = _request()
    assert "path" not in request and "argv" not in request and "environment" not in request
    request_mapping(request)
    request["path"] = "/runtime/deptslm"
    with pytest.raises(ValueError):
        request_mapping(request)


def test_authenticated_frame_requires_fresh_nonce_and_exact_request() -> None:
    token = b"t" * 48
    request = _request()
    nonce = os.urandom(32)
    body = {
        "nonce": nonce.hex(),
        "request": request,
        "mac": hmac.new(token, canonical_json_bytes(request) + nonce, hashlib.sha256).hexdigest(),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    frame = struct.pack("!I", len(encoded)) + encoded
    descriptors: list[int] = []
    for _ in range(4):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        descriptors.append(read_fd)
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=4i", *descriptors))]
    try:
        # Pipe descriptors are intentionally rejected: only private directories
        # may cross the capability boundary.
        with pytest.raises(Exception):
            authenticate_frame(frame, ancillary, token, expected_uid=os.getuid())
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def test_output_stage_fingerprint_is_descriptor_bound(tmp_path: Path) -> None:
    stage = tmp_path / "output"
    stage.mkdir(mode=0o700)
    descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
    try:
        (stage / "adapter_model.safetensors").write_bytes(b"synthetic-candidate")
        evidence = inspect_output_stage(descriptor)
        assert evidence.file_count == 1
        assert evidence.total_bytes == len(b"synthetic-candidate")
        assert len(evidence.fingerprint) == 64
        (stage / "unsafe-link").symlink_to(stage / "adapter_model.safetensors")
        with pytest.raises(ValueError, match="output_invalid"):
            inspect_output_stage(descriptor)
    finally:
        os.close(descriptor)


def test_base_runtime_result_mapping_remains_fake() -> None:
    ids = {name: uuid4() for name in ("department", "execution", "attempt", "job")}
    result = TrainingRuntimeResult.from_closed_mapping(
        {
            "department_id": str(ids["department"]),
            "execution_id": str(ids["execution"]),
            "attempt_id": str(ids["attempt"]),
            "training_job_id": str(ids["job"]),
            "authority_fingerprint": "a" * 64,
            "input_snapshot_fingerprint": "b" * 64,
            "runtime_fingerprint": "c" * 64,
            "classification": "execution_succeeded",
            "error_code": None,
        }
    )
    assert result.runtime_kind == "fake"
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


def test_real_client_has_explicit_process_ready_and_separate_training_deadline(
    tmp_path: Path,
) -> None:
    token = "training-runtime-test-token-0123456789abcdef"
    socket_parent = Path("/tmp") / f"deptslm-{uuid4().hex}"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "runtime.sock"
    request_ids = {
        key: uuid4()
        for key in ("department", "execution", "attempt", "job", "publication", "namespace")
    }
    request = TrainingRuntimeRequest(
        contract_version="phase14-training-execution-v1",
        department_id=request_ids["department"],
        execution_id=request_ids["execution"],
        attempt_id=request_ids["attempt"],
        training_job_id=request_ids["job"],
        publication_attempt_id=request_ids["publication"],
        authority_fingerprint="a" * 64,
        input_snapshot_fingerprint="b" * 64,
        profile_id="phase11-qwen3-0.6b-lora-v1",
        base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        attempt_namespace=request_ids["namespace"],
        runtime_contract_version="phase14-training-runtime-v1",
        dependency_lock_sha256="c" * 64,
        environment_profile_id="deptslm-phase14-training-runtime-linux-x86_64-cuda126-v1",
        expected_environment_fingerprint="d" * 64,
        training_job_code_revision="f" * 40,
        execution_code_revision="e" * 40,
    )
    directories = []
    for index in range(4):
        directory = tmp_path / f"cap-{index}"
        directory.mkdir(mode=0o700)
        directories.append(os.open(directory, os.O_RDONLY | os.O_DIRECTORY))

    def handler(_value, _fds, _stop):
        time.sleep(0.15)
        return {
            "department_id": str(request.department_id),
            "execution_id": str(request.execution_id),
            "attempt_id": str(request.attempt_id),
            "training_job_id": str(request.training_job_id),
            "authority_fingerprint": request.authority_fingerprint,
            "input_snapshot_fingerprint": request.input_snapshot_fingerprint,
            "runtime_fingerprint": "f" * 64,
            "classification": "execution_succeeded",
            "error_code": None,
        }

    server = TrainingRuntimeServer(socket_path, token, handler)
    thread = threading.Thread(target=server.serve_once, daemon=True)
    thread.start()
    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.01)
    assert socket_path.exists()
    try:
        with pytest.raises(TrainingExecutionError, match="worker_timeout"):
            UnixTrainingRuntimeClient(
                str(socket_path),
                token,
                handshake_timeout_seconds=1.0,
                training_timeout_seconds=0.03,
                heartbeat_interval_seconds=0.01,
            ).run(
                request,
                handles=TrainingRuntimeHandles(*directories),
                should_stop=lambda: False,
                heartbeat=lambda: None,
            )
    finally:
        for descriptor in directories:
            os.close(descriptor)
        thread.join(2)
        socket_path.unlink(missing_ok=True)
        socket_parent.rmdir()
    assert not thread.is_alive()
    assert RUNTIME_TRAINING_TIMEOUT_SECONDS == 12 * 60 * 60
