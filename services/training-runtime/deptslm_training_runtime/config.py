"""Closed Phase 11 semantic validation and server-owned config rematerialization."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass

import yaml

from .contract import (
    SEMANTIC_PROFILES,
    SUBSTITUTION_KEYS,
)

MAX_CONFIG_BYTES = 256 * 1024


class TrainingConfigError(RuntimeError):
    def __init__(self, code: str = "training_config_invalid") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RematerializedConfig:
    config_fd: int
    config_sha256: str
    values: dict[str, object]


def read_input_file(input_fd: int, name: str, *, maximum: int = MAX_CONFIG_BYTES) -> bytes:
    if name not in {
        "manifest.json",
        "training.yaml",
        "dataset_info.json",
        "train.jsonl",
        "validation.jsonl",
    }:
        raise TrainingConfigError()
    try:
        metadata = os.stat(name, dir_fd=input_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise TrainingConfigError()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=input_fd)
    except (OSError, TrainingConfigError) as error:
        if isinstance(error, TrainingConfigError):
            raise
        raise TrainingConfigError(
            "runtime_model_unavailable" if name == "manifest.json" else "training_config_invalid"
        ) from error
    try:
        actual = os.fstat(descriptor)
        if (
            actual.st_dev != metadata.st_dev
            or actual.st_ino != metadata.st_ino
            or actual.st_size != metadata.st_size
        ):
            raise TrainingConfigError()
        data = bytearray()
        while block := os.read(descriptor, min(64 * 1024, maximum + 1 - len(data))):
            data.extend(block)
            if len(data) > maximum:
                raise TrainingConfigError()
        return bytes(data)
    except OSError as error:
        raise TrainingConfigError() from error
    finally:
        os.close(descriptor)


def parse_training_yaml(raw: bytes, profile_id: str) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CONFIG_BYTES:
        raise TrainingConfigError()
    expected_profile = SEMANTIC_PROFILES.get(profile_id)
    if expected_profile is None:
        raise TrainingConfigError()
    try:
        value = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise TrainingConfigError() from error
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TrainingConfigError()
    expected_keys = set(expected_profile) | {
        "model_name_or_path",
        "dataset_dir",
        "dataset",
        "eval_dataset",
        "output_dir",
    }
    if set(value) != expected_keys:
        raise TrainingConfigError()
    for key, expected in expected_profile.items():
        if type(value.get(key)) is not type(expected) or value.get(key) != expected:
            raise TrainingConfigError()
    if value.get("dataset") != "deptslm_train" or value.get("eval_dataset") != "deptslm_validation":
        raise TrainingConfigError()
    # Phase 11's portable bundle contains only the semantic path fields.  The
    # runtime-owned cache and logging descriptors are deliberately absent and
    # are added only during rematerialization.
    for key in (SUBSTITUTION_KEYS - {"report_to"}).intersection(value):
        if not isinstance(value.get(key), str) or not value[key] or "\x00" in value[key]:
            raise TrainingConfigError()
    # The Phase 11 bundle's model path is evidence only.  The runtime never
    # executes it and always substitutes its fixed read-only model mount.
    if value.get("report_to") != "none":
        raise TrainingConfigError()
    return dict(value)


def rematerialize_execution_config(
    raw: bytes,
    *,
    profile_id: str,
    input_fd: int,
    scratch_fd: int,
    logs_fd: int,
    output_stage_fd: int,
    model_path: str,
) -> RematerializedConfig:
    parsed = parse_training_yaml(raw, profile_id)
    if not model_path.startswith("/runtime/deptslm/model_cache/") or ".." in model_path:
        raise TrainingConfigError("runtime_model_unavailable")
    values = {key: value for key, value in parsed.items() if key not in SUBSTITUTION_KEYS}
    values.update(
        {
            "model_name_or_path": model_path,
            "dataset_dir": f"/proc/self/fd/{input_fd}",
            "output_dir": f"/proc/self/fd/{output_stage_fd}",
            "cache_dir": f"/proc/self/fd/{scratch_fd}",
            "logging_dir": f"/proc/self/fd/{logs_fd}",
            "report_to": "none",
        }
    )
    # Keep the generated mapping flat and closed; no caller value can inject
    # argv, scripts, callbacks, deepspeed, hub reporting, or resume state.
    try:
        encoded = yaml.safe_dump(values, sort_keys=True, default_flow_style=False).encode("utf-8")
        descriptor = os.open(
            "execution.yaml",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=scratch_fd,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("config write failed")
                view = view[written:]
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except OSError:
            os.close(descriptor)
            raise
    except OSError as error:
        raise TrainingConfigError() from error
    return RematerializedConfig(descriptor, hashlib.sha256(encoded).hexdigest(), values)
