"""Runtime-only tests; no model or CUDA download is required."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from deptslm_training_runtime.config import (  # noqa: E402
    parse_training_yaml,
    rematerialize_execution_config,
)
from deptslm_training_runtime.contract import SEMANTIC_PROFILES  # noqa: E402
from deptslm_training_runtime.hardware import (  # noqa: E402
    HardwarePreflightError,
    preflight_hardware,
)
from deptslm_training_runtime.output_stage import inspect_output_stage  # noqa: E402
from deptslm_training_runtime.supervisor import TrainingProcessSupervisor  # noqa: E402


def _yaml(profile_id: str) -> bytes:
    import yaml

    values = dict(SEMANTIC_PROFILES[profile_id])
    values.update(
        {
            "model_name_or_path": "/runtime/deptslm/model_cache/ignored-by-runtime",
            "dataset_dir": "/runtime/deptslm/training_datasets/foreign",
            "dataset": "deptslm_train",
            "eval_dataset": "deptslm_validation",
            "output_dir": "/runtime/deptslm/adapters/unregistered",
        }
    )
    return yaml.safe_dump(values, sort_keys=True).encode()


@pytest.mark.parametrize(
    "profile_id",
    ["phase11-qwen3-0.6b-lora-v1", "phase11-qwen3-0.6b-qlora-nf4-v1"],
)
def test_phase11_semantics_are_revalidated_and_rematerialized(
    tmp_path: Path, profile_id: str
) -> None:
    raw = _yaml(profile_id)
    parsed = parse_training_yaml(raw, profile_id)
    assert parsed["learning_rate"] == 0.0001
    input_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    scratch = tmp_path / "scratch"
    logs = tmp_path / "logs"
    output = tmp_path / "output"
    for path in (scratch, logs, output):
        path.mkdir(mode=0o700)
    scratch_fd, logs_fd, output_fd = (
        os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path in (scratch, logs, output)
    )
    try:
        rematerialized = rematerialize_execution_config(
            raw,
            profile_id=profile_id,
            input_fd=input_fd,
            scratch_fd=scratch_fd,
            logs_fd=logs_fd,
            output_stage_fd=output_fd,
            model_path="/runtime/deptslm/model_cache/qwen3-0.6b-c1899de289a04d12100db370d81485cdf75e47ca",
        )
        assert rematerialized.values["report_to"] == "none"
        assert rematerialized.values["dataset_dir"].startswith("/proc/self/fd/")
        assert rematerialized.values["model_name_or_path"].startswith(
            "/runtime/deptslm/model_cache/"
        )
        os.close(rematerialized.config_fd)
    finally:
        for descriptor in (input_fd, scratch_fd, logs_fd, output_fd):
            os.close(descriptor)


def test_output_stage_rejects_symlink_and_limits(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        (output / "candidate.bin").write_bytes(b"candidate")
        evidence = inspect_output_stage(descriptor, max_files=1)
        assert evidence.file_count == 1
        (output / "extra.bin").write_bytes(b"extra")
        with pytest.raises(ValueError, match="output_limit_exceeded"):
            inspect_output_stage(descriptor, max_files=1)
    finally:
        os.close(descriptor)


def test_hardware_preflight_fails_closed_without_supported_cuda() -> None:
    with pytest.raises(HardwarePreflightError, match="runtime_hardware_unsupported"):
        preflight_hardware()


def test_supervisor_rejects_non_fixed_executable(tmp_path: Path) -> None:
    descriptors = []
    for name in ("input", "scratch", "logs", "output"):
        path = tmp_path / name
        path.mkdir(mode=0o700)
        descriptors.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY))
    try:
        result = TrainingProcessSupervisor(executable="/bin/echo").run(
            config_fd=descriptors[0],
            input_fd=descriptors[0],
            scratch_fd=descriptors[1],
            logs_fd=descriptors[2],
            output_stage_fd=descriptors[3],
            environment={"PATH": "/usr/bin"},
            should_stop=lambda: False,
        )
        assert result.error_code == "child_start_failed"
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
