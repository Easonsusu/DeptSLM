"""Model-free Phase 12.1B validation-child and IPC boundary tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.adapter_contract import (
    EXPECTED_TENSOR_NAMES,
    EXPECTED_TENSOR_SHAPES,
    canonical_adapter_config_bytes,
)
from app.adapter_source_artifacts import AdapterSourceArtifactError
from app.adapter_source_child import AdapterSourceChildError, validate_descriptors
from app.adapter_source_supervision import _validate_success_result, run_adapter_source_validation


def _adapter_files(tmp_path: Path) -> tuple[Path, Path, int]:
    config = tmp_path / "adapter_config.json"
    model = tmp_path / "adapter_model.safetensors"
    config.write_bytes(canonical_adapter_config_bytes())
    offset = 0
    header: dict[str, object] = {"__metadata__": {"format": "pt"}}
    for name in EXPECTED_TENSOR_NAMES:
        shape = EXPECTED_TENSOR_SHAPES[name]
        size = shape[0] * shape[1] * 2
        header[name] = {
            "dtype": "F16",
            "shape": list(shape),
            "data_offsets": [offset, offset + size],
        }
        offset += size
    raw_header = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    model.write_bytes(len(raw_header).to_bytes(8, "little") + raw_header)
    with model.open("ab") as handle:
        handle.truncate(8 + len(raw_header) + offset)
    os.chmod(config, 0o600)
    os.chmod(model, 0o600)
    return config, model, offset


def _request(config_fd: int, model_fd: int, config_size: int, model_size: int) -> dict[str, object]:
    return {
        "config_fd": config_fd,
        "model_fd": model_fd,
        "config_size": config_size,
        "model_size": model_size,
        "source_contract_version": "phase12-adapter-source-v1",
        "config_contract_version": "phase12-adapter-config-v1",
        "tensor_contract_version": "phase12-adapter-tensors-v1",
    }


def test_child_reads_only_config_and_safetensors_header(tmp_path: Path, monkeypatch) -> None:
    config, model, _payload_size = _adapter_files(tmp_path)
    config_fd = os.open(config, os.O_RDONLY)
    model_fd = os.open(model, os.O_RDONLY)
    model_reads: list[tuple[int, int]] = []
    original_pread = os.pread

    def recording_pread(fd: int, size: int, offset: int) -> bytes:
        if fd == model_fd:
            model_reads.append((size, offset))
        return original_pread(fd, size, offset)

    monkeypatch.setattr("app.adapter_source_child.os.pread", recording_pread)
    try:
        result = validate_descriptors(
            _request(config_fd, model_fd, config.stat().st_size, model.stat().st_size)
        )
    finally:
        os.close(config_fd)
        os.close(model_fd)
    assert result["tensor_count"] == 392
    assert result["tensor_dtype"] == "F16"
    assert all(offset + size <= 8 + 1_048_576 for size, offset in model_reads)
    assert result.keys() <= {
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


def test_supervised_child_returns_content_free_summary(tmp_path: Path) -> None:
    config, model, _payload_size = _adapter_files(tmp_path)
    config_fd = os.open(config, os.O_RDONLY)
    model_fd = os.open(model, os.O_RDONLY)
    try:
        result = run_adapter_source_validation(
            config_fd=config_fd,
            model_fd=model_fd,
            config_size=config.stat().st_size,
            model_size=model.stat().st_size,
        )
    finally:
        os.close(config_fd)
        os.close(model_fd)
    assert result["base_model_id"] == "Qwen/Qwen3-0.6B"
    assert result["tensor_payload_byte_size"] == 20_185_088
    assert "adapter_config.json" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)


def test_child_preserves_descriptor_error_codes(tmp_path: Path) -> None:
    config = tmp_path / "adapter_config.json"
    model = tmp_path / "adapter_model.safetensors"
    config.write_bytes(b"{}")
    model.write_bytes(b"not-a-safetensors")
    os.chmod(config, 0o600)
    os.chmod(model, 0o600)
    config_fd = os.open(config, os.O_RDONLY)
    model_fd = os.open(model, os.O_RDONLY)
    try:
        with pytest.raises(AdapterSourceChildError) as error:
            validate_descriptors(
                _request(config_fd, model_fd, config.stat().st_size, model.stat().st_size)
            )
    finally:
        os.close(config_fd)
        os.close(model_fd)
    assert error.value.code == "adapter_config_invalid"


def test_parent_accepts_only_the_exact_child_success_schema(tmp_path: Path) -> None:
    config, model, _payload_size = _adapter_files(tmp_path)
    config_fd = os.open(config, os.O_RDONLY)
    model_fd = os.open(model, os.O_RDONLY)
    try:
        result = run_adapter_source_validation(
            config_fd=config_fd,
            model_fd=model_fd,
            config_size=config.stat().st_size,
            model_size=model.stat().st_size,
        )
    finally:
        os.close(config_fd)
        os.close(model_fd)
    assert _validate_success_result(result) == result
    with pytest.raises(AdapterSourceArtifactError):
        _validate_success_result({**result, "extra": "rejected"})
    with pytest.raises(AdapterSourceArtifactError):
        _validate_success_result({**result, "tensor_count": True})
