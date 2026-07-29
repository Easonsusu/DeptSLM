"""Fixed exec child for Phase 11 contentful bundle construction.

The child has no database, authentication, model, tokenizer, LlamaFactory, or
runtime imports.  It receives only inherited dataset/stage descriptors and
closed, content-free metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from uuid import UUID

from app.sft_artifacts import STAGE_MARKER
from app.training_job_domain import (
    TRAINING_JOB_FILES,
    TrainingJobContractError,
    build_bundle,
    canonical_json_bytes,
)

_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
_MAX_REQUEST = 64 * 1024
_MAX_RESPONSE = 32 * 1024
_MAX_FILE = 512 * 1024 * 1024
_PHASE10_FILES = frozenset({"manifest.json", "train.jsonl", "validation.jsonl", "provenance.jsonl"})


def main() -> int:
    try:
        os.environ.clear()
        os.environ.update(_ENVIRONMENT)
        request = _read_frame()
        if (
            set(request) != {"operation", "request"}
            or request.get("operation") != "build_training_job"
        ):
            raise TrainingJobContractError("training_job_publication_failed")
        payload = request.get("request")
        if not isinstance(payload, dict):
            raise TrainingJobContractError("training_job_publication_failed")
        result = _build(payload)
        _write_frame({"status": "ok", "result": result})
        return 0
    except BaseException as error:
        code = getattr(error, "code", "training_job_publication_failed")
        _write_frame(
            {
                "status": "error",
                "code": code if isinstance(code, str) else "training_job_publication_failed",
            }
        )
        return 1


def _build(request: dict[str, object]) -> dict[str, object]:
    expected = {
        "dataset_fd",
        "stage_fd",
        "department_id",
        "training_job_id",
        "dataset_build_id",
        "publication_attempt_id",
        "execution_scope_id",
        "attempt_number",
        "code_revision",
        "dataset_build_version",
        "dataset_manifest_sha256",
        "dataset_artifact_contract_version",
        "dataset_example_contract_version",
        "dataset_normalization_version",
        "dataset_split_version",
        "profile_id",
        "dataset_rights_attested",
        "evaluation_contamination_reviewed",
        "expected_train_sha256",
        "expected_train_byte_size",
        "expected_validation_sha256",
        "expected_validation_byte_size",
        "expected_provenance_sha256",
        "expected_provenance_byte_size",
    }
    if set(request) != expected:
        raise TrainingJobContractError("training_job_publication_failed")
    dataset_fd = _fd(request["dataset_fd"])
    stage_fd = _fd(request["stage_fd"])
    _directory(dataset_fd, writable=False)
    _directory(stage_fd, writable=True)
    if set(os.listdir(dataset_fd)) != _PHASE10_FILES or set(os.listdir(stage_fd)) != {STAGE_MARKER}:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    raw_manifest = _read(dataset_fd, "manifest.json", 256 * 1024)
    train = _read(dataset_fd, "train.jsonl", _MAX_FILE)
    validation = _read(dataset_fd, "validation.jsonl", _MAX_FILE)
    provenance = _read(dataset_fd, "provenance.jsonl", _MAX_FILE)
    if set(os.listdir(dataset_fd)) != _PHASE10_FILES:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    _validate_phase10_manifest(raw_manifest, request, train, validation, provenance)
    bundle = build_bundle(
        department_id=_uuid(request["department_id"]),
        training_job_id=_uuid(request["training_job_id"]),
        dataset_build_id=_uuid(request["dataset_build_id"]),
        publication_attempt_id=_uuid(request["publication_attempt_id"]),
        execution_scope_id=_uuid(request["execution_scope_id"]),
        attempt_number=_positive(request["attempt_number"]),
        code_revision=_revision(request["code_revision"]),
        dataset_build_version=_positive(request["dataset_build_version"]),
        dataset_manifest_sha256=_digest(raw_manifest),
        dataset_artifact_contract_version=_string(request["dataset_artifact_contract_version"]),
        dataset_example_contract_version=_string(request["dataset_example_contract_version"]),
        dataset_normalization_version=_string(request["dataset_normalization_version"]),
        dataset_split_version=_string(request["dataset_split_version"]),
        profile_id=_string(request["profile_id"]),
        dataset_rights_attested=_true(request["dataset_rights_attested"]),
        evaluation_contamination_reviewed=_true(request["evaluation_contamination_reviewed"]),
        train=train,
        validation=validation,
    )
    _write(stage_fd, "training.yaml", bundle.training_yaml)
    _write(stage_fd, "dataset_info.json", bundle.dataset_info)
    _write(stage_fd, "train.jsonl", bundle.train)
    _write(stage_fd, "validation.jsonl", bundle.validation)
    _write(stage_fd, "manifest.json", bundle.manifest)
    _fsync(stage_fd)
    if set(os.listdir(stage_fd)) != set(TRAINING_JOB_FILES) | {STAGE_MARKER}:
        raise TrainingJobContractError("training_job_publication_failed")
    manifest = _object(bundle.manifest)
    return {
        "publication_manifest": manifest,
        "files": {
            name: _descriptor(value)
            for name, value in (
                ("manifest.json", bundle.manifest),
                ("training.yaml", bundle.training_yaml),
                ("dataset_info.json", bundle.dataset_info),
                ("train.jsonl", bundle.train),
                ("validation.jsonl", bundle.validation),
            )
        },
        "train_count": bundle.train_count,
        "validation_count": bundle.validation_count,
    }


def _validate_phase10_manifest(
    raw: bytes, request: dict[str, object], train: bytes, validation: bytes, provenance: bytes
) -> None:
    manifest = _object(raw)
    expected = {
        "artifact_contract_version",
        "department_id",
        "source_bundle_id",
        "build_id",
        "publication_attempt_id",
        "attempt_number",
        "code_revision",
        "normalization_version",
        "example_contract_version",
        "split_version",
        "validation_ratio",
        "source_example_count",
        "source_group_count",
        "source_reference_count",
        "train_example_count",
        "validation_example_count",
        "files",
    }
    if set(manifest) != expected:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    if (
        manifest.get("artifact_contract_version") != request["dataset_artifact_contract_version"]
        or manifest.get("department_id") != request["department_id"]
        or manifest.get("build_id") != request["dataset_build_id"]
        or manifest.get("normalization_version") != request["dataset_normalization_version"]
        or manifest.get("example_contract_version") != request["dataset_example_contract_version"]
        or manifest.get("split_version") != request["dataset_split_version"]
        or _digest(raw) != request["dataset_manifest_sha256"]
    ):
        raise TrainingJobContractError("dataset_artifact_mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {
        "train.jsonl",
        "validation.jsonl",
        "provenance.jsonl",
    }:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    for name, value, digest, size in (
        (
            "train.jsonl",
            train,
            request["expected_train_sha256"],
            request["expected_train_byte_size"],
        ),
        (
            "validation.jsonl",
            validation,
            request["expected_validation_sha256"],
            request["expected_validation_byte_size"],
        ),
        (
            "provenance.jsonl",
            provenance,
            request["expected_provenance_sha256"],
            request["expected_provenance_byte_size"],
        ),
    ):
        descriptor = files.get(name)
        if not isinstance(descriptor, dict) or descriptor != {"sha256": digest, "byte_size": size}:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        if _digest(value) != digest or len(value) != size:
            raise TrainingJobContractError("dataset_artifact_mismatch")


def _read_frame() -> dict[str, object]:
    prefix = _read_exact(4)
    size = struct.unpack("!I", prefix)[0]
    if not 1 <= size <= _MAX_REQUEST:
        raise TrainingJobContractError("training_job_publication_failed")
    return _object(_read_exact(size))


def _write_frame(value: dict[str, object]) -> None:
    try:
        raw = canonical_json_bytes(value)
        if not 1 <= len(raw) <= _MAX_RESPONSE:
            raw = b'{"status":"error","code":"training_job_publication_failed"}'
        os.write(1, struct.pack("!I", len(raw)) + raw)
    except OSError:
        pass


def _read_exact(size: int) -> bytes:
    value = bytearray()
    while len(value) < size:
        block = os.read(0, size - len(value))
        if not block:
            raise TrainingJobContractError("training_job_publication_failed")
        value.extend(block)
    return bytes(value)


def _object(raw: bytes) -> dict[str, object]:
    def reject(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise TrainingJobContractError("training_job_publication_failed") from error
    if not isinstance(value, dict):
        raise TrainingJobContractError("training_job_publication_failed")
    return value


def _directory(descriptor: int, *, writable: bool) -> None:
    details = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_nlink < 2
    ):
        raise TrainingJobContractError("dataset_artifact_mismatch")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise TrainingJobContractError("dataset_artifact_mismatch")
    if writable and not os.access(".", os.W_OK | os.X_OK, dir_fd=descriptor):
        raise TrainingJobContractError("training_job_publication_failed")


def _read(directory: int, name: str, maximum: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != 0o600
            or not 1 <= details.st_size <= maximum
        ):
            raise TrainingJobContractError("dataset_artifact_mismatch")
        value = bytearray()
        while True:
            block = os.read(descriptor, min(64 * 1024, maximum + 1 - len(value)))
            if not block:
                break
            value.extend(block)
            if len(value) > maximum:
                raise TrainingJobContractError("dataset_artifact_mismatch")
        if len(value) != details.st_size:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        return bytes(value)
    finally:
        os.close(descriptor)


def _write(directory: int, name: str, value: bytes) -> None:
    if not value or name not in TRAINING_JOB_FILES:
        raise TrainingJobContractError("training_job_publication_failed")
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        view = memoryview(value)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise TrainingJobContractError("training_job_publication_failed")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fd(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TrainingJobContractError("training_job_publication_failed")
    return value


def _uuid(value: object) -> UUID:
    try:
        result = UUID(value) if isinstance(value, str) else value
    except ValueError as error:
        raise TrainingJobContractError("training_job_publication_failed") from error
    if not isinstance(result, UUID) or result.int == 0:
        raise TrainingJobContractError("training_job_publication_failed")
    return result


def _positive(value: object) -> int:
    if type(value) is not int or value < 1:
        raise TrainingJobContractError("training_job_publication_failed")
    return value


def _revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise TrainingJobContractError("training_job_publication_failed")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TrainingJobContractError("training_job_publication_failed")
    return value


def _true(value: object) -> bool:
    if value is not True:
        raise TrainingJobContractError("training_job_publication_failed")
    return True


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _descriptor(value: bytes) -> dict[str, object]:
    return {"sha256": _digest(value), "byte_size": len(value)}


def _fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise TrainingJobContractError("training_job_publication_failed") from error


if __name__ == "__main__":
    raise SystemExit(main())
