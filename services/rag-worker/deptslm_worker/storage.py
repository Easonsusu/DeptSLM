"""Descriptor-relative source and extraction storage boundaries."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope

DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
)
READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
FINAL_FILES = ("normalized.txt", "chunks.jsonl", "manifest.json")
RESULT_FILE = ".runner-result.json"
SOURCE_SNAPSHOT = ".source.snapshot"
SCRATCH_DIRECTORY = "scratch"
STAGING_FILES = (*FINAL_FILES, RESULT_FILE, SOURCE_SNAPSHOT)
COPY_BLOCK_SIZE = 1024 * 1024
MAX_CLEANUP_DEPTH = 64


class ExtractionStorageError(RuntimeError):
    def __init__(self, code: str = "storage_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class FileReview:
    byte_size: int
    sha256: str


@dataclass(slots=True)
class SourceHandle:
    descriptor: int
    byte_size: int
    sha256: str

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def __enter__(self) -> SourceHandle:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class SourceStorage:
    def __init__(self, data_root: Path) -> None:
        self.root = _validated_root(data_root / "uploads", writable=False)

    def create_verified_snapshot(
        self,
        department: DepartmentScope,
        document_id: UUID,
        expected_size: int,
        expected_sha256: str,
        staging: ExtractionStaging,
        *,
        _copy_observer: Callable[[int], None] | None = None,
    ) -> SourceHandle:
        """Stream verified canonical bytes into an exclusive claim-owned snapshot."""

        source_fd = -1
        snapshot_fd = -1
        try:
            source_fd = self._open_canonical(department, document_id)
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                raise ExtractionStorageError("source_integrity_mismatch")
            snapshot_fd = staging.create_file(SOURCE_SNAPSHOT)
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(source_fd, COPY_BLOCK_SIZE):
                total += len(chunk)
                if total > expected_size:
                    raise ExtractionStorageError("source_integrity_mismatch")
                digest.update(chunk)
                _write_all(snapshot_fd, chunk)
                if _copy_observer is not None:
                    _copy_observer(total)
            os.fsync(snapshot_fd)
            os.fsync(staging.claim_fd)
            after = os.fstat(source_fd)
            snapshot_metadata = os.fstat(snapshot_fd)
            actual_sha256 = digest.hexdigest()
            if (
                total != expected_size
                or actual_sha256 != expected_sha256
                or snapshot_metadata.st_size != expected_size
                or _mutation_identity(before) != _mutation_identity(after)
            ):
                raise ExtractionStorageError("source_integrity_mismatch")
            os.close(snapshot_fd)
            snapshot_fd = -1
            return staging.open_readonly(
                SOURCE_SNAPSHOT, expected_size, expected_sha256
            )
        except FileNotFoundError as error:
            raise ExtractionStorageError("source_missing") from error
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error
        finally:
            if snapshot_fd >= 0:
                os.close(snapshot_fd)
            if source_fd >= 0:
                os.close(source_fd)

    def verify_canonical(
        self,
        department: DepartmentScope,
        document_id: UUID,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        """Re-verify the mutable canonical source without exposing its descriptor."""

        source_fd = -1
        try:
            source_fd = self._open_canonical(department, document_id)
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
                raise ExtractionStorageError("source_integrity_mismatch")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(source_fd, COPY_BLOCK_SIZE):
                total += len(chunk)
                if total > expected_size:
                    raise ExtractionStorageError("source_integrity_mismatch")
                digest.update(chunk)
            after = os.fstat(source_fd)
            if (
                total != expected_size
                or digest.hexdigest() != expected_sha256
                or _mutation_identity(before) != _mutation_identity(after)
            ):
                raise ExtractionStorageError("source_integrity_mismatch")
        except FileNotFoundError as error:
            raise ExtractionStorageError("source_missing") from error
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error
        finally:
            if source_fd >= 0:
                os.close(source_fd)

    def _open_canonical(self, department: DepartmentScope, document_id: UUID) -> int:
        descriptors: list[int] = []
        source_fd = -1
        try:
            descriptors.append(_open_directory(self.root))
            descriptors.append(_open_child_directory(descriptors[-1], str(department)))
            descriptors.append(_open_child_directory(descriptors[-1], str(document_id)))
            source_fd = os.open("source", READ_FLAGS, dir_fd=descriptors[-1])
            if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                os.close(source_fd)
                source_fd = -1
                raise ExtractionStorageError("source_missing")
            return source_fd
        except Exception:
            if source_fd >= 0:
                os.close(source_fd)
            raise
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


@dataclass(slots=True)
class ExtractionStaging:
    department_id: UUID
    document_id: UUID
    extraction_id: UUID
    claim_token: UUID
    root_fd: int
    department_fd: int
    document_fd: int
    staging_fd: int
    extraction_staging_fd: int
    claim_fd: int
    scratch_fd: int
    file_identities: dict[str, FileIdentity] = field(default_factory=dict)
    file_guards: dict[str, int] = field(default_factory=dict)
    prepared_files: dict[str, FileReview] = field(default_factory=dict)
    prepared_output_size: int | None = None
    published: bool = False
    closed: bool = False

    @property
    def temporary_directory(self) -> str:
        """Descriptor alias for non-publishable parser scratch space."""

        if self.scratch_fd < 0:
            raise ExtractionStorageError()
        return f"/dev/fd/{self.scratch_fd}"

    def create_file(self, name: str) -> int:
        if (
            name not in STAGING_FILES
            or self.published
            or self.prepared_output_size is not None
        ):
            raise ExtractionStorageError()
        descriptor = -1
        guard_fd = -1
        try:
            descriptor = os.open(name, CREATE_FLAGS, 0o600, dir_fd=self.claim_fd)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ExtractionStorageError()
            os.fchmod(descriptor, 0o600)
            guard_fd = os.open(name, READ_FLAGS, dir_fd=self.claim_fd)
            guard_metadata = os.fstat(guard_fd)
            if (guard_metadata.st_dev, guard_metadata.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise ExtractionStorageError()
            self.file_identities[name] = FileIdentity(metadata.st_dev, metadata.st_ino)
            self.file_guards[name] = guard_fd
            return descriptor
        except ExtractionStorageError:
            if guard_fd >= 0:
                os.close(guard_fd)
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as error:
            if guard_fd >= 0:
                os.close(guard_fd)
            if descriptor >= 0:
                os.close(descriptor)
            raise ExtractionStorageError() from error

    def open_readonly(
        self, name: str, expected_size: int, expected_sha256: str
    ) -> SourceHandle:
        descriptor = -1
        try:
            descriptor, metadata = self._open_verified_file(name)
            review = _review_file(descriptor, metadata)
            if review.byte_size != expected_size or review.sha256 != expected_sha256:
                raise ExtractionStorageError("source_integrity_mismatch")
            os.lseek(descriptor, 0, os.SEEK_SET)
            return SourceHandle(descriptor, review.byte_size, review.sha256)
        except ExtractionStorageError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise ExtractionStorageError() from error

    def read_file(self, name: str, maximum: int) -> bytes:
        if name not in STAGING_FILES:
            raise ExtractionStorageError()
        descriptor = -1
        try:
            descriptor, _metadata = self._open_verified_file(name)
            data = bytearray()
            while chunk := os.read(
                descriptor, min(COPY_BLOCK_SIZE, maximum + 1 - len(data))
            ):
                data.extend(chunk)
                if len(data) > maximum:
                    raise ExtractionStorageError("extraction_output_limit")
            return bytes(data)
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def write_file(self, name: str, payload: bytes) -> None:
        descriptor = self.create_file(name)
        try:
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except OSError as error:
            raise ExtractionStorageError() from error
        finally:
            os.close(descriptor)

    def remove_file(self, name: str) -> None:
        if name not in STAGING_FILES:
            raise ExtractionStorageError()
        try:
            os.unlink(name, dir_fd=self.claim_fd)
            self.file_identities.pop(name, None)
            self._close_guard(name)
        except FileNotFoundError:
            self.file_identities.pop(name, None)
            self._close_guard(name)
        except OSError as error:
            raise ExtractionStorageError() from error

    def prepare_publication(self) -> int:
        """Remove private state and validate the exact final artifact allowlist."""

        if self.published or self.prepared_output_size is not None:
            raise ExtractionStorageError()
        try:
            self.remove_file(RESULT_FILE)
            self.remove_file(SOURCE_SNAPSHOT)
            self._remove_scratch()
            if set(os.listdir(self.claim_fd)) != set(FINAL_FILES):
                raise ExtractionStorageError()
            total = 0
            for name in FINAL_FILES:
                descriptor, metadata = self._open_verified_file(name)
                try:
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                    metadata = os.fstat(descriptor)
                    review = _review_file(descriptor, metadata)
                    self.prepared_files[name] = review
                    total += review.byte_size
                finally:
                    os.close(descriptor)
            os.fsync(self.claim_fd)
            self.prepared_output_size = total
            return total
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error

    def publish(self) -> None:
        if self.published or self.prepared_output_size is None:
            raise ExtractionStorageError()
        destination = str(self.extraction_id)
        final_fd = -1
        try:
            if set(os.listdir(self.claim_fd)) != set(FINAL_FILES):
                raise ExtractionStorageError()
            os.mkdir(destination, 0o700, dir_fd=self.document_fd)
            final_fd = _open_child_directory(self.document_fd, destination)
            os.fchmod(final_fd, 0o700)
            for name in FINAL_FILES:
                self._verify_reviewed_file(name, self.claim_fd)
                os.rename(name, name, src_dir_fd=self.claim_fd, dst_dir_fd=final_fd)
                self._verify_reviewed_file(name, final_fd)
            if set(os.listdir(final_fd)) != set(FINAL_FILES):
                raise ExtractionStorageError()
            os.fsync(final_fd)
            os.fsync(self.document_fd)
            if os.listdir(self.claim_fd):
                raise ExtractionStorageError()
            os.rmdir(str(self.claim_token), dir_fd=self.extraction_staging_fd)
            try:
                os.rmdir(str(self.extraction_id), dir_fd=self.staging_fd)
            except OSError:
                pass
            self.published = True
        except FileExistsError as error:
            raise ExtractionStorageError() from error
        except ExtractionStorageError:
            if final_fd >= 0:
                self._remove_created_final_if_exact(final_fd)
            raise
        except OSError as error:
            if final_fd >= 0:
                self._remove_created_final_if_exact(final_fd)
            raise ExtractionStorageError() from error
        finally:
            if final_fd >= 0:
                os.close(final_fd)

    def cleanup(self) -> None:
        if self.closed:
            return
        try:
            if not self.published:
                self._close_scratch()
                _clear_directory(self.claim_fd)
                try:
                    os.rmdir(str(self.claim_token), dir_fd=self.extraction_staging_fd)
                except FileNotFoundError:
                    pass
                try:
                    os.rmdir(str(self.extraction_id), dir_fd=self.staging_fd)
                except OSError:
                    pass
        except OSError as error:
            raise ExtractionStorageError() from error
        finally:
            self.close()

    def compensate_final(self) -> None:
        if not self.published:
            self.cleanup()
            return
        final_fd = -1
        try:
            final_fd = _open_child_directory(self.document_fd, str(self.extraction_id))
            if set(os.listdir(final_fd)) != set(FINAL_FILES):
                raise ExtractionStorageError()
            for name in FINAL_FILES:
                self._verify_reviewed_file(name, final_fd)
            for name in FINAL_FILES:
                os.unlink(name, dir_fd=final_fd)
            os.close(final_fd)
            final_fd = -1
            os.rmdir(str(self.extraction_id), dir_fd=self.document_fd)
            os.fsync(self.document_fd)
            self.published = False
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error
        finally:
            if final_fd >= 0:
                os.close(final_fd)
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._close_scratch()
        self._close_guards()
        for descriptor in (
            self.claim_fd,
            self.extraction_staging_fd,
            self.staging_fd,
            self.document_fd,
            self.department_fd,
            self.root_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _open_verified_file(
        self, name: str, *, parent_fd: int | None = None
    ) -> tuple[int, os.stat_result]:
        if name not in STAGING_FILES:
            raise ExtractionStorageError()
        parent = self.claim_fd if parent_fd is None else parent_fd
        descriptor = -1
        try:
            descriptor = os.open(name, READ_FLAGS, dir_fd=parent)
            metadata = os.fstat(descriptor)
            self._verify_file_metadata(name, metadata)
            return descriptor, metadata
        except ExtractionStorageError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise ExtractionStorageError() from error

    def _verify_file_metadata(self, name: str, metadata: os.stat_result) -> None:
        expected = self.file_identities.get(name)
        guard_fd = self.file_guards.get(name)
        guard_metadata = os.fstat(guard_fd) if guard_fd is not None else None
        if (
            expected is None
            or guard_metadata is None
            or not stat.S_ISREG(metadata.st_mode)
            or not stat.S_ISREG(guard_metadata.st_mode)
            or metadata.st_nlink != 1
            or guard_metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (expected.device, expected.inode)
            or (guard_metadata.st_dev, guard_metadata.st_ino)
            != (expected.device, expected.inode)
        ):
            raise ExtractionStorageError()

    def _verify_reviewed_file(self, name: str, parent_fd: int) -> None:
        expected = self.prepared_files.get(name)
        if expected is None:
            raise ExtractionStorageError()
        descriptor, metadata = self._open_verified_file(name, parent_fd=parent_fd)
        try:
            if _review_file(descriptor, metadata) != expected:
                raise ExtractionStorageError()
        finally:
            os.close(descriptor)

    def _remove_scratch(self) -> None:
        self._close_scratch()
        scratch_fd = -1
        try:
            scratch_fd = _open_literal_directory(self.claim_fd, SCRATCH_DIRECTORY)
            _clear_directory(scratch_fd)
            os.close(scratch_fd)
            scratch_fd = -1
            os.rmdir(SCRATCH_DIRECTORY, dir_fd=self.claim_fd)
        except FileNotFoundError:
            pass
        finally:
            if scratch_fd >= 0:
                os.close(scratch_fd)

    def _close_scratch(self) -> None:
        if self.scratch_fd >= 0:
            try:
                os.close(self.scratch_fd)
            except OSError:
                pass
            self.scratch_fd = -1

    def _close_guard(self, name: str) -> None:
        descriptor = self.file_guards.pop(name, None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _close_guards(self) -> None:
        for name in tuple(self.file_guards):
            self._close_guard(name)

    def _remove_created_final_if_exact(self, final_fd: int) -> None:
        try:
            names = set(os.listdir(final_fd))
            if not names.issubset(FINAL_FILES):
                return
            for name in names:
                self._verify_reviewed_file(name, final_fd)
            for name in names:
                os.unlink(name, dir_fd=final_fd)
            os.rmdir(str(self.extraction_id), dir_fd=self.document_fd)
            os.fsync(self.document_fd)
        except (OSError, ExtractionStorageError):
            pass


class ExtractionStorage:
    def __init__(self, data_root: Path) -> None:
        self.root = _validated_root(data_root / "extracted_text", writable=True)

    def create_staging(
        self,
        department: DepartmentScope,
        document_id: UUID,
        extraction_id: UUID,
        claim_token: UUID,
    ) -> ExtractionStaging:
        descriptors: list[int] = []
        claim_fd = -1
        scratch_fd = -1
        claim_created = False
        try:
            descriptors.append(_open_directory(self.root))
            descriptors.append(_open_or_create_child(descriptors[-1], str(department)))
            descriptors.append(_open_or_create_child(descriptors[-1], str(document_id)))
            descriptors.append(_open_or_create_child(descriptors[-1], ".staging"))
            descriptors.append(
                _open_or_create_child(descriptors[-1], str(extraction_id))
            )
            os.mkdir(str(claim_token), 0o700, dir_fd=descriptors[-1])
            claim_created = True
            claim_fd = _open_child_directory(descriptors[-1], str(claim_token))
            os.fchmod(claim_fd, 0o700)
            os.mkdir(SCRATCH_DIRECTORY, 0o700, dir_fd=claim_fd)
            scratch_fd = _open_literal_directory(claim_fd, SCRATCH_DIRECTORY)
            os.fchmod(scratch_fd, 0o700)
            return ExtractionStaging(
                department.value,
                document_id,
                extraction_id,
                claim_token,
                *descriptors,
                claim_fd,
                scratch_fd,
            )
        except Exception as error:
            if scratch_fd >= 0:
                os.close(scratch_fd)
            if claim_fd >= 0:
                try:
                    _clear_directory(claim_fd)
                except OSError:
                    pass
                os.close(claim_fd)
                try:
                    os.rmdir(str(claim_token), dir_fd=descriptors[-1])
                except OSError:
                    pass
            elif claim_created:
                try:
                    os.rmdir(str(claim_token), dir_fd=descriptors[-1])
                except OSError:
                    pass
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(error, ExtractionStorageError):
                raise
            raise ExtractionStorageError() from error

    def cleanup_claim(
        self,
        department: DepartmentScope,
        document_id: UUID,
        extraction_id: UUID,
        claim_token: UUID,
    ) -> None:
        """Idempotently remove only one fully identified stale claim directory."""

        descriptors: list[int] = []
        try:
            descriptors.append(_open_directory(self.root))
            for name in (
                str(department),
                str(document_id),
                ".staging",
                str(extraction_id),
            ):
                descriptor = _try_open_child_directory(descriptors[-1], name)
                if descriptor is None:
                    return
                descriptors.append(descriptor)
            claim_fd = _try_open_child_directory(descriptors[-1], str(claim_token))
            if claim_fd is None:
                return
            descriptors.append(claim_fd)
            _clear_directory(claim_fd)
            os.close(descriptors.pop())
            try:
                os.rmdir(str(claim_token), dir_fd=descriptors[-1])
            except FileNotFoundError:
                pass
            try:
                os.rmdir(str(extraction_id), dir_fd=descriptors[-2])
            except OSError:
                pass
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _validated_root(path: Path, *, writable: bool) -> Path:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ExtractionStorageError() from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ExtractionStorageError()
    mode = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not os.access(path, mode):
        raise ExtractionStorageError()
    return path.resolve(strict=True)


def _open_directory(path: Path) -> int:
    descriptor = os.open(path, DIRECTORY_FLAGS)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ExtractionStorageError()
    return descriptor


def _open_child_directory(parent_fd: int, name: str) -> int:
    _validate_uuid_or_staging(name)
    return _open_literal_directory(parent_fd, name)


def _try_open_child_directory(parent_fd: int, name: str) -> int | None:
    _validate_uuid_or_staging(name)
    try:
        return _open_literal_directory(parent_fd, name)
    except FileNotFoundError:
        return None


def _open_literal_directory(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ExtractionStorageError() from error
        raise
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ExtractionStorageError()
    return descriptor


def _open_or_create_child(parent_fd: int, name: str) -> int:
    _validate_uuid_or_staging(name)
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = _open_child_directory(parent_fd, name)
    os.fchmod(descriptor, 0o700)
    return descriptor


def _validate_uuid_or_staging(value: str) -> None:
    if value == ".staging":
        return
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise ExtractionStorageError() from error
    if str(parsed) != value:
        raise ExtractionStorageError()


def _clear_directory(directory_fd: int, *, depth: int = 0) -> None:
    if depth > MAX_CLEANUP_DEPTH:
        raise OSError("cleanup depth exceeded")
    for name in os.listdir(directory_fd):
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child_fd = -1
            try:
                child_fd = _open_literal_directory(directory_fd, name)
                _clear_directory(child_fd, depth=depth + 1)
            except FileNotFoundError:
                continue
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
            try:
                os.rmdir(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        else:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _mutation_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _review_file(descriptor: int, metadata: os.stat_result) -> FileReview:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while chunk := os.read(descriptor, COPY_BLOCK_SIZE):
        total += len(chunk)
        digest.update(chunk)
    after = os.fstat(descriptor)
    if total != metadata.st_size or _mutation_identity(metadata) != _mutation_identity(
        after
    ):
        raise ExtractionStorageError()
    return FileReview(total, digest.hexdigest())


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]
