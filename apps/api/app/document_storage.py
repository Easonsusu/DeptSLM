"""Descriptor-relative storage for staged and finalized document bytes."""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.authorization import DepartmentScope
from app.storage_paths import ArtifactArea, department_artifact_path


class DocumentStorageError(RuntimeError):
    """Raised when upload storage cannot safely complete an operation."""


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)


@dataclass(slots=True)
class StagedDocument:
    data_root: Path
    department: DepartmentScope
    upload_id: UUID
    uploads_fd: int
    department_fd: int
    staging_fd: int
    file_fd: int | None
    finalized_document_id: UUID | None = None
    closed: bool = False

    @property
    def staging_path(self) -> Path:
        return department_artifact_path(
            self.data_root,
            ArtifactArea.UPLOADS,
            self.department,
            ".staging",
            f"{self.upload_id}.part",
        )

    def write(self, chunk: bytes) -> None:
        if self.file_fd is None:
            raise DocumentStorageError("staging file is not writable")
        try:
            view = memoryview(chunk)
            while view:
                written = os.write(self.file_fd, view)
                if written <= 0:
                    raise DocumentStorageError("staging write failed")
                view = view[written:]
        except OSError as error:
            raise DocumentStorageError("staging write failed") from error

    def finish(self) -> None:
        if self.file_fd is None:
            raise DocumentStorageError("staging file is not open")
        try:
            os.fsync(self.file_fd)
            os.close(self.file_fd)
            self.file_fd = None
            os.fsync(self.staging_fd)
        except OSError as error:
            raise DocumentStorageError("staging sync failed") from error

    def finalize(self, document_id: UUID) -> Path:
        if self.file_fd is not None or self.finalized_document_id is not None:
            raise DocumentStorageError("staging file is not ready for finalization")
        directory_name = str(document_id)
        try:
            os.mkdir(directory_name, 0o700, dir_fd=self.department_fd)
        except FileExistsError as error:
            raise DocumentStorageError("document destination already exists") from error
        final_fd: int | None = None
        try:
            final_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=self.department_fd)
            _require_directory(final_fd)
            os.fchmod(final_fd, 0o700)
            os.rename(
                f"{self.upload_id}.part",
                "source",
                src_dir_fd=self.staging_fd,
                dst_dir_fd=final_fd,
            )
            source_fd = os.open(
                "source", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=final_fd
            )
            try:
                if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                    raise DocumentStorageError("finalized upload is not a regular file")
                os.fchmod(source_fd, 0o600)
                os.fsync(source_fd)
            finally:
                os.close(source_fd)
            os.fsync(final_fd)
            os.fsync(self.department_fd)
        except Exception as error:
            if final_fd is not None:
                try:
                    os.unlink("source", dir_fd=final_fd)
                except FileNotFoundError:
                    pass
            try:
                os.rmdir(directory_name, dir_fd=self.department_fd)
            except OSError:
                pass
            if isinstance(error, DocumentStorageError):
                raise
            raise DocumentStorageError("unable to finalize document storage") from error
        finally:
            if final_fd is not None:
                os.close(final_fd)
        self.finalized_document_id = document_id
        return department_artifact_path(
            self.data_root,
            ArtifactArea.UPLOADS,
            self.department,
            directory_name,
            "source",
        )

    def abort(self) -> None:
        try:
            if self.file_fd is not None:
                try:
                    os.close(self.file_fd)
                finally:
                    self.file_fd = None
            try:
                os.unlink(f"{self.upload_id}.part", dir_fd=self.staging_fd)
            except FileNotFoundError:
                pass
        except OSError as error:
            raise DocumentStorageError("unable to remove staged document") from error
        finally:
            self.release()

    def compensate(self) -> None:
        try:
            document_id = self.finalized_document_id
            if document_id is not None:
                directory_name = str(document_id)
                final_fd: int | None = None
                try:
                    final_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=self.department_fd)
                    try:
                        os.unlink("source", dir_fd=final_fd)
                    except FileNotFoundError:
                        pass
                except FileNotFoundError:
                    pass
                finally:
                    if final_fd is not None:
                        os.close(final_fd)
                try:
                    os.rmdir(directory_name, dir_fd=self.department_fd)
                except FileNotFoundError:
                    pass
                self.finalized_document_id = None
            else:
                try:
                    os.unlink(f"{self.upload_id}.part", dir_fd=self.staging_fd)
                except FileNotFoundError:
                    pass
        except OSError as error:
            raise DocumentStorageError("unable to compensate document storage") from error
        finally:
            self.release()

    def release(self) -> None:
        if self.closed:
            return
        self.closed = True
        for descriptor in (self.staging_fd, self.department_fd, self.uploads_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


class DocumentStorage:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve(strict=True)
        self.uploads_root = self.data_root / ArtifactArea.UPLOADS.value
        metadata = self.uploads_root.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DocumentStorageError("uploads root is not a real directory")

    def create_staging(self, department: DepartmentScope, upload_id: UUID) -> StagedDocument:
        uploads_fd: int | None = None
        department_fd: int | None = None
        staging_fd: int | None = None
        file_fd: int | None = None
        try:
            uploads_fd = os.open(self.uploads_root, _DIRECTORY_FLAGS)
            department_fd = _open_or_create_directory(uploads_fd, str(department))
            staging_fd = _open_or_create_directory(department_fd, ".staging")
            file_fd = os.open(
                f"{upload_id}.part",
                _FILE_FLAGS,
                0o600,
                dir_fd=staging_fd,
            )
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise DocumentStorageError("staging upload is not a regular file")
            os.fchmod(file_fd, 0o600)
            return StagedDocument(
                data_root=self.data_root,
                department=department,
                upload_id=upload_id,
                uploads_fd=uploads_fd,
                department_fd=department_fd,
                staging_fd=staging_fd,
                file_fd=file_fd,
            )
        except Exception as error:
            if file_fd is not None:
                os.close(file_fd)
                if staging_fd is not None:
                    try:
                        os.unlink(f"{upload_id}.part", dir_fd=staging_fd)
                    except OSError:
                        pass
            for descriptor in (staging_fd, department_fd, uploads_fd):
                if descriptor is not None:
                    os.close(descriptor)
            if isinstance(error, DocumentStorageError):
                raise
            raise DocumentStorageError("unable to create safe staging storage") from error


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise DocumentStorageError(
                "runtime storage contains an unsafe path component"
            ) from error
        raise
    try:
        _require_directory(descriptor)
        os.fchmod(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _require_directory(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise DocumentStorageError("runtime storage path is not a directory")
