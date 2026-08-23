"""One-request Phase 14.2 runtime orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from .config import TrainingConfigError, read_input_file, rematerialize_execution_config
from .contract import (
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    DATASET_INFO_VERSION,
    EXECUTION_PROFILE_VERSION,
    LLAMAFACTORY_VERSION,
    MAX_RECORD_CONTENT_BYTES,
    RUNTIME_CONTRACT_VERSION,
    SAFE_ERROR_CODES,
    TRAINING_CONFIG_VERSION,
    TRAINING_JOB_CONTRACT_VERSION,
    TRAINING_JOB_MANIFEST_VERSION,
    canonical_json_bytes,
    request_mapping,
)
from .hardware import HardwarePreflightError, preflight_hardware
from .model_store import ModelStoreError, validate_model_directory
from .output_stage import inspect_output_stage
from .supervisor import TrainingProcessSupervisor


class RuntimeSettingsError(RuntimeError):
    def __init__(self, code: str = "runtime_environment_invalid") -> None:
        self.code = code
        super().__init__(code)


class TrainingRuntime:
    def __init__(self) -> None:
        self.token = _required_token()
        self.model_path = Path(
            os.getenv(
                "DEPTSLM_TRAINING_MODEL_PATH",
                f"/runtime/deptslm/model_cache/qwen3-0.6b-{BASE_MODEL_REVISION}",
            )
        )
        self.dependency_lock_sha256 = os.getenv("DEPTSLM_TRAINING_DEPENDENCY_LOCK_SHA256", "")
        self.environment_profile_id = os.getenv(
            "DEPTSLM_TRAINING_ENVIRONMENT_PROFILE_ID",
            "deptslm-phase14-training-runtime-linux-x86_64-cuda126-v1",
        )
        self.environment_fingerprint, environment_contract = _environment_contract()
        if len(self.dependency_lock_sha256) != 64:
            raise RuntimeSettingsError("runtime_dependency_mismatch")
        if (
            environment_contract.get("environment_profile_id") != self.environment_profile_id
            or environment_contract.get("dependency_lock_sha256") != self.dependency_lock_sha256
        ):
            raise RuntimeSettingsError("runtime_dependency_mismatch")
        try:
            lock_digest = hashlib.sha256(
                Path("/opt/llamafactory/requirements.lock").read_bytes()
            ).hexdigest()
        except OSError as error:
            raise RuntimeSettingsError("runtime_dependency_mismatch") from error
        if lock_digest != self.dependency_lock_sha256:
            raise RuntimeSettingsError("runtime_dependency_mismatch")
        if os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN"):
            raise RuntimeSettingsError("runtime_environment_invalid")
        if os.getenv("DATABASE_URL") or os.getenv("DEPTSLM_DATA_DIR"):
            raise RuntimeSettingsError("runtime_environment_invalid")

    def handle(
        self,
        request_value: dict[str, object],
        fds: tuple[int, int, int, int],
        disconnected: Callable[[], bool],
    ) -> dict[str, object]:
        input_fd, scratch_fd, logs_fd, output_fd = fds
        try:
            request = request_mapping(request_value)
            if request["expected_environment_fingerprint"] != self.environment_fingerprint:
                raise RuntimeSettingsError("runtime_dependency_mismatch")
            if request["dependency_lock_sha256"] != self.dependency_lock_sha256:
                raise RuntimeSettingsError("runtime_dependency_mismatch")
            if (
                request["base_model_id"] != BASE_MODEL_ID
                or request["base_model_revision"] != BASE_MODEL_REVISION
            ):
                raise RuntimeSettingsError("runtime_model_unavailable")
            manifest = _parse_manifest(read_input_file(input_fd, "manifest.json"), request)
            _verify_manifest_files(input_fd, manifest)
            config_bytes = read_input_file(input_fd, "training.yaml")
            qlora = request["profile_id"] == "phase11-qwen3-0.6b-qlora-nf4-v1"
            hardware = preflight_hardware(qlora=qlora)
            validate_model_directory(self.model_path)
            rematerialized = rematerialize_execution_config(
                config_bytes,
                profile_id=str(request["profile_id"]),
                input_fd=input_fd,
                scratch_fd=scratch_fd,
                logs_fd=logs_fd,
                output_stage_fd=output_fd,
                model_path=str(self.model_path),
            )
            try:
                if disconnected():
                    return _failure(request, "runtime_disconnected")
                supervisor = TrainingProcessSupervisor()
                result = supervisor.run(
                    config_fd=rematerialized.config_fd,
                    input_fd=input_fd,
                    scratch_fd=scratch_fd,
                    logs_fd=logs_fd,
                    output_stage_fd=output_fd,
                    environment=_child_environment(self.model_path),
                    should_stop=disconnected,
                )
            finally:
                os.close(rematerialized.config_fd)
            if result.classification != "execution_succeeded":
                return _failure(request, result.error_code or "child_failed")
            try:
                output = inspect_output_stage(output_fd)
            except ValueError as error:
                return _failure(
                    request,
                    str(error) if str(error) in SAFE_ERROR_CODES else "output_invalid",
                )
            if output.file_count < 1 or output.total_bytes < 1:
                return _failure(request, "output_invalid")
            runtime_fp = _real_runtime_fingerprint(request, hardware, output.fingerprint)
            return {
                **_identity(request),
                "runtime_kind": "real",
                "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
                "dependency_lock_sha256": self.dependency_lock_sha256,
                "environment_profile_id": self.environment_profile_id,
                "environment_fingerprint": self.environment_fingerprint,
                "hardware_profile_id": hardware.hardware_profile_id,
                "hardware_fingerprint": hardware.hardware_fingerprint,
                "runtime_fingerprint": runtime_fp,
                "classification": "execution_succeeded",
                "error_code": None,
                "output_stage_fingerprint": output.fingerprint,
                "output_file_count": output.file_count,
                "output_total_bytes": output.total_bytes,
            }
        except (
            RuntimeSettingsError,
            HardwarePreflightError,
            ModelStoreError,
            TrainingConfigError,
        ) as error:
            code = getattr(error, "code", "runtime_environment_invalid")
            return _failure(
                request_value,
                code if code in SAFE_ERROR_CODES else "runtime_environment_invalid",
            )
        except ValueError as error:
            code = str(error)
            return _failure(
                request_value,
                code if code in SAFE_ERROR_CODES else "runtime_protocol_invalid",
            )
        except (OSError, TypeError, json.JSONDecodeError):
            return _failure(request_value, "runtime_protocol_invalid")


def _required_token() -> str:
    token = os.getenv("DEPTSLM_TRAINING_RUNTIME_TOKEN", "")
    if token != token.strip() or len(token) < 32 or any(character.isspace() for character in token):
        raise RuntimeSettingsError("runtime_auth_failed")
    return token


def _environment_contract() -> tuple[str, dict[str, object]]:
    path = Path(
        os.getenv(
            "DEPTSLM_TRAINING_ENVIRONMENT_CONTRACT",
            "/opt/llamafactory/environment.json",
        )
    )
    try:
        data = path.read_bytes()
        value = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeSettingsError("runtime_dependency_mismatch") from error
    expected = {
        "environment_profile_id": "deptslm-phase14-training-runtime-linux-x86_64-cuda126-v1",
        "python_version": "3.12",
        "llamafactory_version": "0.9.5",
        "supported_os": "Linux",
        "supported_architecture": "x86_64",
        "expected_cuda_runtime_family": "12.6",
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "base_image": (
            "nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04@"
            "sha256:8aef630a54bc5c5146ae5ce68e6af5caa3df0fb690bb91544175c91f307e4356"
        ),
    }
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        raise RuntimeSettingsError("runtime_dependency_mismatch")
    if not isinstance(value.get("dependency_lock_sha256"), str) or len(value) != 9:
        raise RuntimeSettingsError("runtime_dependency_mismatch")
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest(), value


def _child_environment(model_path: Path) -> dict[str, str]:
    return {
        "PATH": "/opt/llamafactory/bin:/usr/local/bin:/usr/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "CUDA_VISIBLE_DEVICES": "0",
        "NVIDIA_VISIBLE_DEVICES": "0",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        "DEPTSLM_TRAINING_RUNTIME_PROFILE": "phase14-training-runtime-v1",
        "DEPTSLM_TRAINING_MODEL_PATH": str(model_path),
    }


def _parse_manifest(raw: bytes, request: dict[str, object]) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrainingConfigError("training_config_invalid") from error
    expected_keys = {
        "artifact_contract_version",
        "manifest_contract_version",
        "configuration_contract_version",
        "dataset_info_contract_version",
        "execution_profile_contract_version",
        "department_id",
        "training_job_id",
        "publication_attempt_id",
        "execution_scope_id",
        "attempt_number",
        "code_revision",
        "dataset_build_id",
        "dataset_build_version",
        "dataset_artifact_contract_version",
        "dataset_example_contract_version",
        "dataset_normalization_version",
        "dataset_split_version",
        "dataset_manifest_sha256",
        "train_example_count",
        "validation_example_count",
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "llamafactory_version",
        "profile_id",
        "maximum_record_content_bytes",
        "tokenizer_preflight_required",
        "dataset_rights_attested",
        "evaluation_contamination_reviewed",
        "files",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise TrainingConfigError()
    for key, request_key in (
        ("department_id", "department_id"),
        ("training_job_id", "training_job_id"),
        ("publication_attempt_id", "publication_attempt_id"),
        ("profile_id", "profile_id"),
        ("base_model_id", "base_model_id"),
        ("base_model_revision", "base_model_revision"),
        ("llamafactory_version", None),
    ):
        expected = LLAMAFACTORY_VERSION if key == "llamafactory_version" else request[request_key]
        if value.get(key) != expected:
            raise TrainingConfigError("training_config_invalid")
    expected_static = {
        "artifact_contract_version": TRAINING_JOB_CONTRACT_VERSION,
        "manifest_contract_version": TRAINING_JOB_MANIFEST_VERSION,
        "configuration_contract_version": TRAINING_CONFIG_VERSION,
        "dataset_info_contract_version": DATASET_INFO_VERSION,
        "execution_profile_contract_version": EXECUTION_PROFILE_VERSION,
        "base_model_license": BASE_MODEL_LICENSE,
        "maximum_record_content_bytes": MAX_RECORD_CONTENT_BYTES,
        "tokenizer_preflight_required": True,
    }
    if any(value.get(key) != expected for key, expected in expected_static.items()):
        raise TrainingConfigError("training_config_invalid")
    if (
        value.get("dataset_rights_attested") is not True
        or value.get("evaluation_contamination_reviewed") is not True
    ):
        raise TrainingConfigError("training_config_invalid")
    if value.get("code_revision") != request.get("execution_code_revision"):
        raise TrainingConfigError("training_config_invalid")
    for key in (
        "department_id",
        "training_job_id",
        "publication_attempt_id",
        "execution_scope_id",
        "dataset_build_id",
    ):
        if not _valid_uuid(value.get(key)):
            raise TrainingConfigError("training_config_invalid")
    if (
        not isinstance(value.get("code_revision"), str)
        or re.fullmatch(r"[0-9a-f]{40}", value["code_revision"]) is None
        or type(value.get("attempt_number")) is not int
        or value["attempt_number"] < 1
        or type(value.get("dataset_build_version")) is not int
        or value["dataset_build_version"] < 1
        or value.get("dataset_artifact_contract_version") != "phase10-sft-dataset-v1"
        or value.get("dataset_example_contract_version") != "phase10-sft-example-v1"
        or value.get("dataset_normalization_version") != "phase10-sft-normalization-v1"
        or value.get("dataset_split_version") != "phase10-sft-group-split-v1"
    ):
        raise TrainingConfigError("training_config_invalid")
    if (
        not isinstance(value.get("dataset_manifest_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["dataset_manifest_sha256"]) is None
        or type(value.get("train_example_count")) is not int
        or value["train_example_count"] < 1
        or type(value.get("validation_example_count")) is not int
        or value["validation_example_count"] < 1
    ):
        raise TrainingConfigError("training_config_invalid")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != {
        "training.yaml",
        "dataset_info.json",
        "train.jsonl",
        "validation.jsonl",
    }:
        raise TrainingConfigError()
    for descriptor in files.values():
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"sha256", "byte_size"}
            or not isinstance(descriptor["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"]) is None
            or type(descriptor["byte_size"]) is not int
            or descriptor["byte_size"] <= 0
        ):
            raise TrainingConfigError()
    return value


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.int != 0


def _verify_manifest_files(input_fd: int, manifest: dict[str, object]) -> None:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise TrainingConfigError("training_config_invalid")
    for name, descriptor in files.items():
        if not isinstance(name, str) or not isinstance(descriptor, dict):
            raise TrainingConfigError("training_config_invalid")
        expected_size = descriptor["byte_size"]
        expected_sha = descriptor["sha256"]
        try:
            metadata = os.stat(name, dir_fd=input_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != expected_size
            ):
                raise TrainingConfigError("training_config_invalid")
            descriptor_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=input_fd)
        except (OSError, TrainingConfigError) as error:
            if isinstance(error, TrainingConfigError):
                raise
            raise TrainingConfigError("training_config_invalid") from error
        digest = hashlib.sha256()
        total = 0
        try:
            actual = os.fstat(descriptor_fd)
            if (
                actual.st_dev != metadata.st_dev
                or actual.st_ino != metadata.st_ino
                or actual.st_nlink != 1
            ):
                raise TrainingConfigError("training_config_invalid")
            while block := os.read(descriptor_fd, 1024 * 1024):
                total += len(block)
                digest.update(block)
        except OSError as error:
            raise TrainingConfigError("training_config_invalid") from error
        finally:
            os.close(descriptor_fd)
        if total != expected_size or digest.hexdigest() != expected_sha:
            raise TrainingConfigError("training_config_invalid")


def _identity(request: dict[str, object]) -> dict[str, object]:
    safe_request = request if isinstance(request, dict) else {}
    return {
        "department_id": safe_request.get("department_id", ""),
        "execution_id": safe_request.get("execution_id", ""),
        "attempt_id": safe_request.get("attempt_id", ""),
        "training_job_id": safe_request.get("training_job_id", ""),
        "authority_fingerprint": safe_request.get("authority_fingerprint", ""),
        "input_snapshot_fingerprint": safe_request.get("input_snapshot_fingerprint", ""),
    }


def _failure(request: dict[str, object], code: str) -> dict[str, object]:
    return {
        **_identity(request),
        "runtime_kind": "real",
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "dependency_lock_sha256": request.get("dependency_lock_sha256"),
        "environment_profile_id": request.get("environment_profile_id"),
        "environment_fingerprint": request.get("expected_environment_fingerprint"),
        "hardware_profile_id": None,
        "hardware_fingerprint": None,
        "runtime_fingerprint": "0" * 64,
        "classification": "execution_cancelled"
        if code in {"cancelled", "claim_lost", "runtime_disconnected"}
        else "execution_failed",
        "error_code": code if code in SAFE_ERROR_CODES else "runtime_protocol_invalid",
        "output_stage_fingerprint": None,
        "output_file_count": None,
        "output_total_bytes": None,
    }


def _real_runtime_fingerprint(request: dict[str, object], hardware: object, output: str) -> str:
    value = {
        "runtime_fingerprint_version": "phase14-real-runtime-fingerprint-v1",
        "execution_id": request["execution_id"],
        "attempt_id": request["attempt_id"],
        "authority_fingerprint": request["authority_fingerprint"],
        "input_snapshot_fingerprint": request["input_snapshot_fingerprint"],
        "runtime_contract_version": RUNTIME_CONTRACT_VERSION,
        "llamafactory_version": LLAMAFACTORY_VERSION,
        "dependency_lock_sha256": request["dependency_lock_sha256"],
        "environment_profile_id": request["environment_profile_id"],
        "environment_fingerprint": request["expected_environment_fingerprint"],
        "hardware_profile_id": hardware.hardware_profile_id,
        "hardware_fingerprint": hardware.hardware_fingerprint,
        "base_model_id": request["base_model_id"],
        "base_model_revision": request["base_model_revision"],
        "profile_id": request["profile_id"],
        "execution_code_revision": request.get("execution_code_revision", ""),
        "output_stage_fingerprint": output,
    }
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
