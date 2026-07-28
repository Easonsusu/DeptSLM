"""Exec-isolated, heartbeat-supervised Phase 10 child operations.

The parent is the sole PostgreSQL claimant.  Each child starts through the
fixed ``app.sft_child`` entrypoint with a closed request schema, an exact
environment allowlist, and only explicitly passed external-artifact handles.
"""

from __future__ import annotations

import json
import os
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path

MAX_REQUEST_FRAME_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_FRAME_BYTES = 32 * 1024
_POLL_SECONDS = 0.2


class SftChildOperation(StrEnum):
    SELECT_SOURCE = "select_source"
    BUILD_DATASET = "build_dataset"
    BOUNDARY_PROBE = "boundary_probe"


def run_claimed_operation(
    *,
    timeout_seconds: int,
    heartbeat_seconds: int,
    should_stop: Callable[[], bool],
    heartbeat: Callable[[], None],
    error: Callable[[str], Exception],
    operation: SftChildOperation,
    request: Mapping[str, object],
    pass_fds: tuple[int, ...],
) -> dict[str, object]:
    """Run one closed child operation without blocking claim heartbeats.

    Both the request write and the response read are framed, bounded, and
    deadline-controlled.  The child cannot inherit the parent database engine,
    environment, configuration objects, or unrelated descriptors.
    """

    if not isinstance(operation, SftChildOperation):
        raise error("dataset_publication_failed")
    payload = _frame(
        {"operation": operation.value, "request": _json_value(request)}, MAX_REQUEST_FRAME_BYTES
    )
    checked_fds = tuple(sorted(set(pass_fds)))
    if any(not isinstance(descriptor, int) or descriptor < 0 for descriptor in checked_fds):
        raise error("dataset_publication_failed")
    child = subprocess.Popen(
        [sys.executable, "-m", "app.sft_child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        close_fds=True,
        pass_fds=checked_fds,
        start_new_session=True,
    )
    if child.stdin is None or child.stdout is None:
        _terminate_group(child)
        raise error("dataset_publication_failed")
    write_fd = child.stdin.fileno()
    read_fd = child.stdout.fileno()
    os.set_blocking(write_fd, False)
    os.set_blocking(read_fd, False)
    deadline = time.monotonic() + timeout_seconds
    heartbeat_interval = max(0.1, min(15.0, heartbeat_seconds / 3))
    heartbeat_at = time.monotonic()
    sent = 0
    received = bytearray()
    expected_size: int | None = None
    write_open = True
    try:
        while True:
            now = time.monotonic()
            if should_stop():
                raise error("worker_shutdown")
            if now >= deadline:
                raise error("worker_timeout")
            if now >= heartbeat_at:
                heartbeat()
                heartbeat_at = now + heartbeat_interval
            if write_open and sent == len(payload):
                child.stdin.close()
                write_open = False
            wait = min(_POLL_SECONDS, max(0.001, deadline - now), max(0.001, heartbeat_at - now))
            readable, writable, _ = select.select(
                [read_fd], [write_fd] if write_open else [], [], wait
            )
            if write_open and write_fd in writable:
                try:
                    count = os.write(write_fd, payload[sent : sent + 64 * 1024])
                except BlockingIOError:
                    count = 0
                except BrokenPipeError as caught:
                    raise error("dataset_publication_failed") from caught
                if count <= 0:
                    raise error("dataset_publication_failed")
                sent += count
            if read_fd in readable:
                try:
                    block = os.read(read_fd, 64 * 1024)
                except BlockingIOError:
                    block = b""
                if block:
                    received.extend(block)
                    expected_size = _response_size(received, expected_size)
                    if expected_size is not None and len(received) >= expected_size + 4:
                        if len(received) != expected_size + 4:
                            raise error("dataset_publication_failed")
                        response = _decode_response(bytes(received[4:]))
                        child.wait(timeout=1)
                        if response.get("status") != "ok":
                            raise error(_error_code(response.get("code")))
                        result = response.get("result")
                        if not isinstance(result, dict):
                            raise error("dataset_publication_failed")
                        return result
                elif child.poll() is not None:
                    raise error("dataset_publication_failed")
            if child.poll() is not None and expected_size is None:
                raise error("dataset_publication_failed")
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as caught:
        raise error("dataset_publication_failed") from caught
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.stdout.close()
        except OSError:
            pass
        _terminate_group(child)


def _frame(value: Mapping[str, object], maximum: int) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(raw) <= maximum:
        raise ValueError("frame is out of bounds")
    return struct.pack("!I", len(raw)) + raw


def _response_size(received: bytearray, expected_size: int | None) -> int | None:
    if expected_size is not None:
        return expected_size
    if len(received) < 4:
        return None
    size = struct.unpack("!I", received[:4])[0]
    if not 1 <= size <= MAX_RESPONSE_FRAME_BYTES:
        raise ValueError("response frame is out of bounds")
    return size


def _decode_response(raw: bytes) -> dict[str, object]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) - {"status", "code", "result"}:
        raise ValueError("response schema is invalid")
    return value


def _json_value(value: Mapping[str, object]) -> dict[str, object]:
    # A JSON round trip rejects Python objects, closures, descriptors, and
    # configuration instances before a child process is started.
    parsed = json.loads(json.dumps(dict(value), sort_keys=True, separators=(",", ":")))
    if not isinstance(parsed, dict):
        raise ValueError("request schema is invalid")
    return parsed


def _error_code(value: object) -> str:
    return value if isinstance(value, str) else "dataset_publication_failed"


def _terminate_group(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
