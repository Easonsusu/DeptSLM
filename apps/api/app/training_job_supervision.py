"""Killable, bounded IPC supervision for the Phase 11 bundle child."""

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
from pathlib import Path

MAX_REQUEST_FRAME_BYTES = 64 * 1024
MAX_RESPONSE_FRAME_BYTES = 32 * 1024


def run_training_job_child(
    *,
    timeout_seconds: int,
    heartbeat_seconds: int,
    should_stop: Callable[[], bool],
    heartbeat: Callable[[], None],
    error: Callable[[str], Exception],
    request: Mapping[str, object],
    pass_fds: tuple[int, ...],
) -> dict[str, object]:
    """Run the fixed child without blocking lease heartbeats or inheriting secrets."""

    raw = json.dumps(
        {"operation": "build_training_job", "request": dict(request)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(raw) <= MAX_REQUEST_FRAME_BYTES:
        raise error("training_job_publication_failed")
    descriptors = tuple(sorted(set(pass_fds)))
    if any(type(item) is not int or item < 0 for item in descriptors):
        raise error("training_job_publication_failed")
    child = subprocess.Popen(
        [sys.executable, "-m", "app.training_job_child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        close_fds=True,
        pass_fds=descriptors,
        start_new_session=True,
    )
    if child.stdin is None or child.stdout is None:
        _reap(child)
        raise error("training_job_publication_failed")
    payload = struct.pack("!I", len(raw)) + raw
    write_fd, read_fd = child.stdin.fileno(), child.stdout.fileno()
    os.set_blocking(write_fd, False)
    os.set_blocking(read_fd, False)
    deadline = time.monotonic() + timeout_seconds
    interval = max(0.1, min(15.0, heartbeat_seconds / 3))
    heartbeat_at = time.monotonic()
    sent = 0
    received = bytearray()
    expected: int | None = None
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
                heartbeat_at = now + interval
            if write_open and sent == len(payload):
                child.stdin.close()
                write_open = False
            timeout = min(0.2, max(0.001, deadline - now), max(0.001, heartbeat_at - now))
            readable, writable, _ = select.select(
                [read_fd], [write_fd] if write_open else [], [], timeout
            )
            if write_open and write_fd in writable:
                try:
                    sent += os.write(write_fd, payload[sent : sent + 64 * 1024])
                except (BlockingIOError, InterruptedError):
                    pass
                except BrokenPipeError as caught:
                    raise error("training_job_publication_failed") from caught
            if read_fd in readable:
                block = os.read(read_fd, 64 * 1024)
                if not block:
                    if child.poll() is not None:
                        raise error("training_job_publication_failed")
                    continue
                received.extend(block)
                if expected is None and len(received) >= 4:
                    expected = struct.unpack("!I", received[:4])[0]
                    if not 1 <= expected <= MAX_RESPONSE_FRAME_BYTES:
                        raise error("training_job_publication_failed")
                if expected is not None and len(received) >= expected + 4:
                    if len(received) != expected + 4:
                        raise error("training_job_publication_failed")
                    response = json.loads(received[4:].decode("utf-8"))
                    if not isinstance(response, dict) or response.get("status") != "ok":
                        raise error(
                            response.get("code")
                            if isinstance(response, dict)
                            else "training_job_publication_failed"
                        )
                    result = response.get("result")
                    if not isinstance(result, dict):
                        raise error("training_job_publication_failed")
                    child.wait(timeout=1)
                    return result
            if child.poll() is not None and expected is None:
                raise error("training_job_publication_failed")
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as caught:
        raise error("training_job_publication_failed") from caught
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.stdout.close()
        except OSError:
            pass
        _reap(child)


def _reap(child: subprocess.Popen[bytes]) -> None:
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
