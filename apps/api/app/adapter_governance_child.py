"""Fixed, model-free validator for an already-open registry-final pair."""

from __future__ import annotations

import json
import os
import stat
import struct
import sys

from app.adapter_contract import (
    ADAPTER_CONFIG_CONTRACT_VERSION,
    MAX_CONFIG_BYTES,
    parse_canonical_adapter_config,
    validate_safetensors_metadata,
)

MAX_REQUEST_FRAME_BYTES = 16 * 1024
MAX_RESPONSE_FRAME_BYTES = 16 * 1024
CHILD_ERROR_CODES = frozenset(
    {
        "adapter_config_invalid",
        "adapter_config_unsupported",
        "adapter_header_invalid",
        "adapter_header_too_large",
        "adapter_file_too_large",
        "adapter_tensor_set_invalid",
        "adapter_tensor_shape_invalid",
        "adapter_tensor_dtype_invalid",
        "adapter_tensor_offsets_invalid",
        "adapter_tensor_size_invalid",
        "adapter_registry_authority_changed",
        "adapter_registry_manifest_invalid",
    }
)


class AdapterGovernanceChildError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in CHILD_ERROR_CODES else "adapter_registry_authority_changed"
        super().__init__(self.code)


class _PreadReader:
    """Descriptor-bound bounded reader used by the safetensors validator."""

    def __init__(self, descriptor: int, limit: int) -> None:
        self.descriptor = descriptor
        self.limit = limit
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if type(size) is not int or size < 0 or self.offset + size > self.limit:
            raise ValueError("invalid bounded read")
        result = os.pread(self.descriptor, size, self.offset)
        if len(result) != size:
            raise ValueError("short bounded read")
        self.offset += len(result)
        return result


def _read_bounded(descriptor: int, size: int) -> bytes:
    if type(size) is not int or size <= 0 or size > MAX_CONFIG_BYTES:
        raise AdapterGovernanceChildError("adapter_config_invalid")
    raw = os.pread(descriptor, size, 0)
    if len(raw) != size:
        raise AdapterGovernanceChildError("adapter_registry_authority_changed")
    return raw


def validate_descriptors(request: dict[str, object]) -> dict[str, object]:
    expected = {"config_fd", "model_fd", "config_size", "model_size"}
    if not isinstance(request, dict) or set(request) != expected:
        raise AdapterGovernanceChildError("adapter_registry_manifest_invalid")
    config_fd, model_fd = request["config_fd"], request["model_fd"]
    config_size, model_size = request["config_size"], request["model_size"]
    if any(type(value) is not int or value < 0 for value in (config_fd, model_fd)):
        raise AdapterGovernanceChildError("adapter_registry_authority_changed")
    if config_fd == model_fd:
        raise AdapterGovernanceChildError("adapter_registry_authority_changed")
    if any(type(value) is not int or value <= 0 for value in (config_size, model_size)):
        raise AdapterGovernanceChildError("adapter_registry_authority_changed")
    config_meta = os.fstat(config_fd)
    model_meta = os.fstat(model_fd)
    for metadata, size in ((config_meta, config_size), (model_meta, model_size)):
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != size
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise AdapterGovernanceChildError("adapter_registry_authority_changed")
    parse_canonical_adapter_config(_read_bounded(config_fd, config_size))
    tensor_summary = validate_safetensors_metadata(
        _PreadReader(model_fd, min(model_size, 8 + 1_048_576)), model_size
    )
    if os.fstat(config_fd).st_size != config_size or os.fstat(model_fd).st_size != model_size:
        raise AdapterGovernanceChildError("adapter_registry_authority_changed")
    return {
        "config_contract_version": ADAPTER_CONFIG_CONTRACT_VERSION,
        "tensor_contract_version": tensor_summary.contract_version,
        "tensor_dtype": tensor_summary.dtype,
        "tensor_count": tensor_summary.tensor_count,
        "tensor_element_count": tensor_summary.total_tensor_elements,
        "tensor_payload_byte_size": tensor_summary.total_tensor_bytes,
    }


def _read_frame() -> dict[str, object]:
    prefix = sys.stdin.buffer.read(4)
    if len(prefix) != 4:
        raise ValueError("short request")
    size = struct.unpack("!I", prefix)[0]
    if not 1 <= size <= MAX_REQUEST_FRAME_BYTES:
        raise ValueError("request too large")
    raw = sys.stdin.buffer.read(size)
    if len(raw) != size:
        raise ValueError("short request")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {"operation", "request"}:
        raise ValueError("request schema")
    if value["operation"] != "validate_registry_final":
        raise ValueError("request operation")
    return value["request"]


def _write_frame(value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(raw) <= MAX_RESPONSE_FRAME_BYTES:
        raw = b'{"status":"error","code":"adapter_registry_authority_changed"}'
    sys.stdout.buffer.write(struct.pack("!I", len(raw)) + raw)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        result = validate_descriptors(_read_frame())
    except AdapterGovernanceChildError as error:
        _write_frame({"status": "error", "code": error.code})
        return 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        _write_frame({"status": "error", "code": "adapter_registry_authority_changed"})
        return 1
    _write_frame({"status": "ok", "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
