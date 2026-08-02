"""Bounded, killable, secret-free supervision for the registry child."""

from __future__ import annotations

import hashlib
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
from uuid import UUID

from app.adapter_registry_artifacts import AdapterRegistryArtifactError
from app.adapter_registry_child import (
    CHILD_ERROR_CODES,
    MAX_REQUEST_FRAME_BYTES,
    MAX_RESPONSE_FRAME_BYTES,
)
from app.adapter_registry_domain import canonical_json_bytes, parse_registry_manifest

DEFAULT_TIMEOUT_SECONDS = 120
_POLL_SECONDS = 0.1
_RESULT_KEYS = frozenset(
    {
        "publication_manifest",
        "artifact_contract_version",
        "manifest_contract_version",
        "registry_manifest_sha256",
        "registry_manifest_byte_size",
        "registry_adapter_config_sha256",
        "registry_adapter_config_byte_size",
        "registry_adapter_model_sha256",
        "registry_adapter_model_byte_size",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
    }
)


def run_adapter_registry_child(
    *,
    source_config_fd: int,
    source_model_fd: int,
    source_manifest_fd: int,
    training_manifest_fd: int,
    stage_fd: int,
    source_config_size: int,
    source_model_size: int,
    source_manifest_size: int,
    training_manifest_size: int,
    department_id: UUID,
    adapter_id: UUID,
    publication_attempt_id: UUID,
    attempt_number: int,
    code_revision: str,
    source: dict[str, object],
    governance_lineage: dict[str, object],
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> dict[str, object]:
    descriptors = tuple(
        sorted(
            {
                source_config_fd,
                source_model_fd,
                source_manifest_fd,
                training_manifest_fd,
                stage_fd,
            }
        )
    )
    if any(type(value) is not int or value < 0 for value in descriptors):
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    request = {
        "source_config_fd": source_config_fd,
        "source_model_fd": source_model_fd,
        "source_manifest_fd": source_manifest_fd,
        "training_manifest_fd": training_manifest_fd,
        "stage_fd": stage_fd,
        "source_config_size": source_config_size,
        "source_model_size": source_model_size,
        "source_manifest_size": source_manifest_size,
        "training_manifest_size": training_manifest_size,
        "department_id": str(department_id),
        "adapter_id": str(adapter_id),
        "publication_attempt_id": str(publication_attempt_id),
        "attempt_number": attempt_number,
        "code_revision": code_revision,
        "source": source,
        "governance_lineage": governance_lineage,
    }
    raw = json.dumps(
        {"operation": "build_registry", "request": request},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not 1 <= len(raw) <= MAX_REQUEST_FRAME_BYTES:
        raise AdapterRegistryArtifactError("adapter_registry_manifest_invalid")
    payload = struct.pack("!I", len(raw)) + raw
    child = subprocess.Popen(
        [sys.executable, "-m", "app.adapter_registry_child"],
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
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
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
                        "adapter_registry_publication_failed"
                    ) from error
            if read_fd in readable:
                block = os.read(read_fd, 32 * 1024)
                if block:
                    received.extend(block)
                    if expected is None and len(received) >= 4:
                        expected = struct.unpack("!I", received[:4])[0]
                        if not 1 <= expected <= MAX_RESPONSE_FRAME_BYTES:
                            raise AdapterRegistryArtifactError(
                                "adapter_registry_publication_failed"
                            )
                    if expected is not None and len(received) >= expected + 4:
                        if len(received) != expected + 4:
                            raise AdapterRegistryArtifactError(
                                "adapter_registry_publication_failed"
                            )
                        value = json.loads(received[4:].decode("utf-8"))
                        return _validate_response(value)
                elif child.poll() is not None:
                    raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
            if child.poll() is not None and expected is None:
                raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    except AdapterRegistryArtifactError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed") from error
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


def _validate_response(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    if value.get("status") == "error":
        if set(value) != {"status", "code"} or value.get("code") not in CHILD_ERROR_CODES:
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
        raise AdapterRegistryArtifactError(str(value["code"]))
    if set(value) != {"status", "result"} or value.get("status") != "ok":
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    result = value.get("result")
    if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    if not isinstance(result["publication_manifest"], dict):
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    for key in (
        "registry_manifest_sha256",
        "registry_adapter_config_sha256",
        "registry_adapter_model_sha256",
    ):
        if (
            not isinstance(result[key], str)
            or len(result[key]) != 64
            or any(char not in "0123456789abcdef" for char in result[key])
        ):
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    for key in (
        "registry_manifest_byte_size",
        "registry_adapter_config_byte_size",
        "registry_adapter_model_byte_size",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
    ):
        if type(result[key]) is not int or result[key] <= 0:
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    if result["tensor_dtype"] not in {"F16", "BF16", "F32"}:
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
    try:
        manifest_bytes = canonical_json_bytes(result["publication_manifest"])
        parse_registry_manifest(manifest_bytes)
    except (TypeError, ValueError, UnicodeError) as error:
        raise AdapterRegistryArtifactError("adapter_registry_manifest_invalid") from error
    if (
        hashlib.sha256(manifest_bytes).hexdigest() != result["registry_manifest_sha256"]
        or len(manifest_bytes) != result["registry_manifest_byte_size"]
    ):
        raise AdapterRegistryArtifactError("adapter_registry_manifest_invalid")
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


__all__ = ["run_adapter_registry_child", "_validate_response"]
