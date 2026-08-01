"""Fixed, model-free validation child for Phase 12.1B source intake."""

from __future__ import annotations

import json
import os
import stat
import struct
import sys

from app.adapter_contract import (
    ADAPTER_CONFIG_CONTRACT_VERSION,
    ADAPTER_SOURCE_CONTRACT_VERSION,
    ADAPTER_TENSOR_CONTRACT_VERSION,
    BASE_MODEL_ID,
    PEFT_FORMAT_REFERENCE_VERSION,
    SAFETENSORS_FORMAT_REFERENCE_VERSION,
    AdapterContractError,
    parse_external_adapter_config,
    validate_safetensors_metadata,
)

MAX_REQUEST_FRAME_BYTES = 64 * 1024
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
        "adapter_input_invalid",
        "adapter_input_unsafe",
        "adapter_source_changed",
    }
)


class AdapterSourceChildError(RuntimeError):
    """Fixed child-local error boundary for contract and descriptor failures."""

    def __init__(self, code: str) -> None:
        self.code = code if code in CHILD_ERROR_CODES else "adapter_input_invalid"
        super().__init__(self.code)


class _PreadReader:
    """Binary reader whose offset is private and whose reads are descriptor-bound."""

    def __init__(self, descriptor: int, limit: int) -> None:
        self.descriptor = descriptor
        self.limit = limit
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if type(size) is not int or size < 0:
            raise ValueError("invalid read size")
        if self.offset + size > self.limit:
            raise ValueError("read outside header")
        result = os.pread(self.descriptor, size, self.offset)
        if len(result) != size:
            raise ValueError("short read")
        self.offset += len(result)
        return result


def validate_descriptors(request: dict[str, object]) -> dict[str, object]:
    """Validate exactly the bounded config and safetensors header."""

    expected = {
        "config_fd",
        "model_fd",
        "config_size",
        "model_size",
        "source_contract_version",
        "config_contract_version",
        "tensor_contract_version",
    }
    if set(request) != expected:
        raise AdapterSourceChildError("adapter_config_invalid")
    descriptors = (request["config_fd"], request["model_fd"])
    sizes = (request["config_size"], request["model_size"])
    if any(type(value) is not int or value < 0 for value in descriptors):
        raise AdapterSourceChildError("adapter_input_invalid")
    if any(type(value) is not int or value <= 0 for value in sizes):
        raise AdapterSourceChildError("adapter_input_invalid")
    if request["source_contract_version"] != ADAPTER_SOURCE_CONTRACT_VERSION:
        raise AdapterSourceChildError("adapter_config_unsupported")
    if request["config_contract_version"] != ADAPTER_CONFIG_CONTRACT_VERSION:
        raise AdapterSourceChildError("adapter_config_unsupported")
    if request["tensor_contract_version"] != ADAPTER_TENSOR_CONTRACT_VERSION:
        raise AdapterSourceChildError("adapter_config_unsupported")
    config_fd, model_fd = descriptors
    config_size, model_size = sizes
    config_metadata = os.fstat(config_fd)
    model_metadata = os.fstat(model_fd)
    if (
        not stat.S_ISREG(config_metadata.st_mode)
        or not stat.S_ISREG(model_metadata.st_mode)
        or config_metadata.st_size != config_size
        or model_metadata.st_size != model_size
        or config_metadata.st_nlink != 1
        or model_metadata.st_nlink != 1
        or config_metadata.st_uid != os.geteuid()
        or model_metadata.st_uid != os.geteuid()
        or config_metadata.st_mode & 0o077
        or model_metadata.st_mode & 0o077
    ):
        raise AdapterSourceChildError("adapter_input_unsafe")
    config_raw = _read_bounded(config_fd, config_size)
    try:
        parse_external_adapter_config(config_raw)
        model_summary = validate_safetensors_metadata(
            _PreadReader(model_fd, min(model_size, 8 + 1_048_576)), model_size
        )
    except AdapterContractError as error:
        raise AdapterSourceChildError(error.code) from error
    if os.fstat(config_fd).st_size != config_size or os.fstat(model_fd).st_size != model_size:
        raise AdapterSourceChildError("adapter_source_changed")
    return {
        "source_contract_version": ADAPTER_SOURCE_CONTRACT_VERSION,
        "config_contract_version": ADAPTER_CONFIG_CONTRACT_VERSION,
        "tensor_contract_version": ADAPTER_TENSOR_CONTRACT_VERSION,
        "base_model_id": BASE_MODEL_ID,
        "peft_version": PEFT_FORMAT_REFERENCE_VERSION,
        "safetensors_format": SAFETENSORS_FORMAT_REFERENCE_VERSION,
        "tensor_dtype": model_summary.dtype,
        "tensor_count": model_summary.tensor_count,
        "tensor_element_count": model_summary.total_tensor_elements,
        "tensor_payload_byte_size": model_summary.total_tensor_bytes,
        "base_model_display_id": BASE_MODEL_ID,
    }


def _read_bounded(descriptor: int, size: int) -> bytes:
    if size <= 0:
        raise AdapterSourceChildError("adapter_input_invalid")
    blocks: list[bytes] = []
    offset = 0
    while offset < size:
        block = os.pread(descriptor, min(64 * 1024, size - offset), offset)
        if not block:
            raise AdapterSourceChildError("adapter_source_changed")
        blocks.append(block)
        offset += len(block)
    return b"".join(blocks)


def _read_frame() -> dict[str, object]:
    prefix = _read_stdin(4)
    size = struct.unpack("!I", prefix)[0]
    if not 1 <= size <= MAX_REQUEST_FRAME_BYTES:
        raise ValueError("request too large")
    raw = _read_stdin(size)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != {"operation", "request"}:
        raise ValueError("request schema")
    if value["operation"] != "validate_adapter_source" or not isinstance(value["request"], dict):
        raise ValueError("request operation")
    return value["request"]


def _read_stdin(size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        block = sys.stdin.buffer.read(size - len(data))
        if not block:
            raise ValueError("short request")
        data.extend(block)
    return bytes(data)


def _write_frame(value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(raw) <= MAX_RESPONSE_FRAME_BYTES:
        raw = b'{"status":"error","code":"adapter_input_invalid"}'
    sys.stdout.buffer.write(struct.pack("!I", len(raw)) + raw)
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        result = validate_descriptors(_read_frame())
    except AdapterSourceChildError as error:
        _write_frame({"status": "error", "code": error.code})
        return 1
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        _write_frame({"status": "error", "code": "adapter_input_invalid"})
        return 1
    _write_frame({"status": "ok", "result": result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_REQUEST_FRAME_BYTES",
    "MAX_RESPONSE_FRAME_BYTES",
    "CHILD_ERROR_CODES",
    "AdapterSourceChildError",
    "validate_descriptors",
    "main",
]
