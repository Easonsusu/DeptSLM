"""Descriptor-relative source and extraction storage boundaries."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope

DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
FINAL_FILES = ("normalized.txt", "chunks.jsonl", "manifest.json")
STAGING_FILES = (*FINAL_FILES, ".runner-result.json")


class ExtractionStorageError(RuntimeError):
    def __init__(self, code: str = "storage_unavailable") -> None:
        self.code = code
        super().__init__(code)


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

    def open_verified(
        self,
        department: DepartmentScope,
        document_id: UUID,
        expected_size: int,
        expected_sha256: str,
    ) -> SourceHandle:
        descriptors: list[int] = []
        try:
            descriptors.append(_open_directory(self.root))
            descriptors.append(_open_child_directory(descriptors[-1], str(department)))
            descriptors.append(_open_child_directory(descriptors[-1], str(document_id)))
            source_fd = os.open("source", READ_FLAGS, dir_fd=descriptors[-1])
            metadata = os.fstat(source_fd)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(source_fd)
                raise ExtractionStorageError("source_missing")
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(source_fd, 1024 * 1024):
                total += len(chunk)
                digest.update(chunk)
            os.lseek(source_fd, 0, os.SEEK_SET)
            if total != expected_size or digest.hexdigest() != expected_sha256:
                os.close(source_fd)
                raise ExtractionStorageError("source_integrity_mismatch")
            return SourceHandle(source_fd, total, digest.hexdigest())
        except FileNotFoundError as error:
            raise ExtractionStorageError("source_missing") from error
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error
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
    published: bool = False
    closed: bool = False

    @property
    def temporary_directory(self) -> str:
        """Descriptor alias that does not reveal the external host path."""

        return f"/dev/fd/{self.claim_fd}"

    def create_file(self, name: str) -> int:
        if name not in STAGING_FILES or self.published:
            raise ExtractionStorageError()
        try:
            descriptor = os.open(name, CREATE_FLAGS, 0o600, dir_fd=self.claim_fd)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise ExtractionStorageError()
            os.fchmod(descriptor, 0o600)
            return descriptor
        except OSError as error:
            raise ExtractionStorageError() from error

    def read_file(self, name: str, maximum: int) -> bytes:
        if name not in STAGING_FILES:
            raise ExtractionStorageError()
        descriptor = -1
        try:
            descriptor = os.open(name, READ_FLAGS, dir_fd=self.claim_fd)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ExtractionStorageError()
            data = bytearray()
            while chunk := os.read(descriptor, min(1024 * 1024, maximum + 1 - len(data))):
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
        except FileNotFoundError:
            pass
        except OSError as error:
            raise ExtractionStorageError() from error

    def output_size(self) -> int:
        try:
            total = 0
            for name in FINAL_FILES:
                metadata = os.stat(name, dir_fd=self.claim_fd, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ExtractionStorageError()
                total += metadata.st_size
            return total
        except OSError as error:
            raise ExtractionStorageError() from error

    def publish(self) -> None:
        if self.published:
            raise ExtractionStorageError()
        destination = str(self.extraction_id)
        try:
            for name in FINAL_FILES:
                descriptor = os.open(name, READ_FLAGS, dir_fd=self.claim_fd)
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise ExtractionStorageError()
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            try:
                os.stat(destination, dir_fd=self.document_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ExtractionStorageError("storage_unavailable")
            os.fsync(self.claim_fd)
            os.rename(
                str(self.claim_token),
                destination,
                src_dir_fd=self.extraction_staging_fd,
                dst_dir_fd=self.document_fd,
            )
            self.published = True
            os.fsync(self.document_fd)
            try:
                os.rmdir(str(self.extraction_id), dir_fd=self.staging_fd)
            except OSError:
                pass
        except ExtractionStorageError:
            raise
        except OSError as error:
            raise ExtractionStorageError() from error

    def cleanup(self) -> None:
        if self.closed:
            return
        try:
            if not self.published:
                for name in STAGING_FILES:
                    try:
                        os.unlink(name, dir_fd=self.claim_fd)
                    except FileNotFoundError:
                        pass
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
            for name in FINAL_FILES:
                try:
                    os.unlink(name, dir_fd=final_fd)
                except FileNotFoundError:
                    pass
            os.close(final_fd)
            final_fd = -1
            os.rmdir(str(self.extraction_id), dir_fd=self.document_fd)
            self.published = False
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
        try:
            descriptors.append(_open_directory(self.root))
            descriptors.append(_open_or_create_child(descriptors[-1], str(department)))
            descriptors.append(_open_or_create_child(descriptors[-1], str(document_id)))
            descriptors.append(_open_or_create_child(descriptors[-1], ".staging"))
            descriptors.append(_open_or_create_child(descriptors[-1], str(extraction_id)))
            os.mkdir(str(claim_token), 0o700, dir_fd=descriptors[-1])
            claim_fd = _open_child_directory(descriptors[-1], str(claim_token))
            os.fchmod(claim_fd, 0o700)
            return ExtractionStaging(
                department.value,
                document_id,
                extraction_id,
                claim_token,
                *descriptors,
                claim_fd,
            )
        except Exception as error:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(error, ExtractionStorageError):
                raise
            raise ExtractionStorageError() from error


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


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]
