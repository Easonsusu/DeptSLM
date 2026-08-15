"""Bounded supervision for the model-free governance validator child."""

from __future__ import annotations

import json
import os
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from app.adapter_governance_child import (
    CHILD_ERROR_CODES,
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
)
from app.adapter_registry_artifacts import AdapterRegistryArtifactError

DEFAULT_TIMEOUT_SECONDS = 120
_POLL_SECONDS = 0.1
_RESULT_KEYS = frozenset(
    {
        "config_contract_version",
        "tensor_contract_version",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
    }
)


def run_adapter_governance_validation_child(
    *,
    config_fd: int,
    model_fd: int,
    config_size: int,
    model_size: int,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, object]:
    if (
        type(timeout_seconds) is not int
        or timeout_seconds <= 0
        or type(config_fd) is not int
        or type(model_fd) is not int
        or config_fd < 0
        or model_fd < 0
        or config_fd == model_fd
    ):
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    descriptors = (config_fd, model_fd)
    request = {
        "config_fd": config_fd,
        "model_fd": model_fd,
        "config_size": config_size,
        "model_size": model_size,
    }
    raw = json.dumps(
        {"operation": "validate_registry_final", "request": request},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(raw) <= MAX_REQUEST_FRAME_BYTES:
        raise AdapterRegistryArtifactError("adapter_registry_manifest_invalid")
    payload = struct.pack("!I", len(raw)) + raw
    child = subprocess.Popen(
        [sys.executable, "-m", "app.adapter_governance_child"],
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
        _terminate(child)
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    write_fd, read_fd = child.stdin.fileno(), child.stdout.fileno()
    os.set_blocking(write_fd, False)
    os.set_blocking(read_fd, False)
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    check_stop = should_stop or (lambda: False)
    beat = heartbeat or (lambda: None)
    sent = 0
    received = bytearray()
    expected: int | None = None
    write_open = True
    try:
        while True:
            now = time.monotonic()
            if check_stop():
                raise AdapterRegistryArtifactError("claim_lost")
            if now >= deadline:
                raise AdapterRegistryArtifactError("worker_timeout")
            beat()
            if write_open and sent == len(payload):
                child.stdin.close()
                write_open = False
            readable, writable, _ = select.select(
                [read_fd], [write_fd] if write_open else [], [], min(_POLL_SECONDS, deadline - now)
            )
            if write_open and write_fd in writable:
                try:
                    sent += os.write(write_fd, payload[sent : sent + 16 * 1024])
                except (BlockingIOError, InterruptedError):
                    pass
                except BrokenPipeError as error:
                    raise AdapterRegistryArtifactError(
                        "adapter_registry_authority_changed"
                    ) from error
            if read_fd in readable:
                block = os.read(read_fd, 16 * 1024)
                if block:
                    received.extend(block)
                    if expected is None and len(received) >= 4:
                        expected = struct.unpack("!I", received[:4])[0]
                        if not 1 <= expected <= MAX_RESPONSE_FRAME_BYTES:
                            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
                    if expected is not None and len(received) >= expected + 4:
                        if len(received) != expected + 4:
                            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
                        value = json.loads(received[4:].decode("utf-8"))
                        return _validate_response(value)
                elif child.poll() is not None:
                    raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
            if child.poll() is not None and expected is None:
                raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    except AdapterRegistryArtifactError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed") from error
    finally:
        try:
            child.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            child.stdout.close()
        except OSError:
            pass
        _terminate(child)


def _validate_response(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    if value.get("status") == "error":
        if set(value) != {"status", "code"}:
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        code = value.get("code")
        if not isinstance(code, str) or code not in CHILD_ERROR_CODES:
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        raise AdapterRegistryArtifactError(code)
    if set(value) != {"status", "result"} or value.get("status") != "ok":
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    result = value.get("result")
    if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    if any(
        not isinstance(result[key], str)
        for key in ("config_contract_version", "tensor_contract_version", "tensor_dtype")
    ):
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    numeric_keys = _RESULT_KEYS - {
        "config_contract_version",
        "tensor_contract_version",
        "tensor_dtype",
    }
    if any(type(result[key]) is not int or result[key] <= 0 for key in numeric_keys):
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    return dict(result)


def _terminate(child: subprocess.Popen[bytes]) -> None:
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
