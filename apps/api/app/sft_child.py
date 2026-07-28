"""Fixed exec child for contentful Phase 10 dataset construction.

This module has no database, authentication, vector-service, or runtime
configuration imports.  It receives only two inherited external descriptors:
the verified source directory and one exact private dataset stage directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
from dataclasses import dataclass
from uuid import UUID

from app.sft_artifacts import DATASET_FILES, STAGE_MARKER
from app.sft_domain import (
    DATASET_ARTIFACT_CONTRACT_VERSION,
    EXAMPLE_CONTRACT_VERSION,
    NORMALIZATION_VERSION,
    SPLIT_VERSION,
    VALIDATION_RATIO,
    SftContractError,
    canonical_json_bytes,
    dataset_record,
    parse_source_bundle,
    provenance_record,
    split_examples,
)
from app.sft_supervision import MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES

_CHILD_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


@dataclass(frozen=True, slots=True)
class _AuthorityReference:
    document_id: str
    extraction_id: str
    indexing_id: str
    chunk_id: str
    vector_attempt_id: str

    def provenance_value(self) -> dict[str, str]:
        return {
            "document_id": self.document_id,
            "extraction_id": self.extraction_id,
            "indexing_id": self.indexing_id,
            "chunk_id": self.chunk_id,
            "vector_attempt_id": self.vector_attempt_id,
        }


def main() -> int:
    try:
        # macOS may inject locale metadata while execing a process.  Remove it
        # before any operation so the child runtime is the reviewed allowlist.
        os.environ.clear()
        os.environ.update(_CHILD_ENVIRONMENT)
        request = _read_request()
        if set(request) != {"operation", "request"}:
            raise SftContractError("dataset_publication_failed")
        raw = request["request"]
        if not isinstance(raw, dict):
            raise SftContractError("dataset_publication_failed")
        if request["operation"] == "build_dataset":
            result = _build_dataset(raw)
        elif request["operation"] == "boundary_probe":
            result = _boundary_probe(raw)
        else:
            raise SftContractError("dataset_publication_failed")
        _write_response({"status": "ok", "result": result})
        return 0
    except BaseException as caught:
        code = getattr(caught, "code", "dataset_publication_failed")
        _write_response(
            {
                "status": "error",
                "code": code if isinstance(code, str) else "dataset_publication_failed",
            }
        )
        return 1


def _build_dataset(request: dict[str, object]) -> dict[str, object]:
    required = {
        "source_fd",
        "stage_fd",
        "department_id",
        "source_bundle_id",
        "build_id",
        "publication_attempt_id",
        "attempt_number",
        "code_revision",
        "authority_fingerprint",
        "authority",
    }
    if set(request) != required:
        raise SftContractError("dataset_publication_failed")
    source_fd = _fd(request["source_fd"])
    stage_fd = _fd(request["stage_fd"])
    department_id = _uuid(request["department_id"])
    source_bundle_id = _uuid(request["source_bundle_id"])
    build_id = _uuid(request["build_id"])
    attempt_id = _uuid(request["publication_attempt_id"])
    attempt_number = request["attempt_number"]
    code_revision = request["code_revision"]
    fingerprint = request["authority_fingerprint"]
    if (
        type(attempt_number) is not int
        or attempt_number < 1
        or not isinstance(code_revision, str)
        or len(code_revision) > 40
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
    ):
        raise SftContractError("dataset_publication_failed")
    _private_directory(source_fd, writable=False)
    _private_directory(stage_fd, writable=True)
    if set(os.listdir(source_fd)) != {"manifest.json", "examples.jsonl"}:
        raise SftContractError("source_artifact_mismatch")
    if set(os.listdir(stage_fd)) != {STAGE_MARKER}:
        raise SftContractError("dataset_publication_failed")
    manifest_raw = _read_file(source_fd, "manifest.json", 128 * 1024)
    examples_raw = _read_file(source_fd, "examples.jsonl", 512 * 1024 * 1024)
    if set(os.listdir(source_fd)) != {"manifest.json", "examples.jsonl"}:
        raise SftContractError("source_artifact_mismatch")
    source = parse_source_bundle(manifest_raw, examples_raw)
    if source.department_id != department_id or source.source_bundle_id != source_bundle_id:
        raise SftContractError("source_artifact_mismatch")
    authorities = _authorities(request["authority"])
    source_chunk_ids = {item for example in source.examples for item in example.source_chunk_ids}
    if set(authorities) != source_chunk_ids:
        raise SftContractError("source_authority_changed")
    train, validation = split_examples(source, build_id=build_id)
    train_digest = _write_lines(stage_fd, "train.jsonl", (dataset_record(item) for item in train))
    validation_digest = _write_lines(
        stage_fd, "validation.jsonl", (dataset_record(item) for item in validation)
    )
    provenance_digest = _write_lines(
        stage_fd,
        "provenance.jsonl",
        (provenance_record(item, split="train", authorities=authorities) for item in train),
        suffix=(
            provenance_record(item, split="validation", authorities=authorities)
            for item in validation
        ),
    )
    manifest = {
        "artifact_contract_version": DATASET_ARTIFACT_CONTRACT_VERSION,
        "department_id": str(department_id),
        "source_bundle_id": str(source_bundle_id),
        "build_id": str(build_id),
        "publication_attempt_id": str(attempt_id),
        "attempt_number": attempt_number,
        "code_revision": code_revision,
        "normalization_version": NORMALIZATION_VERSION,
        "example_contract_version": EXAMPLE_CONTRACT_VERSION,
        "split_version": SPLIT_VERSION,
        "validation_ratio": str(VALIDATION_RATIO),
        "source_example_count": len(source.examples),
        "source_group_count": source.group_count,
        "source_reference_count": source.source_reference_count,
        "train_example_count": len(train),
        "validation_example_count": len(validation),
        "files": {
            "train.jsonl": train_digest,
            "validation.jsonl": validation_digest,
            "provenance.jsonl": provenance_digest,
        },
    }
    manifest_digest = _write_bytes(
        stage_fd, "manifest.json", canonical_json_bytes(manifest) + b"\n"
    )
    if set(os.listdir(stage_fd)) != set(DATASET_FILES) | {STAGE_MARKER}:
        raise SftContractError("dataset_publication_failed")
    return {
        "source": {
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "examples_sha256": source.examples_sha256,
            "examples_byte_size": source.examples_byte_size,
            "example_count": len(source.examples),
            "group_count": source.group_count,
            "source_reference_count": source.source_reference_count,
        },
        "publication_manifest": manifest,
        "files": {
            "manifest.json": manifest_digest,
            "train.jsonl": train_digest,
            "validation.jsonl": validation_digest,
            "provenance.jsonl": provenance_digest,
        },
        "train_count": len(train),
        "validation_count": len(validation),
        "authority_fingerprint": fingerprint,
    }


def _boundary_probe(request: dict[str, object]) -> dict[str, object]:
    """Internal regression probe for the fixed exec boundary, never an API operation."""

    if set(request) != {"probe_fd"}:
        raise SftContractError("dataset_publication_failed")
    descriptor = _fd(request["probe_fd"])
    try:
        os.fstat(descriptor)
    except OSError:
        descriptor_open = False
    else:
        descriptor_open = True
    return {
        "database_url_present": "DATABASE_URL" in os.environ,
        "sentinel_present": "DEPTSLM_PHASE10_SENTINEL" in os.environ,
        "probe_fd_open": descriptor_open,
        "environment_keys": sorted(os.environ),
    }


def _authorities(value: object) -> dict[UUID, _AuthorityReference]:
    if not isinstance(value, list) or len(value) > 800_000:
        raise SftContractError("source_authority_changed")
    result: dict[UUID, _AuthorityReference] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "document_id",
            "extraction_id",
            "indexing_id",
            "chunk_id",
            "vector_attempt_id",
        }:
            raise SftContractError("source_authority_changed")
        values = {key: _uuid(item[key]) for key in item}
        reference = _AuthorityReference(**{key: str(value) for key, value in values.items()})
        if values["chunk_id"] in result:
            raise SftContractError("source_authority_changed")
        result[values["chunk_id"]] = reference
    return result


def _read_request() -> dict[str, object]:
    raw = _read_exact(0, 4)
    size = struct.unpack("!I", raw)[0]
    if not 1 <= size <= MAX_REQUEST_FRAME_BYTES:
        raise SftContractError("dataset_publication_failed")
    value = json.loads(_read_exact(0, size).decode("utf-8"))
    if not isinstance(value, dict):
        raise SftContractError("dataset_publication_failed")
    return value


def _write_response(value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(raw) <= MAX_RESPONSE_FRAME_BYTES:
        raw = b'{"code":"dataset_publication_failed","status":"error"}'
    payload = struct.pack("!I", len(raw)) + raw
    total = 0
    while total < len(payload):
        total += os.write(1, payload[total:])


def _read_exact(descriptor: int, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        block = os.read(descriptor, size - len(result))
        if not block:
            raise SftContractError("dataset_publication_failed")
        result.extend(block)
    return bytes(result)


def _fd(value: object) -> int:
    if type(value) is not int or value < 3:
        raise SftContractError("dataset_publication_failed")
    return value


def _uuid(value: object) -> UUID:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError as error:
        raise SftContractError("dataset_publication_failed") from error
    if parsed is None or parsed.int == 0:
        raise SftContractError("dataset_publication_failed")
    return parsed


def _private_directory(descriptor: int, *, writable: bool) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or (writable and not metadata.st_mode & stat.S_IWUSR)
    ):
        raise SftContractError("dataset_publication_failed")


def _read_file(directory_fd: int, name: str, maximum: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o077
            or not 1 <= before.st_size <= maximum
        ):
            raise SftContractError("source_artifact_mismatch")
        output = bytearray()
        while True:
            block = os.read(descriptor, min(64 * 1024, maximum + 1 - len(output)))
            if not block:
                break
            output.extend(block)
            if len(output) > maximum:
                raise SftContractError("source_artifact_mismatch")
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_file(before, after) or not _same_file(before, current):
            raise SftContractError("source_artifact_mismatch")
        return bytes(output)
    finally:
        os.close(descriptor)


def _write_lines(
    directory_fd: int,
    name: str,
    values,
    *,
    suffix=(),
) -> dict[str, object]:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        digest = hashlib.sha256()
        total = 0
        for group in (values, suffix):
            for value in group:
                raw = canonical_json_bytes(value) + b"\n"
                _write_all(descriptor, raw)
                digest.update(raw)
                total += len(raw)
        if total < 1:
            raise SftContractError("dataset_publication_failed")
        os.fsync(descriptor)
        return {"sha256": digest.hexdigest(), "byte_size": total}
    finally:
        os.close(descriptor)


def _write_bytes(directory_fd: int, name: str, value: bytes) -> dict[str, object]:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        _write_all(descriptor, value)
        os.fsync(descriptor)
        return {"sha256": hashlib.sha256(value).hexdigest(), "byte_size": len(value)}
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, value: bytes) -> None:
    total = 0
    view = memoryview(value)
    while total < len(value):
        total += os.write(descriptor, view[total:])


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_uid == second.st_uid
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_nlink == second.st_nlink == 1
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


if __name__ == "__main__":  # pragma: no cover - executable module entrypoint
    raise SystemExit(main())
