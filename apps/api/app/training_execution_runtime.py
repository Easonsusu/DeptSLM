"""Process boundary for Phase 14.1 execution.

The API and normal worker contain only this protocol.  A fake implementation is
provided by tests through dependency injection; there is no production fake
environment switch and no LlamaFactory/model dependency here.
"""

from __future__ import annotations

import errno
import hmac
import json
import os
import re
import secrets
import select
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.training_execution_domain import (
    EXECUTION_ERROR_CODES,
    EXECUTION_MODEL_ID,
    EXECUTION_MODEL_REVISION,
    EXECUTION_PROFILES,
    REAL_RUNTIME_CONTRACT_VERSION,
    TrainingExecutionError,
    runtime_fingerprint,
)

RUNTIME_SOCKET_ENV = "DEPTSLM_TRAINING_RUNTIME_SOCKET"
RUNTIME_TOKEN_ENV = "DEPTSLM_TRAINING_RUNTIME_TOKEN"
MAX_RUNTIME_IPC_FRAME_BYTES = 65_536
RUNTIME_HANDSHAKE_TIMEOUT_SECONDS = 120.0
RUNTIME_TRAINING_TIMEOUT_SECONDS = 12 * 60 * 60
RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 30.0
_FRAME_HEADER = struct.Struct("!I")


class StopReason(StrEnum):
    """Closed worker-side reasons for interrupting a real runtime call."""

    CANCELLED = "cancelled"
    CLAIM_LOST = "claim_lost"
    WORKER_SHUTDOWN = "worker_shutdown"
    WORKER_TIMEOUT = "worker_timeout"


def _stop_reason(value: object) -> StopReason | None:
    if value is None or value is False:
        return None
    if isinstance(value, StopReason):
        return value
    if value is True:
        # A legacy external callback can only signal worker shutdown.  Claim
        # loss is supplied separately by the server-authoritative callback.
        return StopReason.WORKER_SHUTDOWN
    if isinstance(value, str):
        try:
            return StopReason(value)
        except ValueError:
            return StopReason.WORKER_SHUTDOWN
    return StopReason.WORKER_SHUTDOWN


@dataclass(frozen=True, slots=True)
class TrainingRuntimeRequest:
    contract_version: str
    department_id: UUID
    execution_id: UUID
    attempt_id: UUID
    training_job_id: UUID
    publication_attempt_id: UUID
    authority_fingerprint: str
    input_snapshot_fingerprint: str
    profile_id: str
    base_model_id: str
    base_model_revision: str
    attempt_namespace: UUID
    training_job_code_revision: str
    execution_code_revision: str
    runtime_contract_version: str = ""
    dependency_lock_sha256: str = ""
    environment_profile_id: str = ""
    expected_environment_fingerprint: str = ""

    def as_closed_mapping(self) -> dict[str, object]:
        return {
            "runtime_contract_version": self.runtime_contract_version,
            "department_id": str(self.department_id),
            "execution_id": str(self.execution_id),
            "attempt_id": str(self.attempt_id),
            "training_job_id": str(self.training_job_id),
            "publication_attempt_id": str(self.publication_attempt_id),
            "authority_fingerprint": self.authority_fingerprint,
            "input_snapshot_fingerprint": self.input_snapshot_fingerprint,
            "profile_id": self.profile_id,
            "base_model_id": self.base_model_id,
            "base_model_revision": self.base_model_revision,
            "attempt_namespace": str(self.attempt_namespace),
            "dependency_lock_sha256": self.dependency_lock_sha256,
            "environment_profile_id": self.environment_profile_id,
            "expected_environment_fingerprint": self.expected_environment_fingerprint,
            "training_job_code_revision": self.training_job_code_revision,
            "execution_code_revision": self.execution_code_revision,
        }


@dataclass(frozen=True, slots=True)
class TrainingRuntimeHandles:
    """Process-local capabilities kept outside the closed runtime protocol."""

    input_fd: int
    scratch_fd: int
    logs_fd: int
    output_stage_fd: int


@dataclass(frozen=True, slots=True)
class TrainingRuntimeResult:
    department_id: UUID
    execution_id: UUID
    attempt_id: UUID
    training_job_id: UUID
    authority_fingerprint: str
    input_snapshot_fingerprint: str
    runtime_fingerprint: str
    classification: str
    error_code: str | None = None
    runtime_kind: str = "fake"
    runtime_contract_version: str | None = None
    dependency_lock_sha256: str | None = None
    environment_profile_id: str | None = None
    environment_fingerprint: str | None = None
    hardware_profile_id: str | None = None
    hardware_fingerprint: str | None = None
    output_stage_fingerprint: str | None = None
    output_file_count: int | None = None
    output_total_bytes: int | None = None

    def as_closed_mapping(self) -> dict[str, object]:
        result = {
            "department_id": str(self.department_id),
            "execution_id": str(self.execution_id),
            "attempt_id": str(self.attempt_id),
            "training_job_id": str(self.training_job_id),
            "authority_fingerprint": self.authority_fingerprint,
            "input_snapshot_fingerprint": self.input_snapshot_fingerprint,
            "runtime_fingerprint": self.runtime_fingerprint,
            "classification": self.classification,
            "error_code": self.error_code,
        }
        if self.runtime_kind != "fake":
            result.update(
                {
                    "runtime_kind": self.runtime_kind,
                    "runtime_contract_version": self.runtime_contract_version,
                    "dependency_lock_sha256": self.dependency_lock_sha256,
                    "environment_profile_id": self.environment_profile_id,
                    "environment_fingerprint": self.environment_fingerprint,
                    "hardware_profile_id": self.hardware_profile_id,
                    "hardware_fingerprint": self.hardware_fingerprint,
                    "output_stage_fingerprint": self.output_stage_fingerprint,
                    "output_file_count": self.output_file_count,
                    "output_total_bytes": self.output_total_bytes,
                }
            )
        return result

    @classmethod
    def from_closed_mapping(cls, value: dict[str, object]) -> TrainingRuntimeResult:
        if not isinstance(value, dict):
            raise TrainingExecutionError("runtime_protocol_invalid")
        base_keys = {
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
        runtime_keys = base_keys | {
            "runtime_kind",
            "runtime_contract_version",
            "dependency_lock_sha256",
            "environment_profile_id",
            "environment_fingerprint",
            "hardware_profile_id",
            "hardware_fingerprint",
            "output_stage_fingerprint",
            "output_file_count",
            "output_total_bytes",
        }
        if value.get("runtime_kind", "fake") == "real":
            if set(value) != runtime_keys:
                raise TrainingExecutionError("runtime_protocol_invalid")
        elif set(value) != base_keys:
            raise TrainingExecutionError("runtime_protocol_invalid")
        return cls(
            department_id=UUID(str(value["department_id"])),
            execution_id=UUID(str(value["execution_id"])),
            attempt_id=UUID(str(value["attempt_id"])),
            training_job_id=UUID(str(value["training_job_id"])),
            authority_fingerprint=str(value["authority_fingerprint"]),
            input_snapshot_fingerprint=str(value["input_snapshot_fingerprint"]),
            runtime_fingerprint=str(value["runtime_fingerprint"]),
            classification=str(value["classification"]),
            error_code=value.get("error_code")
            if isinstance(value.get("error_code"), str)
            else None,
            runtime_kind=str(value.get("runtime_kind", "fake")),
            runtime_contract_version=value.get("runtime_contract_version")
            if isinstance(value.get("runtime_contract_version"), str)
            else None,
            dependency_lock_sha256=value.get("dependency_lock_sha256")
            if isinstance(value.get("dependency_lock_sha256"), str)
            else None,
            environment_profile_id=value.get("environment_profile_id")
            if isinstance(value.get("environment_profile_id"), str)
            else None,
            environment_fingerprint=value.get("environment_fingerprint")
            if isinstance(value.get("environment_fingerprint"), str)
            else None,
            hardware_profile_id=value.get("hardware_profile_id")
            if isinstance(value.get("hardware_profile_id"), str)
            else None,
            hardware_fingerprint=value.get("hardware_fingerprint")
            if isinstance(value.get("hardware_fingerprint"), str)
            else None,
            output_stage_fingerprint=value.get("output_stage_fingerprint")
            if isinstance(value.get("output_stage_fingerprint"), str)
            else None,
            output_file_count=value.get("output_file_count")
            if type(value.get("output_file_count")) is int
            else None,
            output_total_bytes=value.get("output_total_bytes")
            if type(value.get("output_total_bytes")) is int
            else None,
        )


class TrainingExecutionRuntime(Protocol):
    def run(
        self,
        request: TrainingRuntimeRequest,
        *,
        handles: TrainingRuntimeHandles,
        should_stop: Callable[[], object],
        heartbeat: Callable[[], object],
    ) -> TrainingRuntimeResult | dict[str, object]: ...


class UnavailableTrainingRuntime:
    """Production default: fail closed until a reviewed runtime is supplied."""

    def run(
        self,
        request: TrainingRuntimeRequest,
        *,
        handles: TrainingRuntimeHandles,
        should_stop: Callable[[], object],
        heartbeat: Callable[[], object],
    ) -> TrainingRuntimeResult:
        del handles, should_stop, heartbeat
        return TrainingRuntimeResult(
            request.department_id,
            request.execution_id,
            request.attempt_id,
            request.training_job_id,
            request.authority_fingerprint,
            request.input_snapshot_fingerprint,
            runtime_fingerprint(
                execution_id=request.execution_id,
                attempt_id=request.attempt_id,
                authority=request.authority_fingerprint,
            ),
            "execution_failed",
            "runtime_unavailable",
        )


class UnixTrainingRuntimeClient:
    """Private UDS client with separate handshake and training deadlines."""

    def __init__(
        self,
        socket_path: str,
        token: str,
        *,
        handshake_timeout_seconds: float = RUNTIME_HANDSHAKE_TIMEOUT_SECONDS,
        training_timeout_seconds: float = RUNTIME_TRAINING_TIMEOUT_SECONDS,
        timeout_seconds: float | None = None,
        heartbeat_interval_seconds: float = RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        if not socket_path.startswith("/") or not token or len(token) < 32:
            raise TrainingExecutionError("runtime_auth_failed")
        self._socket_path = socket_path
        self._token = token.encode("utf-8")
        # ``timeout_seconds`` is retained as a compatibility alias for old
        # callers that injected a short handshake timeout in tests.  It never
        # limits the active training/result stage.
        if timeout_seconds is not None:
            handshake_timeout_seconds = timeout_seconds
        self._handshake_timeout = max(0.01, handshake_timeout_seconds)
        self._training_timeout = max(0.01, training_timeout_seconds)
        self._heartbeat_interval = min(30.0, max(0.1, heartbeat_interval_seconds))

    def run(
        self,
        request: TrainingRuntimeRequest,
        *,
        handles: TrainingRuntimeHandles,
        should_stop: Callable[[], object],
        heartbeat: Callable[[], object],
    ) -> TrainingRuntimeResult:
        validate_runtime_request(request)
        fds = (handles.input_fd, handles.scratch_fd, handles.logs_fd, handles.output_stage_fd)
        if any(type(fd) is not int or fd < 0 for fd in fds) or len(set(fds)) != 4:
            raise TrainingExecutionError("runtime_protocol_invalid")
        nonce = secrets.token_bytes(32)
        request_bytes = _canonical_json(request.as_closed_mapping())
        envelope = {
            "nonce": nonce.hex(),
            "request": request.as_closed_mapping(),
            "mac": hmac.new(self._token, request_bytes + nonce, "sha256").hexdigest(),
        }
        frame = _encode_frame(envelope)
        handshake_deadline = time.monotonic() + self._handshake_timeout
        last_heartbeat = time.monotonic()
        stop_sent = False

        def send_stop(reason: StopReason, deadline: float) -> None:
            nonlocal stop_sent
            if stop_sent:
                return
            stop_sent = True
            try:
                _send_control_frame(
                    sock,
                    reason.value,
                    min(deadline, time.monotonic() + 1.0),
                )
            except (OSError, TrainingExecutionError):
                # The local stop remains authoritative even if the runtime
                # socket has already disappeared.  Never wait indefinitely
                # just to transmit a best-effort interruption notice.
                pass

        def checkpoint(deadline: float) -> None:
            nonlocal last_heartbeat
            reason = _stop_reason(should_stop())
            if reason is not None:
                send_stop(reason, deadline)
                raise TrainingExecutionError(reason.value)
            now = time.monotonic()
            if now - last_heartbeat >= self._heartbeat_interval:
                heartbeat_value = heartbeat()
                if heartbeat_value is False:
                    heartbeat_reason = StopReason.CLAIM_LOST
                elif heartbeat_value is True:
                    heartbeat_reason = None
                else:
                    heartbeat_reason = _stop_reason(heartbeat_value)
                if heartbeat_reason is not None:
                    send_stop(heartbeat_reason, deadline)
                    raise TrainingExecutionError(heartbeat_reason.value)
                last_heartbeat = now
            if now >= deadline:
                raise TrainingExecutionError("worker_timeout")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            _connect_with_deadline(
                sock,
                self._socket_path,
                handshake_deadline,
                lambda: checkpoint(handshake_deadline),
            )
            _send_fd_frame(
                sock,
                frame,
                fds,
                handshake_deadline,
                lambda: checkpoint(handshake_deadline),
            )
            response = bytearray()
            acknowledgement = _receive_frame(
                sock,
                response,
                handshake_deadline,
                lambda deadline: checkpoint(deadline),
            )
            _validate_process_ready(acknowledgement, request)
            training_deadline = time.monotonic() + self._training_timeout
            final = _receive_frame(
                sock,
                response,
                training_deadline,
                lambda deadline: checkpoint(deadline),
            )
            return TrainingRuntimeResult.from_closed_mapping(final)
        except TrainingExecutionError:
            raise
        except (OSError, ValueError, json.JSONDecodeError, struct.error) as error:
            raise TrainingExecutionError("runtime_disconnected") from error
        finally:
            sock.close()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _encode_frame(value: object) -> bytes:
    payload = _canonical_json(value)
    if not 1 <= len(payload) <= MAX_RUNTIME_IPC_FRAME_BYTES:
        raise TrainingExecutionError("runtime_protocol_invalid")
    return _FRAME_HEADER.pack(len(payload)) + payload


def _try_decode_frame(buffer: bytearray) -> dict[str, object] | None:
    if len(buffer) < _FRAME_HEADER.size:
        return None
    length = _FRAME_HEADER.unpack(buffer[: _FRAME_HEADER.size])[0]
    if not 1 <= length <= MAX_RUNTIME_IPC_FRAME_BYTES:
        raise TrainingExecutionError("runtime_protocol_invalid")
    if len(buffer) < _FRAME_HEADER.size + length:
        return None
    payload = bytes(buffer[_FRAME_HEADER.size : _FRAME_HEADER.size + length])
    del buffer[: _FRAME_HEADER.size + length]
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise TrainingExecutionError("runtime_protocol_invalid")
    return value


def _receive_frame(
    sock: socket.socket,
    buffer: bytearray,
    deadline: float,
    checkpoint: Callable[[float], None],
) -> dict[str, object]:
    while True:
        frame_value = _try_decode_frame(buffer)
        if frame_value is not None:
            return frame_value
        now = time.monotonic()
        checkpoint(deadline)
        ready, _, _ = select.select([sock], [], [], min(0.2, max(0.0, deadline - now)))
        if not ready:
            continue
        chunk = sock.recv(16 * 1024)
        if not chunk:
            raise TrainingExecutionError("runtime_disconnected")
        buffer.extend(chunk)
        if len(buffer) > MAX_RUNTIME_IPC_FRAME_BYTES * 2 + _FRAME_HEADER.size:
            raise TrainingExecutionError("runtime_protocol_invalid")


def _validate_process_ready(value: dict[str, object], request: TrainingRuntimeRequest) -> None:
    expected = {
        "classification",
        "department_id",
        "execution_id",
        "attempt_id",
        "training_job_id",
        "authority_fingerprint",
        "input_snapshot_fingerprint",
    }
    if set(value) != expected or value.get("classification") != "process_ready":
        raise TrainingExecutionError("runtime_protocol_invalid")
    for key, expected_value in (
        ("department_id", str(request.department_id)),
        ("execution_id", str(request.execution_id)),
        ("attempt_id", str(request.attempt_id)),
        ("training_job_id", str(request.training_job_id)),
        ("authority_fingerprint", request.authority_fingerprint),
        ("input_snapshot_fingerprint", request.input_snapshot_fingerprint),
    ):
        if value.get(key) != expected_value:
            raise TrainingExecutionError("runtime_protocol_invalid")


def _connect_with_deadline(
    sock: socket.socket,
    path: str,
    deadline: float,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    result = sock.connect_ex(path)
    while result not in (0, errno.EISCONN):
        if checkpoint is not None:
            checkpoint()
        if time.monotonic() >= deadline:
            raise TrainingExecutionError("runtime_unavailable")
        if result not in (errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
            raise OSError(result, os.strerror(result))
        _, writable, _ = select.select([], [sock], [], min(0.2, deadline - time.monotonic()))
        if writable:
            result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)


def _send_fd_frame(
    sock: socket.socket,
    frame: bytes,
    fds: tuple[int, ...],
    deadline: float,
    checkpoint: Callable[[], None] | None = None,
) -> None:
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=4i", *fds))]
    sent = 0
    first_send = True
    while sent < len(frame):
        if checkpoint is not None:
            checkpoint()
        if time.monotonic() >= deadline:
            raise TrainingExecutionError("worker_timeout")
        try:
            if first_send:
                written = sock.sendmsg([frame[sent:]], ancillary)
            else:
                written = sock.send(frame[sent:])
        except BlockingIOError:
            written = 0
        if written > 0:
            sent += written
            first_send = False
            continue
        _, writable, _ = select.select([], [sock], [], min(0.2, deadline - time.monotonic()))
        if not writable:
            continue


def _send_control_frame(sock: socket.socket, reason: str, deadline: float) -> None:
    if reason not in {item.value for item in StopReason}:
        raise TrainingExecutionError("runtime_protocol_invalid")
    frame = _encode_frame({"control": "stop", "reason": reason})
    sent = 0
    while sent < len(frame):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TrainingExecutionError("worker_timeout")
        try:
            written = sock.send(frame[sent:])
        except BlockingIOError:
            written = 0
        if written > 0:
            sent += written
            continue
        _, writable, _ = select.select([], [sock], [], min(0.05, remaining))
        if not writable:
            continue


def validate_runtime_request(request: TrainingRuntimeRequest) -> None:
    if (
        request.contract_version != "phase14-training-execution-v1"
        or not isinstance(request.department_id, UUID)
        or not isinstance(request.execution_id, UUID)
        or not isinstance(request.attempt_id, UUID)
        or not isinstance(request.training_job_id, UUID)
        or not isinstance(request.publication_attempt_id, UUID)
        or not isinstance(request.attempt_namespace, UUID)
        or request.profile_id not in EXECUTION_PROFILES
        or request.base_model_id != EXECUTION_MODEL_ID
        or request.base_model_revision != EXECUTION_MODEL_REVISION
        or not re.fullmatch(r"[0-9a-f]{40}", request.training_job_code_revision)
        or not re.fullmatch(r"[0-9a-f]{40}", request.execution_code_revision)
        or re.fullmatch(r"[0-9a-f]{64}", request.authority_fingerprint) is None
        or re.fullmatch(r"[0-9a-f]{64}", request.input_snapshot_fingerprint) is None
        or any(
            identifier.int == 0
            for identifier in (
                request.department_id,
                request.execution_id,
                request.attempt_id,
                request.training_job_id,
                request.publication_attempt_id,
                request.attempt_namespace,
            )
        )
    ):
        raise TrainingExecutionError("runtime_protocol_invalid")
    if request.runtime_contract_version:
        if request.runtime_contract_version != REAL_RUNTIME_CONTRACT_VERSION:
            raise TrainingExecutionError("runtime_protocol_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", request.dependency_lock_sha256):
            raise TrainingExecutionError("runtime_protocol_invalid")
        if not request.environment_profile_id or not re.fullmatch(
            r"[0-9a-f]{64}", request.expected_environment_fingerprint
        ):
            raise TrainingExecutionError("runtime_protocol_invalid")


def validate_runtime_result_shape(
    result: TrainingRuntimeResult | dict[str, object],
) -> dict[str, object]:
    if isinstance(result, TrainingRuntimeResult):
        result = result.as_closed_mapping()
    if not isinstance(result, dict):
        raise TrainingExecutionError("runtime_protocol_invalid")
    allowed = {
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
    if set(result) != allowed:
        raise TrainingExecutionError("runtime_protocol_invalid")
    if result.get("error_code") is not None and result["error_code"] not in EXECUTION_ERROR_CODES:
        raise TrainingExecutionError("runtime_protocol_invalid")
    return result


__all__ = [
    "StopReason",
    "TrainingExecutionRuntime",
    "TrainingRuntimeHandles",
    "TrainingRuntimeRequest",
    "TrainingRuntimeResult",
    "UnixTrainingRuntimeClient",
    "UnavailableTrainingRuntime",
    "validate_runtime_request",
    "validate_runtime_result_shape",
]
