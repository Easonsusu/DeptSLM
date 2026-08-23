"""Real AF_UNIX/SCM_RIGHTS protocol tests; no model or CUDA is required."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import stat
import struct
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from deptslm_training_runtime.contract import (
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
    RUNTIME_CONTRACT_VERSION,
    canonical_json_bytes,
)
from deptslm_training_runtime.ipc import TrainingRuntimeServer

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real SCM_RIGHTS requires Linux CI")


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


def _frame(request: dict[str, object], token: bytes, *, nonce: bytes | None = None) -> bytes:
    nonce = nonce or os.urandom(32)
    envelope = {
        "nonce": nonce.hex(),
        "request": request,
        "mac": hmac.new(token, canonical_json_bytes(request) + nonce, hashlib.sha256).hexdigest(),
    }
    payload = canonical_json_bytes(envelope)
    return struct.pack("!I", len(payload)) + payload


def _wait_socket(path: Path) -> None:
    for _ in range(100):
        if path.exists():
            return
        time.sleep(0.01)
    pytest.fail("runtime socket did not become ready")


def _socket_path(root: Path, label: str) -> Path:
    parent = root / f"deptslm-runtime-{os.getpid()}-{uuid4().hex}-{label}"
    parent.mkdir(mode=0o700)
    return parent / "runtime.sock"


def _send_request(
    path: Path, frame: bytes, descriptors: list[int]
) -> tuple[dict[str, object], int]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.connect(str(path))
    try:
        ancillary = [
            (
                socket.SOL_SOCKET,
                socket.SCM_RIGHTS,
                struct.pack(f"={len(descriptors)}i", *descriptors),
            )
        ]
        connection.sendmsg([frame], ancillary)
        header = connection.recv(4)
        assert len(header) == 4
        size = struct.unpack("!I", header)[0]
        body = connection.recv(size)
        while len(body) < size:
            body += connection.recv(size - len(body))
        return json.loads(body.decode("utf-8")), stat.S_IMODE(path.stat().st_mode)
    finally:
        connection.close()


def _directories(root: Path, count: int) -> list[int]:
    descriptors: list[int] = []
    for index in range(count):
        path = root / f"cap-{index}"
        path.mkdir(mode=0o700)
        descriptors.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY))
    return descriptors


def test_real_four_directory_capabilities_and_private_socket(tmp_path: Path) -> None:
    parent = tmp_path / "ipc"
    parent.mkdir(mode=0o700)
    socket_path = _socket_path(tmp_path, "four")
    token = b"runtime-token-" + b"x" * 40
    request = _request()
    source = _directories(tmp_path, 4)
    seen: list[tuple[int, int]] = []
    ready = threading.Event()

    def handler(value, fds, _disconnected):
        assert value == request
        for original, received in zip(source, fds, strict=True):
            expected = os.fstat(original)
            actual = os.fstat(received)
            seen.append((actual.st_dev, actual.st_ino))
            assert (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
        ready.set()
        return {"classification": "execution_failed", "error_code": "runtime_hardware_unsupported"}

    thread = threading.Thread(
        target=TrainingRuntimeServer(socket_path, token.decode(), handler).serve_once,
        daemon=True,
    )
    thread.start()
    _wait_socket(socket_path)
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    response, _mode = _send_request(socket_path, _frame(request, token), source)
    assert response == {
        "classification": "execution_failed",
        "error_code": "runtime_hardware_unsupported",
    }
    assert ready.wait(1)
    thread.join(2)
    assert not thread.is_alive()
    for descriptor in source:
        os.close(descriptor)
    assert len(seen) == 4
    payload = json.dumps(request)
    assert "/" not in payload or "Qwen/Qwen3-0.6B" in payload
    assert all(str(descriptor) not in payload for descriptor in source)


@pytest.mark.parametrize("count", [3, 5])
def test_wrong_fd_count_is_rejected(tmp_path: Path, count: int) -> None:
    parent = tmp_path / "ipc"
    parent.mkdir(mode=0o700)
    socket_path = _socket_path(tmp_path, str(count))
    token = b"runtime-token-" + b"x" * 40
    thread = threading.Thread(
        target=TrainingRuntimeServer(socket_path, token.decode(), lambda *_: {}).serve_once,
        daemon=True,
    )
    thread.start()
    _wait_socket(socket_path)
    descriptors = _directories(tmp_path, count)
    try:
        response, _mode = _send_request(socket_path, _frame(_request(), token), descriptors)
        assert response["error_code"] == "runtime_protocol_invalid"
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    thread.join(2)
    assert not thread.is_alive()


def test_bad_hmac_and_non_directory_capability_are_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "ipc"
    parent.mkdir(mode=0o700)
    socket_path = _socket_path(tmp_path, "bad")
    token = b"runtime-token-" + b"x" * 40
    thread = threading.Thread(
        target=TrainingRuntimeServer(socket_path, token.decode(), lambda *_: {}).serve_once,
        daemon=True,
    )
    thread.start()
    _wait_socket(socket_path)
    descriptors = _directories(tmp_path, 3)
    file_path = tmp_path / "not-a-directory"
    file_path.write_bytes(b"x")
    descriptors.append(os.open(file_path, os.O_RDONLY))
    try:
        tampered = bytearray(_frame(_request(), token))
        tampered[-1] ^= 1
        response, _mode = _send_request(socket_path, bytes(tampered), descriptors)
        assert response["error_code"] == "runtime_auth_failed"
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    thread.join(2)
    assert not thread.is_alive()
