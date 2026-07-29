"""Fixed exec child for Phase 11 contentful bundle construction.

The child receives only exact inherited descriptors.  It never opens a Phase
10 source by pathname, never receives a source directory descriptor, and
streams the two dataset JSONL files into the exact private job stage.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from dataclasses import dataclass
from uuid import UUID

from app.sft_artifacts import STAGE_MARKER
from app.training_job_domain import (
    TRAINING_JOB_FILES,
    TrainingJobContractError,
    ValidatedDataset,
    build_bundle,
    canonical_json_bytes,
    validate_phase10_record_line,
)

_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
_MAX_REQUEST = 64 * 1024
_MAX_RESPONSE = 32 * 1024
_MAX_FILE = 512 * 1024 * 1024
_MAX_MANIFEST = 256 * 1024
_COPY_BLOCK = 64 * 1024
# A valid reviewed record is much smaller than this bound.  Retaining at most
# one line makes memory independent of the complete external dataset size.
_MAX_JSONL_LINE = 32 * 1024


@dataclass(frozen=True, slots=True)
class _ExpectedFile:
    descriptor: int
    sha256: str
    byte_size: int
    maximum: int


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
        _write_frame({"status": "ok", "result": _build(payload)})
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
        "manifest_fd",
        "train_fd",
        "validation_fd",
        "provenance_fd",
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
        "expected_manifest_sha256",
        "expected_manifest_byte_size",
        "expected_train_sha256",
        "expected_train_byte_size",
        "expected_validation_sha256",
        "expected_validation_byte_size",
        "expected_provenance_sha256",
        "expected_provenance_byte_size",
    }
    if set(request) != expected:
        raise TrainingJobContractError("training_job_publication_failed")
    stage_fd = _fd(request["stage_fd"])
    _directory(stage_fd, writable=True)
    if set(os.listdir(stage_fd)) != {STAGE_MARKER}:
        raise TrainingJobContractError("training_job_publication_failed")

    manifest_file = _expected(request, "manifest", _MAX_MANIFEST)
    train_file = _expected(request, "train", _MAX_FILE)
    validation_file = _expected(request, "validation", _MAX_FILE)
    provenance_file = _expected(request, "provenance", _MAX_FILE)
    raw_manifest = _read_retained(manifest_file)
    _validate_phase10_manifest(raw_manifest, request, train_file, validation_file, provenance_file)
    train = _copy_and_validate_jsonl(train_file, stage_fd, "train.jsonl")
    validation = _copy_and_validate_jsonl(validation_file, stage_fd, "validation.jsonl")
    _verify_retained_digest(provenance_file)
    dataset = ValidatedDataset(
        train_count=train,
        validation_count=validation,
        train_sha256=train_file.sha256,
        validation_sha256=validation_file.sha256,
        train_byte_size=train_file.byte_size,
        validation_byte_size=validation_file.byte_size,
    )
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
        dataset=dataset,
    )
    _write(stage_fd, "training.yaml", bundle.training_yaml)
    _write(stage_fd, "dataset_info.json", bundle.dataset_info)
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
            )
        }
        | {
            "train.jsonl": {"sha256": train_file.sha256, "byte_size": train_file.byte_size},
            "validation.jsonl": {
                "sha256": validation_file.sha256,
                "byte_size": validation_file.byte_size,
            },
        },
        "train_count": bundle.train_count,
        "validation_count": bundle.validation_count,
    }


def _expected(request: dict[str, object], prefix: str, maximum: int) -> _ExpectedFile:
    descriptor = _fd(request[f"{prefix}_fd"])
    sha256 = _sha256(request[f"expected_{prefix}_sha256"])
    byte_size = _size(request[f"expected_{prefix}_byte_size"], maximum)
    details = _private_regular(descriptor, maximum)
    if details.st_size != byte_size:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    return _ExpectedFile(descriptor, sha256, byte_size, maximum)


def _validate_phase10_manifest(
    raw: bytes,
    request: dict[str, object],
    train: _ExpectedFile,
    validation: _ExpectedFile,
    provenance: _ExpectedFile,
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
    for name, value in (
        ("train.jsonl", train),
        ("validation.jsonl", validation),
        ("provenance.jsonl", provenance),
    ):
        descriptor = files.get(name)
        if not isinstance(descriptor, dict) or descriptor != {
            "sha256": value.sha256,
            "byte_size": value.byte_size,
        }:
            raise TrainingJobContractError("dataset_artifact_mismatch")


def _copy_and_validate_jsonl(source: _ExpectedFile, stage_fd: int, name: str) -> int:
    before = _private_regular(source.descriptor, source.maximum)
    if before.st_size != source.byte_size:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    _seek_start(source.descriptor)
    destination = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage_fd
    )
    source_hash = hashlib.sha256()
    destination_hash = hashlib.sha256()
    byte_size = 0
    lines = bytearray()
    count = 0
    ended_with_newline = False
    try:
        while True:
            block = os.read(source.descriptor, _COPY_BLOCK)
            if not block:
                break
            byte_size += len(block)
            if byte_size > source.byte_size:
                raise TrainingJobContractError("dataset_artifact_mismatch")
            source_hash.update(block)
            _write_all(destination, block)
            destination_hash.update(block)
            for line in block.splitlines(keepends=True):
                lines.extend(line)
                if len(lines) > _MAX_JSONL_LINE:
                    raise TrainingJobContractError("dataset_artifact_mismatch")
                if lines.endswith(b"\n"):
                    record = bytes(lines[:-1])
                    validate_phase10_record_line(record)
                    count += 1
                    lines.clear()
                    ended_with_newline = True
                else:
                    ended_with_newline = False
        if byte_size != source.byte_size or not ended_with_newline or lines or count < 1:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        if (
            source_hash.hexdigest() != source.sha256
            or destination_hash.hexdigest() != source.sha256
        ):
            raise TrainingJobContractError("dataset_artifact_mismatch")
        details = os.fstat(destination)
        if details.st_size != byte_size or stat.S_IMODE(details.st_mode) != 0o600:
            raise TrainingJobContractError("training_job_publication_failed")
        os.fsync(destination)
    finally:
        os.close(destination)
    after = os.fstat(source.descriptor)
    if not _same_file(before, after) or after.st_size != source.byte_size:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    return count


def _read_retained(source: _ExpectedFile) -> bytes:
    before = _private_regular(source.descriptor, source.maximum)
    if before.st_size != source.byte_size:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    _seek_start(source.descriptor)
    value = bytearray()
    digest = hashlib.sha256()
    while True:
        block = os.read(source.descriptor, _COPY_BLOCK)
        if not block:
            break
        value.extend(block)
        digest.update(block)
        if len(value) > source.maximum:
            raise TrainingJobContractError("dataset_artifact_mismatch")
    after = os.fstat(source.descriptor)
    if (
        len(value) != source.byte_size
        or digest.hexdigest() != source.sha256
        or not _same_file(before, after)
    ):
        raise TrainingJobContractError("dataset_artifact_mismatch")
    return bytes(value)


def _verify_retained_digest(source: _ExpectedFile) -> None:
    before = _private_regular(source.descriptor, source.maximum)
    if before.st_size != source.byte_size:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    _seek_start(source.descriptor)
    digest = hashlib.sha256()
    byte_size = 0
    while True:
        block = os.read(source.descriptor, _COPY_BLOCK)
        if not block:
            break
        byte_size += len(block)
        if byte_size > source.byte_size:
            raise TrainingJobContractError("dataset_artifact_mismatch")
        digest.update(block)
    after = os.fstat(source.descriptor)
    if (
        byte_size != source.byte_size
        or digest.hexdigest() != source.sha256
        or not _same_file(before, after)
    ):
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
        raise TrainingJobContractError("dataset_artifact_mismatch") from error
    if not isinstance(value, dict):
        raise TrainingJobContractError("dataset_artifact_mismatch")
    return value


def _directory(descriptor: int, *, writable: bool) -> None:
    details = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(details.st_mode)
        or stat.S_IMODE(details.st_mode) != 0o700
        or details.st_nlink < 2
        or (hasattr(os, "getuid") and details.st_uid != os.getuid())
    ):
        raise TrainingJobContractError("dataset_artifact_mismatch")
    if writable and not os.access(".", os.W_OK | os.X_OK, dir_fd=descriptor):
        raise TrainingJobContractError("training_job_publication_failed")


def _private_regular(descriptor: int, maximum: int) -> os.stat_result:
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) != 0o600
        or not 1 <= details.st_size <= maximum
        or (hasattr(os, "getuid") and details.st_uid != os.getuid())
    ):
        raise TrainingJobContractError("dataset_artifact_mismatch")
    return details


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _write(directory: int, name: str, value: bytes) -> None:
    if not value or name not in TRAINING_JOB_FILES:
        raise TrainingJobContractError("training_job_publication_failed")
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        _write_all(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise TrainingJobContractError("training_job_publication_failed")
        view = view[count:]


def _fd(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TrainingJobContractError("training_job_publication_failed")
    return value


def _size(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise TrainingJobContractError("dataset_artifact_mismatch")
    return value


def _sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise TrainingJobContractError("dataset_artifact_mismatch")
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


def _seek_start(descriptor: int) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise TrainingJobContractError("dataset_artifact_mismatch") from error


def _fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise TrainingJobContractError("training_job_publication_failed") from error


if __name__ == "__main__":
    raise SystemExit(main())
