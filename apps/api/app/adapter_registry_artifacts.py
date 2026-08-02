"""Descriptor-bound Phase 12.1C source, training, and registry artifacts."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.adapter_registry_domain import parse_registry_manifest
from app.authorization import DepartmentScope

REGISTRY_FINAL_FILES = frozenset(
    {"adapter_config.json", "adapter_model.safetensors", "manifest.json"}
)
REGISTRY_STAGE_MARKER = ".deptslm-adapter-registry-stage-owner"
REGISTRY_STAGE_MARKER_BYTES = b"deptslm-adapter-registry-stage-v1\n"
_MAX_MANIFEST_BYTES = 256 * 1024
_CHUNK = 1024 * 1024


class AdapterRegistryArtifactError(RuntimeError):
    SAFE_CODES = frozenset(
        {
            "adapter_source_unavailable",
            "adapter_source_artifact_mismatch",
            "adapter_source_authority_changed",
            "training_job_unavailable",
            "training_job_artifact_mismatch",
            "training_job_authority_changed",
            "adapter_registry_manifest_invalid",
            "adapter_registry_publication_failed",
            "adapter_registry_authority_changed",
            "adapter_input_unsafe",
        }
    )

    def __init__(self, code: str = "adapter_registry_publication_failed") -> None:
        self.code = code if code in self.SAFE_CODES else "adapter_registry_publication_failed"
        super().__init__(self.code)


@dataclass(slots=True)
class RetainedFinal:
    """A final directory and its allowlisted file descriptors."""

    directory_fd: int
    parent_fd: int
    resource_id: UUID
    files: tuple[tuple[str, int, os.stat_result], ...]
    read_only: bool = True
    directory_metadata: os.stat_result | None = None

    def close(self) -> None:
        descriptors = [self.directory_fd, self.parent_fd] + [fd for _name, fd, _meta in self.files]
        seen: set[int] = set()
        for descriptor in descriptors:
            if descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass

    def descriptor(self, name: str) -> tuple[int, os.stat_result]:
        for current, descriptor, metadata in self.files:
            if current == name:
                return descriptor, metadata
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")

    def read_small(self, name: str, maximum: int = _MAX_MANIFEST_BYTES) -> bytes:
        descriptor, metadata = self.descriptor(name)
        if metadata.st_size > maximum:
            raise AdapterRegistryArtifactError("adapter_registry_manifest_invalid")
        raw = os.pread(descriptor, metadata.st_size, 0)
        if len(raw) != metadata.st_size:
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        return raw

    def verify_identity(self) -> None:
        current_directory = _require_private_directory(
            self.directory_fd, writable=not self.read_only
        )
        if self.directory_metadata is not None and not _same_file(
            self.directory_metadata, current_directory
        ):
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        _entry_matches(self.parent_fd, str(self.resource_id), self.directory_fd)
        if {name for name, _fd, _meta in self.files} != {
            "intake_manifest.json",
            "adapter_config.json",
            "adapter_model.safetensors",
        } and {name for name, _fd, _meta in self.files} != {
            "manifest.json",
            "training.yaml",
            "dataset_info.json",
            "train.jsonl",
            "validation.jsonl",
        }:
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        for name, descriptor, before in self.files:
            after = os.fstat(descriptor)
            named = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
            if not _same_file(before, after) or not _same_file(before, named):
                raise AdapterRegistryArtifactError("adapter_registry_authority_changed")


@dataclass(slots=True)
class RegistryStage:
    department: DepartmentScope
    adapter_id: UUID
    publication_attempt_id: UUID
    stage_parent_fd: int
    stage_fd: int
    final_parent_fd: int
    files: tuple[tuple[str, int, os.stat_result], ...] = ()
    renamed: bool = False
    directory_metadata: os.stat_result | None = None

    def close(self) -> None:
        descriptors = [self.stage_fd, self.stage_parent_fd, self.final_parent_fd]
        descriptors += [fd for _name, fd, _meta in self.files]
        seen: set[int] = set()
        for descriptor in descriptors:
            if descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass

    def recheck(self) -> None:
        current_directory = _require_private_directory(self.stage_fd, writable=True)
        if self.directory_metadata is not None and not _same_file(
            self.directory_metadata, current_directory
        ):
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        if self.renamed:
            _entry_matches(self.final_parent_fd, str(self.adapter_id), self.stage_fd)
        else:
            _entry_matches(self.stage_parent_fd, str(self.publication_attempt_id), self.stage_fd)


class AdapterRegistryArtifactStore:
    """Owns only the fixed adapter imports/registry descriptor trees."""

    def __init__(self, data_dir: Path) -> None:
        if not isinstance(data_dir, Path) or not data_dir.is_absolute() or not data_dir.is_dir():
            raise AdapterRegistryArtifactError("adapter_input_unsafe")
        try:
            self._adapters_fd = _open_directory_path(data_dir / "adapters", writable=True)
            self._imports_fd = _open_private_child(self._adapters_fd, "imports")
            self._registry_fd = _open_private_child(self._adapters_fd, "registry")
            self._staging_fd = _open_private_child(self._adapters_fd, ".staging")
            self._staging_registry_fd = _open_private_child(self._staging_fd, "registry")
            try:
                self._training_jobs_fd = _open_directory_path(
                    data_dir / "training_datasets" / "jobs", writable=False
                )
            except OSError:
                self._training_jobs_fd = None
        except Exception:
            self.close()
            raise
        self._closed = False

    def close(self) -> None:
        for attribute in (
            "_staging_registry_fd",
            "_staging_fd",
            "_registry_fd",
            "_imports_fd",
            "_adapters_fd",
            "_training_jobs_fd",
        ):
            descriptor = getattr(self, attribute, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, attribute, None)
        self._closed = True

    def __enter__(self) -> AdapterRegistryArtifactStore:
        return self

    def __exit__(self, *_ignored: object) -> None:
        self.close()

    def open_source_final(
        self, department: DepartmentScope, source_bundle_id: UUID
    ) -> RetainedFinal:
        return self._open_final(
            self._imports_fd,
            department,
            source_bundle_id,
            frozenset({"intake_manifest.json", "adapter_config.json", "adapter_model.safetensors"}),
            read_only=True,
        )

    def open_training_job_final(
        self, department: DepartmentScope, training_job_id: UUID
    ) -> RetainedFinal:
        if self._training_jobs_fd is None:
            raise AdapterRegistryArtifactError("training_job_unavailable")
        return self._open_final(
            self._training_jobs_fd,
            department,
            training_job_id,
            frozenset(
                {
                    "manifest.json",
                    "training.yaml",
                    "dataset_info.json",
                    "train.jsonl",
                    "validation.jsonl",
                }
            ),
            read_only=True,
        )

    def open_training_job_final_from_root(
        self, training_datasets_root: Path, department: DepartmentScope, training_job_id: UUID
    ) -> RetainedFinal:
        root: int | None = None
        try:
            root = _open_directory_path(training_datasets_root / "jobs", writable=False)
            return self._open_final(
                root,
                department,
                training_job_id,
                frozenset(
                    {
                        "manifest.json",
                        "training.yaml",
                        "dataset_info.json",
                        "train.jsonl",
                        "validation.jsonl",
                    }
                ),
                read_only=True,
            )
        except AdapterRegistryArtifactError:
            raise
        except OSError as error:
            raise AdapterRegistryArtifactError("training_job_unavailable") from error
        finally:
            if root is not None:
                try:
                    os.close(root)
                except OSError:
                    pass

    def _open_final(
        self,
        root_fd: int,
        department: DepartmentScope,
        resource_id: UUID,
        allowlist: frozenset[str],
        *,
        read_only: bool,
    ) -> RetainedFinal:
        if (
            not isinstance(department, DepartmentScope)
            or not isinstance(resource_id, UUID)
            or resource_id.int == 0
        ):
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        parent = _open_private_child(root_fd, str(department.value), writable=not read_only)
        directory: int | None = None
        files: list[tuple[str, int, os.stat_result]] = []
        try:
            directory = _open_private_child(parent, str(resource_id), writable=not read_only)
            names = set(os.listdir(directory))
            if names != allowlist:
                raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
            for name in sorted(allowlist):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
                metadata = _require_private_file(descriptor, maximum=None)
                files.append((name, descriptor, metadata))
            _entry_matches(parent, str(resource_id), directory)
            return RetainedFinal(
                directory,
                parent,
                resource_id,
                tuple(files),
                read_only=read_only,
                directory_metadata=os.fstat(directory),
            )
        except AdapterRegistryArtifactError:
            _close_files(files, directory, parent)
            raise
        except OSError as error:
            _close_files(files, directory, parent)
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed") from error

    def prepare_registry_stage(
        self, department: DepartmentScope, adapter_id: UUID, publication_attempt_id: UUID
    ) -> RegistryStage:
        if (
            not isinstance(department, DepartmentScope)
            or adapter_id.int == 0
            or publication_attempt_id.int == 0
        ):
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
        stage_parent: int | None = None
        stage: int | None = None
        final_department: int | None = None
        try:
            staging_department = _ensure_private_child(
                self._staging_registry_fd, str(department.value)
            )
            stage_parent = _ensure_private_child(staging_department, str(adapter_id))
            created = False
            try:
                stage = _create_private_child(stage_parent, str(publication_attempt_id))
                created = True
            except FileExistsError:
                stage = _open_private_child(stage_parent, str(publication_attempt_id))
                names = set(os.listdir(stage))
                if names not in (set(), {REGISTRY_STAGE_MARKER}):
                    raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
            if created or REGISTRY_STAGE_MARKER not in set(os.listdir(stage)):
                _write_exact(stage, REGISTRY_STAGE_MARKER, REGISTRY_STAGE_MARKER_BYTES)
            final_department = _ensure_private_child(self._registry_fd, str(department.value))
            os.close(staging_department)
            return RegistryStage(
                department,
                adapter_id,
                publication_attempt_id,
                stage_parent,
                stage,
                final_department,
            )
        except FileExistsError:
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed") from None
        except AdapterRegistryArtifactError:
            _close_files([], stage, stage_parent, final_department)
            raise
        except OSError as error:
            _close_files([], stage, stage_parent, final_department)
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed") from error

    def publish_registry(self, staged: RegistryStage) -> RegistryStage:
        try:
            staged.recheck()
            names = set(os.listdir(staged.stage_fd))
            if names != set(REGISTRY_FINAL_FILES) | {REGISTRY_STAGE_MARKER}:
                raise AdapterRegistryArtifactError("adapter_registry_manifest_invalid")
            _verify_marker(staged.stage_fd)
            _unlink_exact(staged.stage_fd, REGISTRY_STAGE_MARKER)
            _fsync(staged.stage_fd)
            if set(os.listdir(staged.stage_fd)) != set(REGISTRY_FINAL_FILES):
                raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
            _rename_no_replace(
                staged.stage_parent_fd,
                str(staged.publication_attempt_id),
                staged.final_parent_fd,
                str(staged.adapter_id),
            )
            staged.renamed = True
            _fsync(staged.stage_parent_fd)
            _fsync(staged.final_parent_fd)
            return staged
        except AdapterRegistryArtifactError:
            raise
        except OSError as error:
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed") from error

    def verify_registry_final(self, staged: RegistryStage) -> dict[str, tuple[str, int]]:
        if not staged.renamed:
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        parent = staged.final_parent_fd
        staged.recheck()
        if set(os.listdir(staged.stage_fd)) != set(REGISTRY_FINAL_FILES):
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        raw_manifest = _read_at(staged.stage_fd, "manifest.json", _MAX_MANIFEST_BYTES)
        try:
            parse_registry_manifest(raw_manifest)
        except ValueError as error:
            raise AdapterRegistryArtifactError("adapter_registry_manifest_invalid") from error
        result: dict[str, tuple[str, int]] = {}
        retained: list[tuple[str, int, os.stat_result]] = []
        try:
            for name in sorted(REGISTRY_FINAL_FILES):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=staged.stage_fd,
                )
                try:
                    metadata = _require_private_file(descriptor, maximum=None)
                    digest, size = _digest_descriptor(descriptor)
                    if metadata.st_size != size:
                        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
                    retained.append((name, descriptor, metadata))
                    result[name] = (digest, size)
                except Exception:
                    os.close(descriptor)
                    raise
            _entry_matches(parent, str(staged.adapter_id), staged.stage_fd)
            staged.directory_metadata = os.fstat(staged.stage_fd)
            staged.files = tuple(retained)
            return result
        except Exception:
            for _name, descriptor, _metadata in retained:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def remove_owned_registry_stage(
        self, department: DepartmentScope, adapter_id: UUID, publication_attempt_id: UUID
    ) -> bool:
        """Delete only an exact private UUID stage; marker bytes are not authority."""

        parent: int | None = None
        stage: int | None = None
        try:
            department_fd = _open_private_child(self._staging_registry_fd, str(department.value))
            try:
                parent = _open_private_child(department_fd, str(adapter_id))
            finally:
                os.close(department_fd)
            stage = _open_private_child(parent, str(publication_attempt_id))
            _require_private_directory(stage, writable=True)
            _entry_matches(parent, str(publication_attempt_id), stage)
            _remove_contents(stage)
            _entry_matches(parent, str(publication_attempt_id), stage)
            os.rmdir(str(publication_attempt_id), dir_fd=parent)
            _fsync(parent)
            return True
        except FileNotFoundError:
            return False
        except (AdapterRegistryArtifactError, NotADirectoryError, OSError) as error:
            if isinstance(error, AdapterRegistryArtifactError):
                raise
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed") from error
        finally:
            _close_files([], stage, parent)


def _open_directory_path(path: Path, *, writable: bool) -> int:
    descriptor = os.open(
        os.fspath(path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        _require_private_directory(descriptor, writable=writable)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _safe_name(name: str) -> None:
    if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")


def _open_private_child(parent_fd: int, name: str, *, writable: bool = True) -> int:
    _safe_name(name)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        _require_private_directory(descriptor, writable=writable)
        _entry_matches(parent_fd, name, descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _ensure_private_child(parent_fd: int, name: str) -> int:
    _safe_name(name)
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        _fsync(parent_fd)
    except FileExistsError:
        pass
    return _open_private_child(parent_fd, name)


def _create_private_child(parent_fd: int, name: str) -> int:
    _safe_name(name)
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    _fsync(parent_fd)
    return _open_private_child(parent_fd, name)


def _require_private_directory(descriptor: int, *, writable: bool) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (writable and not metadata.st_mode & stat.S_IWUSR)
    ):
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    return metadata


def _require_private_file(descriptor: int, *, maximum: int | None) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (maximum is not None and metadata.st_size > maximum)
    ):
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
    return metadata


def _entry_matches(parent_fd: int, name: str, child_fd: int) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(child_fd)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise AdapterRegistryArtifactError("adapter_registry_authority_changed")


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_uid == second.st_uid
        and first.st_size == second.st_size
        and first.st_nlink == second.st_nlink
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _write_exact(directory_fd: int, name: str, value: bytes) -> None:
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=directory_fd
    )
    try:
        offset = 0
        while offset < len(value):
            offset += os.write(descriptor, value[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_at(directory_fd: int, name: str, maximum: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        metadata = _require_private_file(descriptor, maximum=maximum)
        raw = os.pread(descriptor, metadata.st_size, 0)
        if len(raw) != metadata.st_size:
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed")
        return raw
    finally:
        os.close(descriptor)


def _verify_marker(directory_fd: int) -> None:
    raw = _read_at(directory_fd, REGISTRY_STAGE_MARKER, len(REGISTRY_STAGE_MARKER_BYTES))
    if raw != REGISTRY_STAGE_MARKER_BYTES:
        raise AdapterRegistryArtifactError("adapter_registry_publication_failed")


def _unlink_exact(directory_fd: int, name: str) -> None:
    _safe_name(name)
    os.unlink(name, dir_fd=directory_fd)


def _remove_contents(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        _safe_name(name)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise AdapterRegistryArtifactError("adapter_registry_authority_changed") from error
        try:
            _require_private_file(descriptor, maximum=None)
        finally:
            os.close(descriptor)
        os.unlink(name, dir_fd=directory_fd)
    _fsync(directory_fd)


def _digest_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    offset = 0
    while True:
        block = os.pread(descriptor, _CHUNK, offset)
        if not block:
            break
        digest.update(block)
        total += len(block)
        offset += len(block)
    return digest.hexdigest(), total


def _rename_no_replace(
    source_parent_fd: int, source_name: str, destination_parent_fd: int, destination_name: str
) -> None:
    _safe_name(source_name)
    _safe_name(destination_name)
    libc = CDLL(None, use_errno=True)
    source = source_name.encode("ascii")
    destination = destination_name.encode("ascii")
    if sys.platform == "darwin":
        operation = getattr(libc, "renameatx_np", None)
        if operation is None:
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
        operation.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
        operation.restype = c_int
        result = operation(source_parent_fd, source, destination_parent_fd, destination, 0x00000004)
    else:
        operation = getattr(libc, "renameat2", None)
        if operation is None:
            raise AdapterRegistryArtifactError("adapter_registry_publication_failed")
        operation.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
        operation.restype = c_int
        result = operation(source_parent_fd, source, destination_parent_fd, destination, 1)
    if result != 0:
        number = get_errno()
        raise OSError(number or 1, os.strerror(number or 1))


def _fsync(descriptor: int) -> None:
    os.fsync(descriptor)


def _close_files(files: list[tuple[str, int, os.stat_result]], *descriptors: int | None) -> None:
    seen: set[int] = set()
    for _name, descriptor, _metadata in files:
        seen.add(descriptor)
        try:
            os.close(descriptor)
        except OSError:
            pass
    for descriptor in descriptors:
        if descriptor is None or descriptor in seen:
            continue
        seen.add(descriptor)
        try:
            os.close(descriptor)
        except OSError:
            pass


__all__ = [
    "REGISTRY_FINAL_FILES",
    "REGISTRY_STAGE_MARKER",
    "REGISTRY_STAGE_MARKER_BYTES",
    "AdapterRegistryArtifactError",
    "RetainedFinal",
    "RegistryStage",
    "AdapterRegistryArtifactStore",
]
