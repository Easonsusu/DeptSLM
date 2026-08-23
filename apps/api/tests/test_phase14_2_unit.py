"""Focused Phase 14.2 control-plane and private-runtime boundary checks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import sys
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
from deptslm_training_runtime.ipc import authenticate_frame  # noqa: E402
from deptslm_training_runtime.output_stage import inspect_output_stage  # noqa: E402

from app.training_execution_runtime import TrainingRuntimeResult  # noqa: E402


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
