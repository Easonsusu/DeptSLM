"""Dependency-free, closed Phase 14.2 wire and semantic contracts."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import UUID

RUNTIME_CONTRACT_VERSION = "phase14-training-runtime-v1"
LLAMAFACTORY_VERSION = "0.9.5"
BASE_MODEL_ID = "Qwen/Qwen3-0.6B"
BASE_MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"
BASE_MODEL_LICENSE = "Apache-2.0"
TRAINING_JOB_CONTRACT_VERSION = "phase11-training-job-v1"
TRAINING_JOB_MANIFEST_VERSION = "phase11-training-job-manifest-v1"
TRAINING_CONFIG_VERSION = "phase11-training-config-v1"
DATASET_INFO_VERSION = "phase11-dataset-info-v1"
EXECUTION_PROFILE_VERSION = "phase11-execution-profile-v1"
MAX_RECORD_CONTENT_BYTES = 7680
TRAINING_PROFILES = frozenset({"phase11-qwen3-0.6b-lora-v1", "phase11-qwen3-0.6b-qlora-nf4-v1"})
OFFLINE_VARIABLES = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
}
MAX_IPC_FRAME_BYTES = 65_536
MAX_HANDLES = 4
HANDLE_ROLES = ("input", "scratch", "logs", "output_stage")
BITSANDBYTES_VERSION = "0.50.1"
MAX_RUNTIME_ATTEMPTS = 1
HANDSHAKE_TIMEOUT_SECONDS = 120
STARTUP_TIMEOUT_SECONDS = 600
TRAINING_WALL_SECONDS = 12 * 60 * 60
HEARTBEAT_INTERVAL_SECONDS = 30
TERM_GRACE_SECONDS = 20
KILL_REAP_SECONDS = 10
MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_SCRATCH_BYTES = 8 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_OUTPUT_FILES = 4096
MAX_RUNTIME_PIDS = 512
MAX_RUNTIME_TMPFS_BYTES = 256 * 1024 * 1024
SAFE_ERROR_CODES = frozenset(
    {
        "runtime_environment_invalid",
        "runtime_hardware_unsupported",
        "runtime_model_unavailable",
        "runtime_dependency_mismatch",
        "runtime_auth_failed",
        "runtime_busy",
        "training_config_invalid",
        "child_start_failed",
        "child_failed",
        "child_timeout",
        "runtime_disconnected",
        "output_limit_exceeded",
        "output_invalid",
        "runtime_cleanup_failed",
        "cancelled",
        "worker_shutdown",
        "worker_timeout",
        "claim_lost",
    }
)

COMMON_SEMANTIC_VALUES: tuple[tuple[str, object], ...] = (
    ("stage", "sft"),
    ("do_train", True),
    ("finetuning_type", "lora"),
    ("lora_target", "all"),
    ("lora_rank", 16),
    ("lora_alpha", 32),
    ("lora_dropout", 0.05),
    ("template", "qwen3_nothink"),
    ("enable_thinking", False),
    ("cutoff_len", 8192),
    ("packing", False),
    ("neat_packing", False),
    ("preprocessing_num_workers", 1),
    ("dataloader_num_workers", 0),
    ("per_device_train_batch_size", 1),
    ("per_device_eval_batch_size", 1),
    ("gradient_accumulation_steps", 8),
    ("learning_rate", 0.0001),
    ("num_train_epochs", 3.0),
    ("lr_scheduler_type", "cosine"),
    ("warmup_ratio", 0.03),
    ("weight_decay", 0.0),
    ("max_grad_norm", 1.0),
    ("bf16", True),
    ("fp16", False),
    ("seed", 42),
    ("data_seed", 42),
    ("logging_steps", 10),
    ("save_strategy", "epoch"),
    ("eval_strategy", "epoch"),
    ("report_to", "none"),
    ("plot_loss", False),
    ("overwrite_cache", False),
    ("overwrite_output_dir", False),
    ("trust_remote_code", False),
    ("flash_attn", "disabled"),
    ("use_unsloth", False),
    ("enable_liger_kernel", False),
)
SEMANTIC_PROFILES = {
    "phase11-qwen3-0.6b-lora-v1": dict(COMMON_SEMANTIC_VALUES),
    "phase11-qwen3-0.6b-qlora-nf4-v1": dict(
        COMMON_SEMANTIC_VALUES,
        quantization_bit=4,
        quantization_method="bnb",
        quantization_type="nf4",
        double_quantization=True,
    ),
}
SEMANTIC_KEYS = frozenset().union(*(profile.keys() for profile in SEMANTIC_PROFILES.values())) | {
    "model_name_or_path",
    "dataset_dir",
    "dataset",
    "eval_dataset",
    "output_dir",
}
SUBSTITUTION_KEYS = frozenset(
    {
        "model_name_or_path",
        "dataset_dir",
        "output_dir",
        "cache_dir",
        "logging_dir",
        "report_to",
    }
)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_hardware_fingerprint(value: dict[str, object]) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def request_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact serializable request key set without content."""

    expected = {
        "runtime_contract_version",
        "department_id",
        "execution_id",
        "attempt_id",
        "training_job_id",
        "publication_attempt_id",
        "authority_fingerprint",
        "input_snapshot_fingerprint",
        "profile_id",
        "base_model_id",
        "base_model_revision",
        "attempt_namespace",
        "dependency_lock_sha256",
        "environment_profile_id",
        "expected_environment_fingerprint",
        "execution_code_revision",
    }
    if set(value) != expected:
        raise ValueError("runtime_protocol_invalid")
    if value["runtime_contract_version"] != RUNTIME_CONTRACT_VERSION:
        raise ValueError("runtime_protocol_invalid")
    if (
        value["base_model_id"] != BASE_MODEL_ID
        or value["base_model_revision"] != BASE_MODEL_REVISION
    ):
        raise ValueError("runtime_model_unavailable")
    if value["profile_id"] not in TRAINING_PROFILES:
        raise ValueError("training_config_invalid")
    for key in (
        "department_id",
        "execution_id",
        "attempt_id",
        "training_job_id",
        "publication_attempt_id",
        "attempt_namespace",
    ):
        if (
            not isinstance(value[key], str)
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                value[key],
            )
            is None
        ):
            raise ValueError("runtime_protocol_invalid")
        if UUID(value[key]).int == 0:
            raise ValueError("runtime_protocol_invalid")
    for key in (
        "authority_fingerprint",
        "input_snapshot_fingerprint",
        "dependency_lock_sha256",
    ):
        if not isinstance(value[key], str) or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise ValueError("runtime_protocol_invalid")
    if (
        not isinstance(value["execution_code_revision"], str)
        or re.fullmatch(r"[0-9a-f]{40}", value["execution_code_revision"]) is None
    ):
        raise ValueError("runtime_protocol_invalid")
    if (
        not isinstance(value["environment_profile_id"], str)
        or not value["environment_profile_id"]
        or "\x00" in value["environment_profile_id"]
        or not isinstance(value["expected_environment_fingerprint"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["expected_environment_fingerprint"]) is None
    ):
        raise ValueError("runtime_protocol_invalid")
    return value
