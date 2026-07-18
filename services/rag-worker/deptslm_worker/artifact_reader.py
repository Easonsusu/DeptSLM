"""Incremental no-follow validation of succeeded Phase 5 artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope
from app.extraction_domain import (
    CHUNKING_VERSION,
    NORMALIZATION_VERSION,
    PIPELINE_VERSION,
)

DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
FINAL_FILES = frozenset({"normalized.txt", "chunks.jsonl", "manifest.json"})
MAX_MANIFEST_BYTES = 64 * 1024
MAX_JSONL_LINE_BYTES = 2 * 1024 * 1024


class ArtifactError(RuntimeError):
    def __init__(self, code: str = "chunk_artifact_mismatch") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ArtifactExpectation:
    department_id: UUID
    document_id: UUID
    extraction_id: UUID
    expected_chunk_count: int
    normalized_sha256: str
    normalized_byte_size: int
    output_byte_size: int


@dataclass(frozen=True, slots=True)
class ExternalChunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    byte_size: int
    content_sha256: str
    provenance_kind: str
    page_start: int | None
    page_end: int | None
    line_start: int | None
    line_end: int | None


class Phase5ArtifactReader:
    def __init__(
        self,
        data_dir: Path,
        scope: DepartmentScope,
        expectation: ArtifactExpectation,
    ) -> None:
        if scope.value != expectation.department_id:
            raise ArtifactError()
        self.expectation = expectation
        self.descriptors: list[int] = []
        self.files: dict[str, tuple[int, os.stat_result]] = {}
        try:
            self.descriptors.append(_open_root(data_dir / "extracted_text"))
            for value in (
                scope.value,
                expectation.document_id,
                expectation.extraction_id,
            ):
                self.descriptors.append(
                    _open_uuid_directory(self.descriptors[-1], value)
                )
            final_fd = self.descriptors[-1]
            if set(os.listdir(final_fd)) != FINAL_FILES:
                raise ArtifactError()
            for name in FINAL_FILES:
                self.files[name] = _open_regular(final_fd, name)
            total = sum(metadata.st_size for _, metadata in self.files.values())
            if total != expectation.output_byte_size:
                raise ArtifactError()
            self.manifest = self._manifest()
            self._validate_normalized()
        except FileNotFoundError as error:
            self.close()
            raise ArtifactError("chunk_artifact_missing") from error
        except ArtifactError:
            self.close()
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            self.close()
            raise ArtifactError() from error

    def _manifest(self) -> dict:
        descriptor, metadata = self.files["manifest.json"]
        if metadata.st_size <= 0 or metadata.st_size > MAX_MANIFEST_BYTES:
            raise ArtifactError()
        payload = _read_exact(descriptor, metadata.st_size)
        if not payload.endswith(b"\n"):
            raise ArtifactError()
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ArtifactError()
        required = {
            "chunk_count",
            "chunking_version",
            "chunks_sha256",
            "department_id",
            "document_id",
            "extraction_id",
            "normalization_version",
            "normalized_byte_size",
            "normalized_sha256",
            "parser_name",
            "parser_version",
            "pipeline_version",
            "source_byte_size",
            "source_sha256",
        }
        if not required.issubset(value) or set(value) - required - {"chunks_byte_size"}:
            raise ArtifactError()
        expected = self.expectation
        if (
            value["chunk_count"] != expected.expected_chunk_count
            or value["pipeline_version"] != PIPELINE_VERSION
            or value["normalization_version"] != NORMALIZATION_VERSION
            or value["chunking_version"] != CHUNKING_VERSION
            or value["department_id"] != str(expected.department_id)
            or value["document_id"] != str(expected.document_id)
            or value["extraction_id"] != str(expected.extraction_id)
            or value["normalized_byte_size"] != expected.normalized_byte_size
            or value["normalized_sha256"] != expected.normalized_sha256
            or not _sha256_string(value["chunks_sha256"])
            or not _sha256_string(value["source_sha256"])
            or not isinstance(value["source_byte_size"], int)
            or value["source_byte_size"] <= 0
            or not _safe_identifier(value["parser_name"])
            or not _safe_version(value["parser_version"])
        ):
            raise ArtifactError()
        chunks_size = value.get("chunks_byte_size")
        if (
            chunks_size is not None
            and chunks_size != self.files["chunks.jsonl"][1].st_size
        ):
            raise ArtifactError()
        return value

    def _validate_normalized(self) -> None:
        descriptor, metadata = self.files["normalized.txt"]
        review = _hash_file(descriptor)
        if (
            metadata.st_size != self.expectation.normalized_byte_size
            or review != self.expectation.normalized_sha256
        ):
            raise ArtifactError()

    def iter_chunks(self):
        descriptor, metadata = self.files["chunks.jsonl"]
        duplicate = os.dup(descriptor)
        digest = hashlib.sha256()
        total = 0
        count = 0
        try:
            with os.fdopen(duplicate, "rb", closefd=True) as stream:
                duplicate = -1
                while True:
                    line = stream.readline(MAX_JSONL_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_JSONL_LINE_BYTES or not line.endswith(b"\n"):
                        raise ArtifactError()
                    total += len(line)
                    digest.update(line)
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as error:
                        raise ArtifactError() from error
                    chunk = _chunk(value, count)
                    count += 1
                    if count > self.expectation.expected_chunk_count:
                        raise ArtifactError()
                    yield chunk
            if (
                total != metadata.st_size
                or count != self.expectation.expected_chunk_count
                or digest.hexdigest() != self.manifest["chunks_sha256"]
            ):
                raise ArtifactError()
        finally:
            if duplicate >= 0:
                os.close(duplicate)

    def verify_unchanged(self) -> None:
        """Revalidate descriptor identity and exact content before releasing text."""

        try:
            for descriptor, expected in self.files.values():
                actual = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(actual.st_mode)
                    or actual.st_dev != expected.st_dev
                    or actual.st_ino != expected.st_ino
                    or actual.st_size != expected.st_size
                    or actual.st_nlink != 1
                    or actual.st_mtime_ns != expected.st_mtime_ns
                ):
                    raise ArtifactError()
            if (
                _hash_file(self.files["chunks.jsonl"][0])
                != self.manifest["chunks_sha256"]
            ):
                raise ArtifactError()
            self._validate_normalized()
            if self._manifest() != self.manifest:
                raise ArtifactError()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ArtifactError() from error

    def close(self) -> None:
        for descriptor, _metadata in self.files.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.files.clear()
        for descriptor in reversed(self.descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.descriptors.clear()

    def __enter__(self) -> Phase5ArtifactReader:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def _chunk(value, expected_ordinal: int) -> ExternalChunk:
    required = {
        "ordinal",
        "text",
        "char_start",
        "char_end",
        "byte_size",
        "content_sha256",
        "provenance_kind",
        "page_start",
        "page_end",
        "line_start",
        "line_end",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ArtifactError()
    text_value = value["text"]
    encoded = text_value.encode("utf-8") if isinstance(text_value, str) else b""
    if (
        value["ordinal"] != expected_ordinal
        or not text_value
        or isinstance(value["char_start"], bool)
        or not isinstance(value["char_start"], int)
        or isinstance(value["char_end"], bool)
        or not isinstance(value["char_end"], int)
        or isinstance(value["byte_size"], bool)
        or not isinstance(value["byte_size"], int)
        or value["char_start"] < 0
        or value["char_end"] <= value["char_start"]
        or value["byte_size"] != len(encoded)
        or value["content_sha256"] != hashlib.sha256(encoded).hexdigest()
    ):
        raise ArtifactError()
    kind = value["provenance_kind"]
    page_start, page_end = value["page_start"], value["page_end"]
    line_start, line_end = value["line_start"], value["line_end"]
    if kind == "page":
        valid = (
            isinstance(page_start, int)
            and isinstance(page_end, int)
            and page_start > 0
            and page_end >= page_start
            and line_start is None
            and line_end is None
        )
    elif kind == "line":
        valid = (
            isinstance(line_start, int)
            and isinstance(line_end, int)
            and line_start > 0
            and line_end >= line_start
            and page_start is None
            and page_end is None
        )
    else:
        valid = False
    if not valid:
        raise ArtifactError()
    return ExternalChunk(
        expected_ordinal,
        text_value,
        value["char_start"],
        value["char_end"],
        value["byte_size"],
        value["content_sha256"],
        kind,
        page_start,
        page_end,
        line_start,
        line_end,
    )


def _open_root(path: Path) -> int:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactError()
    return os.open(path, DIRECTORY_FLAGS)


def _open_uuid_directory(parent: int, value: UUID) -> int:
    name = str(value)
    descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ArtifactError()
    return descriptor


def _open_regular(parent: int, name: str) -> tuple[int, os.stat_result]:
    descriptor = os.open(name, READ_FLAGS, dir_fd=parent)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise ArtifactError()
    return descriptor, metadata


def _read_exact(descriptor: int, size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    result = bytearray()
    while len(result) < size:
        chunk = os.read(descriptor, min(65536, size - len(result)))
        if not chunk:
            break
        result.extend(chunk)
    if len(result) != size:
        raise ArtifactError()
    return bytes(result)


def _hash_file(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_string(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _safe_identifier(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 100
        and value.replace("-", "").replace("_", "").replace(".", "").isalnum()
    )


def _safe_version(value) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 100
        and all(character.isalnum() or character in "._+-" for character in value)
    )
