"""Bounded, secret-free IPC supervision for the Phase 12.1B validator child."""

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

from app.adapter_contract import (
    ADAPTER_CONFIG_CONTRACT_VERSION,
    ADAPTER_SOURCE_CONTRACT_VERSION,
    ADAPTER_TENSOR_CONTRACT_VERSION,
    BASE_MODEL_ID,
    EXPECTED_TENSOR_BYTES,
    EXPECTED_TENSOR_COUNT,
    EXPECTED_TENSOR_ELEMENTS,
    PEFT_FORMAT_REFERENCE_VERSION,
    SAFETENSORS_FORMAT_REFERENCE_VERSION,
)
from app.adapter_source_artifacts import AdapterSourceArtifactError
from app.adapter_source_child import CHILD_ERROR_CODES

MAX_REQUEST_FRAME_BYTES = 64 * 1024
MAX_RESPONSE_FRAME_BYTES = 16 * 1024
DEFAULT_TIMEOUT_SECONDS = 30
_POLL_SECONDS = 0.1
_SUCCESS_KEYS = frozenset(
    {
        "source_contract_version",
        "config_contract_version",
        "tensor_contract_version",
        "base_model_id",
        "base_model_display_id",
        "peft_version",
        "safetensors_format",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
    }
)
_INTEGER_RESULT_KEYS = frozenset(
    {"tensor_count", "tensor_element_count", "tensor_payload_byte_size"}
)


def run_adapter_source_validation(
    *,
    config_fd: int,
    model_fd: int,
    config_size: int,
    model_size: int,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Run the fixed child with descriptors and metadata only.

    The request never contains source bytes, a pathname, a digest, or an
    environment/configuration object.  Writes and reads are both
    non-blocking and deadline bounded, so a child that refuses to read cannot
    hold an administrator command indefinitely.
    """

    descriptors = tuple(sorted({config_fd, model_fd}))
    if any(type(value) is not int or value < 0 for value in descriptors):
        raise AdapterSourceArtifactError("adapter_input_invalid")
    if any(type(value) is not int or value <= 0 for value in (config_size, model_size)):
        raise AdapterSourceArtifactError("adapter_input_invalid")
    request = {
        "config_fd": config_fd,
        "model_fd": model_fd,
        "config_size": config_size,
        "model_size": model_size,
        "source_contract_version": ADAPTER_SOURCE_CONTRACT_VERSION,
        "config_contract_version": ADAPTER_CONFIG_CONTRACT_VERSION,
        "tensor_contract_version": ADAPTER_TENSOR_CONTRACT_VERSION,
    }
    raw = json.dumps(
        {"operation": "validate_adapter_source", "request": request},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(raw) <= MAX_REQUEST_FRAME_BYTES:
        raise AdapterSourceArtifactError("adapter_input_invalid")
    payload = struct.pack("!I", len(raw)) + raw
    child = subprocess.Popen(
        [sys.executable, "-m", "app.adapter_source_child"],
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
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    write_fd, read_fd = child.stdin.fileno(), child.stdout.fileno()
    os.set_blocking(write_fd, False)
    os.set_blocking(read_fd, False)
    deadline = time.monotonic() + max(1, timeout_seconds)
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
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
            if now >= deadline:
                raise AdapterSourceArtifactError("adapter_source_publication_failed")
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
                    raise AdapterSourceArtifactError("adapter_source_publication_failed") from error
            if read_fd in readable:
                block = os.read(read_fd, 16 * 1024)
                if block:
                    received.extend(block)
                    if expected is None and len(received) >= 4:
                        expected = struct.unpack("!I", received[:4])[0]
                        if not 1 <= expected <= MAX_RESPONSE_FRAME_BYTES:
                            raise AdapterSourceArtifactError("adapter_source_publication_failed")
                    if expected is not None and len(received) >= expected + 4:
                        if len(received) != expected + 4:
                            raise AdapterSourceArtifactError("adapter_source_publication_failed")
                        value = json.loads(received[4:].decode("utf-8"))
                        if not isinstance(value, dict):
                            raise AdapterSourceArtifactError("adapter_source_publication_failed")
                        if value.get("status") == "error":
                            if set(value) != {"status", "code"}:
                                raise AdapterSourceArtifactError(
                                    "adapter_source_publication_failed"
                                )
                            code = value.get("code")
                            if not isinstance(code, str) or code not in CHILD_ERROR_CODES:
                                raise AdapterSourceArtifactError(
                                    "adapter_source_publication_failed"
                                )
                            raise AdapterSourceArtifactError(code)
                        if set(value) != {"status", "result"} or value.get("status") != "ok":
                            raise AdapterSourceArtifactError("adapter_source_publication_failed")
                        return _validate_success_result(value.get("result"))
                elif child.poll() is not None:
                    raise AdapterSourceArtifactError("adapter_source_publication_failed")
            if child.poll() is not None and expected is None:
                raise AdapterSourceArtifactError("adapter_source_publication_failed")
    except AdapterSourceArtifactError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        try:
            child.stdout.close()
        except OSError:
            pass
        _terminate(child)


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


def _validate_success_result(result: object) -> dict[str, object]:
    """Accept only the complete fixed content-free child success schema."""

    if not isinstance(result, dict) or set(result) != _SUCCESS_KEYS:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    expected_strings = {
        "source_contract_version": ADAPTER_SOURCE_CONTRACT_VERSION,
        "config_contract_version": ADAPTER_CONFIG_CONTRACT_VERSION,
        "tensor_contract_version": ADAPTER_TENSOR_CONTRACT_VERSION,
        "base_model_id": BASE_MODEL_ID,
        "base_model_display_id": BASE_MODEL_ID,
        "peft_version": PEFT_FORMAT_REFERENCE_VERSION,
        "safetensors_format": SAFETENSORS_FORMAT_REFERENCE_VERSION,
    }
    for key, expected in expected_strings.items():
        if type(result.get(key)) is not str or result[key] != expected:
            raise AdapterSourceArtifactError("adapter_source_publication_failed")
    if type(result.get("tensor_dtype")) is not str or result["tensor_dtype"] not in {
        "F16",
        "BF16",
        "F32",
    }:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    for key in _INTEGER_RESULT_KEYS:
        if type(result.get(key)) is not int or result[key] <= 0:
            raise AdapterSourceArtifactError("adapter_source_publication_failed")
    if result["tensor_count"] != EXPECTED_TENSOR_COUNT:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    if result["tensor_element_count"] != EXPECTED_TENSOR_ELEMENTS:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    if result["tensor_payload_byte_size"] != EXPECTED_TENSOR_BYTES[result["tensor_dtype"]]:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    return dict(result)


__all__ = [
    "MAX_REQUEST_FRAME_BYTES",
    "MAX_RESPONSE_FRAME_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "_validate_success_result",
    "run_adapter_source_validation",
]
