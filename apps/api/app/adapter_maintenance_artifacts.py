"""Descriptor-bound storage for Phase 12.1E-A reconciliation.

Only the four adapter surfaces are reachable here.  The module deliberately
does not import any model/runtime code and never follows a pathname after an
ownership check.  Partial stages are housekeeping surfaces: their marker and
payload bytes are never parsed or logged.  Final surfaces require a complete
closed manifest before they can be moved to a tombstone.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.adapter_registry_domain import AdapterRegistryDomainError, parse_registry_manifest
from app.adapter_source_artifacts import AdapterSourceArtifactError, parse_source_manifest

SOURCE_STAGE_FILES = frozenset(
    {
        ".deptslm-adapter-stage-owner",
        "intake_manifest.json",
        "adapter_config.json",
        "adapter_model.safetensors",
    }
)
REGISTRY_STAGE_FILES = frozenset(
    {
        ".deptslm-adapter-registry-stage-owner",
        "manifest.json",
        "adapter_config.json",
        "adapter_model.safetensors",
    }
)
SOURCE_FINAL_FILES = frozenset(
    {"intake_manifest.json", "adapter_config.json", "adapter_model.safetensors"}
)
REGISTRY_FINAL_FILES = frozenset(
    {"manifest.json", "adapter_config.json", "adapter_model.safetensors"}
)
SURFACES = frozenset({"source_stage", "source_final", "registry_stage", "registry_final"})
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_FILE_BYTES = 512 * 1024 * 1024
_CHUNK = 1024 * 1024


class AdapterMaintenanceArtifactError(RuntimeError):
    SAFE_CODES = frozenset(
        {
            "staging_path_unsafe",
            "artifact_ownership_mismatch",
            "artifact_manifest_invalid",
            "artifact_permissions_invalid",
            "artifact_authority_changed",
            "artifact_tombstone_conflict",
        }
    )

    def __init__(self, code: str = "artifact_ownership_mismatch") -> None:
        self.code = code if code in self.SAFE_CODES else "artifact_ownership_mismatch"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SurfaceIdentity:
    device: int
    inode: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> SurfaceIdentity:
        return cls(
            value.st_dev,
            value.st_ino,
            stat.S_IMODE(value.st_mode),
            value.st_uid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def as_json(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
            "uid": self.uid,
            "nlink": self.nlink,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }

    @classmethod
    def from_json(cls, value: object) -> SurfaceIdentity:
        required = {"device", "inode", "mode", "uid", "nlink", "size", "mtime_ns", "ctime_ns"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        try:
            values = tuple(
                int(value[key])
                for key in (
                    "device",
                    "inode",
                    "mode",
                    "uid",
                    "nlink",
                    "size",
                    "mtime_ns",
                    "ctime_ns",
                )
            )
        except (TypeError, ValueError, KeyError) as error:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch") from error
        # Directory link counts are normally greater than one (``.`` and
        # ``..`` plus children).  The identity is used for both directories
        # and regular files, so only zero/negative values are invalid here;
        # regular-file callers still enforce nlink == 1 in _require_private_file.
        if any(number < 0 for number in values) or values[4] <= 0:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        return cls(*values)


@dataclass(frozen=True, slots=True)
class BoundSurface:
    surface_type: str
    department_id: UUID
    resource_id: UUID
    attempt_id: UUID | None
    item_id: UUID
    observed_identity: dict[str, object]
    deletion_plan: list[dict[str, object]]
    tombstone_identity: dict[str, object]

    @property
    def address(self) -> PhysicalSurfaceIdentifier:
        return physical_surface_identifier(
            self.surface_type,
            self.department_id,
            self.resource_id,
            self.attempt_id,
        )


@dataclass(frozen=True, slots=True)
class PhysicalSurfaceIdentifier:
    """The only pathname identity accepted for an adapter surface."""

    surface_type: str
    department_id: UUID
    resource_id: UUID
    path_attempt_id: UUID | None


@dataclass(frozen=True, slots=True)
class InspectedSurface:
    """Closed, content-free authority captured from the original surface.

    The descriptors used to produce this value are deliberately closed before
    the caller persists it.  The value is only an observation; it is never a
    tombstone binding and cannot authorize deletion on its own.
    """

    surface_type: str
    department_id: UUID
    resource_id: UUID
    attempt_id: UUID | None
    item_id: UUID
    observed_identity: dict[str, object]
    deletion_plan: list[dict[str, object]]

    @property
    def address(self) -> PhysicalSurfaceIdentifier:
        return physical_surface_identifier(
            self.surface_type,
            self.department_id,
            self.resource_id,
            self.attempt_id,
        )


def _safe_uuid(value: UUID) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")


def physical_surface_identifier(
    surface_type: str,
    department_id: UUID,
    resource_id: UUID,
    path_attempt_id: UUID | None,
) -> PhysicalSurfaceIdentifier:
    """Build the exact physical address for one reviewed surface.

    Stage paths use the source import attempt or registry publication attempt;
    final paths never contain an attempt component.  Keeping this mapping in
    one closed helper prevents registry-attempt primary keys from becoming
    pathname components by accident.
    """

    if surface_type not in SURFACES:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    _safe_uuid(department_id)
    _safe_uuid(resource_id)
    if surface_type.endswith("_stage"):
        _safe_uuid(path_attempt_id)
    elif path_attempt_id is not None:
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    return PhysicalSurfaceIdentifier(surface_type, department_id, resource_id, path_attempt_id)


def _checked_surface_address(value: object) -> PhysicalSurfaceIdentifier:
    if not isinstance(value, PhysicalSurfaceIdentifier):
        raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
    return physical_surface_identifier(
        value.surface_type,
        value.department_id,
        value.resource_id,
        value.path_attempt_id,
    )


def _safe_name(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise AdapterMaintenanceArtifactError("staging_path_unsafe")


def _same(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_uid == second.st_uid
        and first.st_nlink == second.st_nlink
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _same_directory_identity(first: SurfaceIdentity, second: SurfaceIdentity) -> bool:
    """Compare directory identity fields that do not change on child unlink."""

    return (
        first.device == second.device
        and first.inode == second.inode
        and first.mode == second.mode
        and first.uid == second.uid
    )


def _open_dir(parent: int, name: str, *, writable: bool = True) -> int:
    _safe_name(name)
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
    except FileNotFoundError:
        raise
    except OSError as error:
        raise AdapterMaintenanceArtifactError("staging_path_unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (writable and not metadata.st_mode & stat.S_IWUSR)
        ):
            raise AdapterMaintenanceArtifactError("artifact_permissions_invalid")
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not _same(current, metadata):
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _ensure_private_dir(parent: int, name: str) -> int:
    _safe_name(name)
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    except OSError as error:
        raise AdapterMaintenanceArtifactError("artifact_permissions_invalid") from error
    return _open_dir(parent, name)


def _open_path(
    root: int, parts: tuple[str, ...], *, writable: bool = True
) -> tuple[int, list[int]]:
    current = root
    opened: list[int] = []
    try:
        for part in parts:
            child = _open_dir(current, part, writable=writable)
            opened.append(child)
            current = child
        return current, opened
    except Exception:
        for descriptor in reversed(opened):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _require_private_file(descriptor: int, *, allow_empty: bool) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (not allow_empty and metadata.st_size <= 0)
        or metadata.st_size > _MAX_FILE_BYTES
    ):
        raise AdapterMaintenanceArtifactError("artifact_permissions_invalid")
    return metadata


def _rename_no_replace(src_parent: int, src_name: str, dst_parent: int, dst_name: str) -> None:
    _safe_name(src_name)
    _safe_name(dst_name)
    libc = CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = getattr(libc, "renameatx_np", None)
        if operation is None:
            raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
        operation.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
        operation.restype = c_int
        result = operation(src_parent, src_name.encode(), dst_parent, dst_name.encode(), 0x00000004)
    else:
        operation = getattr(libc, "renameat2", None)
        if operation is None:
            raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
        operation.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
        operation.restype = c_int
        result = operation(src_parent, src_name.encode(), dst_parent, dst_name.encode(), 1)
    if result != 0:
        number = get_errno()
        raise AdapterMaintenanceArtifactError(
            "artifact_tombstone_conflict" if number in {17, 20} else "artifact_authority_changed"
        )


def _fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise AdapterMaintenanceArtifactError("artifact_authority_changed") from error


def _digest(descriptor: int) -> tuple[str, int]:
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
        if total > _MAX_FILE_BYTES:
            raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
    return digest.hexdigest(), total


class AdapterMaintenanceArtifactStore:
    """One mount-visible descriptor root for adapter maintenance surfaces.

    ``tombstone_root_name`` is intentionally explicit so Phase 12.1E-B can
    use a namespace separate from the E-A reconciliation tombstones without
    changing any E-A path or adopting an existing tombstone.
    """

    def __init__(self, data_dir: Path, *, tombstone_root_name: str = ".deleting") -> None:
        if not isinstance(data_dir, Path) or not data_dir.is_absolute() or not data_dir.is_dir():
            raise AdapterMaintenanceArtifactError("artifact_permissions_invalid")
        if not isinstance(tombstone_root_name, str) or tombstone_root_name not in {
            ".deleting",
            ".purge-deleting",
        }:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        self.tombstone_root_name = tombstone_root_name
        try:
            self.root_fd = os.open(
                os.fspath(data_dir / "adapters"),
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(self.root_fd)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise AdapterMaintenanceArtifactError("artifact_permissions_invalid")
            self._imports_fd = _open_dir(self.root_fd, "imports")
            self._registry_fd = _open_dir(self.root_fd, "registry")
            staging = _open_dir(self.root_fd, ".staging")
            self._staging_imports_fd = _open_dir(staging, "imports")
            self._staging_registry_fd = _open_dir(staging, "registry")
            deleting = _open_dir(self.root_fd, tombstone_root_name)
            self._deleting_fds = {surface: _open_dir(deleting, surface) for surface in SURFACES}
            os.close(staging)
            os.close(deleting)
            self._closed = False
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for name in (
            "_staging_registry_fd",
            "_staging_imports_fd",
            "_registry_fd",
            "_imports_fd",
            "root_fd",
        ):
            descriptor = getattr(self, name, None)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, name, None)
        for descriptor in getattr(self, "_deleting_fds", {}).values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._deleting_fds = {}
        self._closed = True

    def __enter__(self) -> AdapterMaintenanceArtifactStore:
        return self

    def __exit__(self, *_ignored: object) -> None:
        self.close()

    def _surface_parent(self, surface: str) -> tuple[int, tuple[str, ...], frozenset[str], bool]:
        if surface not in SURFACES:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        if surface.startswith("source_"):
            base = self._staging_imports_fd if surface == "source_stage" else self._imports_fd
            names = SOURCE_STAGE_FILES if surface == "source_stage" else SOURCE_FINAL_FILES
        else:
            base = self._staging_registry_fd if surface == "registry_stage" else self._registry_fd
            names = REGISTRY_STAGE_FILES if surface == "registry_stage" else REGISTRY_FINAL_FILES
        return base, (), names, surface.endswith("_stage")

    def _open_surface(
        self, address: PhysicalSurfaceIdentifier
    ) -> tuple[int, int, list[int], frozenset[str], bool] | None:
        address = _checked_surface_address(address)
        base, _unused, allowlist, is_stage = self._surface_parent(address.surface_type)
        opened: list[int] = []
        try:
            department = _open_dir(base, str(address.department_id))
            opened.append(department)
            resource_parent = department
            resource_name = str(address.resource_id)
            if is_stage:
                resource_dir = _open_dir(resource_parent, resource_name)
                opened.append(resource_dir)
                if address.path_attempt_id is None:
                    raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
                stage = _open_dir(resource_dir, str(address.path_attempt_id))
                opened.append(stage)
                return resource_dir, stage, opened, allowlist, True
            final = _open_dir(resource_parent, resource_name)
            opened.append(final)
            return resource_parent, final, opened, allowlist, False
        except FileNotFoundError:
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            return None
        except Exception:
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def _inspect(
        self,
        fd: int,
        allowlist: frozenset[str],
        *,
        stage: bool,
        expected: object | None,
        expected_manifest_sha256: str | None,
        expected_manifest_byte_size: int | None,
    ) -> tuple[dict[str, object], list[dict[str, object]]]:
        names = set(os.listdir(fd))
        if not names.issubset(allowlist) or any(name not in allowlist for name in names):
            raise AdapterMaintenanceArtifactError(
                "staging_path_unsafe" if stage else "artifact_manifest_invalid"
            )
        if not stage and names != set(allowlist):
            raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
        entries: list[dict[str, object]] = []
        for name in sorted(names):
            try:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd
                )
            except OSError as error:
                raise AdapterMaintenanceArtifactError(
                    "staging_path_unsafe" if stage else "artifact_manifest_invalid"
                ) from error
            try:
                metadata = _require_private_file(descriptor, allow_empty=stage)
                entry = SurfaceIdentity.from_stat(metadata).as_json()
                entry["name"] = name
                if not stage:
                    entry["sha256"], entry["size"] = _digest(descriptor)
                entries.append(entry)
                if not stage and name in {"intake_manifest.json", "manifest.json"}:
                    raw = os.pread(descriptor, _MAX_MANIFEST_BYTES + 1, 0)
                    if len(raw) > _MAX_MANIFEST_BYTES:
                        raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
                    try:
                        value = (
                            parse_registry_manifest(raw)
                            if name == "manifest.json"
                            else parse_source_manifest(raw)
                        )
                    except (
                        AdapterRegistryDomainError,
                        AdapterSourceArtifactError,
                        ValueError,
                        TypeError,
                    ) as error:
                        raise AdapterMaintenanceArtifactError(
                            "artifact_manifest_invalid"
                        ) from error
                    if not isinstance(value, dict) or value != expected:
                        raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
                    if expected_manifest_sha256 is not None:
                        if entry["sha256"] != expected_manifest_sha256:
                            raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
                    if expected_manifest_byte_size is not None:
                        if entry["size"] != expected_manifest_byte_size:
                            raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
            finally:
                os.close(descriptor)
        if not stage:
            expected_files = expected.get("files") if isinstance(expected, dict) else None
            if not isinstance(expected_files, dict):
                raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
            for entry in entries:
                descriptor = expected_files.get(entry["name"])
                if not isinstance(descriptor, dict):
                    if entry["name"] not in {"intake_manifest.json", "manifest.json"}:
                        raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
                    continue
                if descriptor.get("byte_size") != entry["size"] or descriptor.get(
                    "sha256"
                ) != entry.get("sha256"):
                    raise AdapterMaintenanceArtifactError("artifact_manifest_invalid")
        return {
            "directory": SurfaceIdentity.from_stat(os.fstat(fd)).as_json(),
            "entries": entries,
        }, entries

    def _verify_entries(
        self,
        fd: int,
        entries: list[dict[str, object]],
        *,
        stage: bool,
        expected_directory: dict[str, object],
    ) -> None:
        """Recheck the reviewed directory entries immediately before rename."""

        current_directory = SurfaceIdentity.from_stat(os.fstat(fd))
        if current_directory != SurfaceIdentity.from_json(expected_directory):
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        current_names = set(os.listdir(fd))
        expected_names = {entry.get("name") for entry in entries}
        if current_names != expected_names:
            raise AdapterMaintenanceArtifactError(
                "staging_path_unsafe" if stage else "artifact_manifest_invalid"
            )
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str):
                raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=fd,
                )
            except OSError as error:
                raise AdapterMaintenanceArtifactError(
                    "staging_path_unsafe" if stage else "artifact_manifest_invalid"
                ) from error
            try:
                metadata = _require_private_file(descriptor, allow_empty=stage)
                if SurfaceIdentity.from_stat(metadata) != SurfaceIdentity.from_json(entry):
                    raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                if not stage:
                    digest, size = _digest(descriptor)
                    if digest != entry.get("sha256") or size != entry.get("size"):
                        raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            finally:
                os.close(descriptor)

    def inspect_surface(
        self,
        address: PhysicalSurfaceIdentifier,
        item_id: UUID,
        *,
        expected_manifest: dict[str, object] | None,
        expected_manifest_sha256: str | None = None,
        expected_manifest_byte_size: int | None = None,
    ) -> InspectedSurface | None:
        """Capture one exact original surface without mutating storage.

        The no-follow descriptor chain remains open for the entire inspection
        and is closed only after the content-free identity and deletion plan
        have been captured.  No marker or payload bytes are parsed for stages,
        and no tombstone is created here.
        """

        _safe_uuid(item_id)
        opened = self._open_surface(address)
        if opened is None:
            return None
        parent, directory, descriptors, allowlist, stage = opened
        try:
            observed, entries = self._inspect(
                directory,
                allowlist,
                stage=stage,
                expected=expected_manifest,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_manifest_byte_size=expected_manifest_byte_size,
            )
            plan = [{"name": entry["name"], "identity": dict(entry)} for entry in entries]
            return InspectedSurface(
                address.surface_type,
                address.department_id,
                address.resource_id,
                address.path_attempt_id,
                item_id,
                observed,
                plan,
            )
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _open_tombstone_path(
        self,
        address: PhysicalSurfaceIdentifier,
        item_id: UUID,
    ) -> tuple[int, int, list[int]] | None:
        address = _checked_surface_address(address)
        _safe_uuid(item_id)
        parent = self._deleting_fds[address.surface_type]
        opened: list[int] = []
        try:
            department = _open_dir(parent, str(address.department_id))
            opened.append(department)
            resource = _open_dir(department, str(address.resource_id))
            opened.append(resource)
            item = _open_dir(resource, str(item_id))
            opened.append(item)
            return resource, item, opened
        except FileNotFoundError:
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            return None
        except Exception:
            for descriptor in reversed(opened):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    def enumerate_tombstones(self, address: PhysicalSurfaceIdentifier) -> tuple[UUID, ...]:
        """Enumerate every private UUID item tombstone for one exact resource.

        Enumeration is descriptor-relative and content-free.  Any unknown
        name, symlink, non-directory, foreign owner, or permissive directory
        fails closed rather than being ignored.
        """

        address = _checked_surface_address(address)
        parent = self._deleting_fds[address.surface_type]
        opened: list[int] = []
        try:
            department = _open_dir(parent, str(address.department_id))
            opened.append(department)
            resource = _open_dir(department, str(address.resource_id))
            opened.append(resource)
        except FileNotFoundError:
            for descriptor in reversed(opened):
                os.close(descriptor)
            return ()
        try:
            result: list[UUID] = []
            result.extend(self._enumerate_item_ids(resource))
            return tuple(result)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    @staticmethod
    def _enumerate_item_ids(resource: int) -> tuple[UUID, ...]:
        """Validate one resource tombstone directory without reading content."""

        result: list[UUID] = []
        for name in sorted(os.listdir(resource)):
            try:
                item_id = UUID(name)
            except (TypeError, ValueError):
                raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict") from None
            if item_id.int == 0 or str(item_id) != name:
                raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
            item = _open_dir(resource, name)
            os.close(item)
            result.append(item_id)
        return tuple(result)

    def enumerate_department_tombstones(
        self, surface_type: str, department_id: UUID
    ) -> tuple[tuple[UUID, UUID], ...]:
        """Enumerate every resource/item tombstone for one department.

        Reconciliation uses this broader view before confirming an attempt so
        an unknown resource namespace cannot be mistaken for an empty exact
        surface. Only canonical UUID directory names and private service-owned
        directories are accepted; no tombstone contents are opened.
        """

        if surface_type not in SURFACES:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        _safe_uuid(department_id)
        parent = self._deleting_fds[surface_type]
        opened: list[int] = []
        try:
            department = _open_dir(parent, str(department_id))
            opened.append(department)
        except FileNotFoundError:
            return ()
        try:
            result: list[tuple[UUID, UUID]] = []
            for resource_name in sorted(os.listdir(department)):
                try:
                    resource_id = UUID(resource_name)
                except (TypeError, ValueError):
                    raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict") from None
                if resource_id.int == 0 or str(resource_id) != resource_name:
                    raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
                resource = _open_dir(department, resource_name)
                try:
                    result.extend(
                        (resource_id, item_id) for item_id in self._enumerate_item_ids(resource)
                    )
                finally:
                    os.close(resource)
            return tuple(result)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def tombstone_exists(self, address: PhysicalSurfaceIdentifier, item_id: UUID) -> bool:
        """Return whether an item-scoped tombstone exists, without adopting it."""

        address = _checked_surface_address(address)
        _safe_uuid(item_id)
        return item_id in self.enumerate_tombstones(address)

    def surface_exists(self, address: PhysicalSurfaceIdentifier) -> bool:
        """Check exact original presence through the same no-follow chain."""

        address = _checked_surface_address(address)
        opened = self._open_surface(address)
        if opened is None:
            return False
        _parent, _directory, descriptors, _allowlist, _stage = opened
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        return True

    def move_verified_surface_to_tombstone(
        self,
        inspected: InspectedSurface,
        *,
        expected_tombstone_namespace: dict[str, object],
    ) -> BoundSurface | None:
        """Reopen and atomically move a previously verified exact surface.

        The caller must persist the verified observation and move intent before
        invoking this operation.  The original descriptors stay open through
        the final identity checks and no-replace rename.  A pre-existing
        item-scoped tombstone is never adopted or modified.
        """

        if not isinstance(inspected, InspectedSurface):
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        expected = {
            "surface_type": inspected.surface_type,
            "department_id": str(inspected.department_id),
            "resource_id": str(inspected.resource_id),
            "item_id": str(inspected.item_id),
        }
        if expected_tombstone_namespace != expected:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        opened = self._open_surface(
            inspected.address,
        )
        if opened is None:
            # A missing original is only recoverable through the explicit
            # move-intent path, never by manufacturing a new plan here.
            return None
        parent, directory, descriptors, _allowlist, stage = opened
        deleting_parent: int | None = None
        deleting_resource: int | None = None
        resource_parent: int | None = None
        try:
            entries = [
                dict(entry["identity"])
                for entry in inspected.deletion_plan
                if isinstance(entry, dict) and isinstance(entry.get("identity"), dict)
            ]
            observed = {
                "directory": SurfaceIdentity.from_stat(os.fstat(directory)).as_json(),
                "entries": entries,
            }
            if observed != inspected.observed_identity:
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            if entries != [entry["identity"] for entry in inspected.deletion_plan]:
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            self._verify_entries(
                directory,
                [dict(entry["identity"]) for entry in inspected.deletion_plan],
                stage=stage,
                expected_directory=inspected.observed_identity["directory"],
            )
            resource_parent = _ensure_private_dir(
                self._deleting_fds[inspected.surface_type], str(inspected.department_id)
            )
            deleting_parent = _ensure_private_dir(resource_parent, str(inspected.resource_id))
            existing_items = self._enumerate_item_ids(deleting_parent)
            if existing_items:
                raise AdapterMaintenanceArtifactError("artifact_tombstone_conflict")
            if inspected.address.path_attempt_id is None:
                source_name = str(inspected.resource_id)
            else:
                source_name = str(inspected.address.path_attempt_id)
            current_entry = os.stat(source_name, dir_fd=parent, follow_symlinks=False)
            if not _same(current_entry, os.fstat(directory)):
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            _rename_no_replace(parent, source_name, deleting_parent, str(inspected.item_id))
            _fsync(parent)
            _fsync(deleting_parent)
            moved = _open_dir(deleting_parent, str(inspected.item_id))
            deleting_resource = moved
            moved_identity = SurfaceIdentity.from_stat(os.fstat(moved)).as_json()
            if not _same_directory_identity(
                SurfaceIdentity.from_json(moved_identity),
                SurfaceIdentity.from_json(observed["directory"]),
            ):
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            plan = [{"name": entry["name"], "identity": dict(entry)} for entry in entries]
            return BoundSurface(
                inspected.surface_type,
                inspected.department_id,
                inspected.resource_id,
                inspected.attempt_id,
                inspected.item_id,
                dict(inspected.observed_identity),
                plan,
                {"directory": moved_identity, "entries": [dict(entry) for entry in entries]},
            )
        except FileNotFoundError:
            return None
        finally:
            if deleting_resource is not None:
                os.close(deleting_resource)
            if deleting_parent is not None:
                os.close(deleting_parent)
            if resource_parent is not None:
                os.close(resource_parent)
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def open_committed_tombstone(self, bound: BoundSurface) -> BoundSurface:
        """Open only a PostgreSQL-bound tombstone and verify its exact plan."""

        if not isinstance(bound, BoundSurface) or not isinstance(bound.tombstone_identity, dict):
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        opened = self._open_tombstone_path(bound.address, bound.item_id)
        if opened is None:
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        _resource, directory, descriptors = opened
        try:
            expected_directory = bound.tombstone_identity.get("directory")
            if not isinstance(expected_directory, dict):
                raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
            if not _same_directory_identity(
                SurfaceIdentity.from_stat(os.fstat(directory)),
                SurfaceIdentity.from_json(expected_directory),
            ):
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            entries = [entry.get("identity") for entry in bound.deletion_plan]
            if any(not isinstance(entry, dict) for entry in entries):
                raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
            self._verify_entries(
                directory,
                [dict(entry) for entry in entries],
                stage=bound.surface_type.endswith("_stage"),
                expected_directory=expected_directory,
            )
            return bound
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def recover_authorized_move(
        self,
        inspected: InspectedSurface,
        *,
        expected_tombstone_namespace: dict[str, object],
    ) -> BoundSurface:
        """Recover a move only from a committed pre-rename move intent.

        This path never reads a marker or constructs a plan from tombstone
        contents.  It compares the exact persisted observation and deletion
        plan against the item-scoped tombstone and is therefore safe only for
        the caller that already committed the move intent.
        """

        expected = {
            "surface_type": inspected.surface_type,
            "department_id": str(inspected.department_id),
            "resource_id": str(inspected.resource_id),
            "item_id": str(inspected.item_id),
        }
        if expected_tombstone_namespace != expected:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        opened = self._open_tombstone_path(
            inspected.address,
            inspected.item_id,
        )
        if opened is None:
            raise AdapterMaintenanceArtifactError("artifact_authority_changed")
        resource, directory, descriptors = opened
        try:
            expected_entries = [
                dict(entry["identity"])
                for entry in inspected.deletion_plan
                if isinstance(entry, dict) and isinstance(entry.get("identity"), dict)
            ]
            if len(expected_entries) != len(inspected.deletion_plan):
                raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
            current_directory = SurfaceIdentity.from_stat(os.fstat(directory)).as_json()
            expected_original_directory = inspected.observed_identity.get("directory")
            if not isinstance(expected_original_directory, dict) or not _same_directory_identity(
                SurfaceIdentity.from_json(current_directory),
                SurfaceIdentity.from_json(expected_original_directory),
            ):
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            self._verify_entries(
                directory,
                expected_entries,
                stage=inspected.surface_type.endswith("_stage"),
                expected_directory=current_directory,
            )
            return BoundSurface(
                inspected.surface_type,
                inspected.department_id,
                inspected.resource_id,
                inspected.attempt_id,
                inspected.item_id,
                dict(inspected.observed_identity),
                [
                    {"name": entry["name"], "identity": dict(entry["identity"])}
                    for entry in inspected.deletion_plan
                ],
                {"directory": current_directory, "entries": expected_entries},
            )
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _open_bound(self, bound: BoundSurface) -> tuple[int, int, list[int]]:
        parent = _open_dir(self._deleting_fds[bound.surface_type], str(bound.department_id))
        try:
            resource = _open_dir(parent, str(bound.resource_id))
            item = _open_dir(resource, str(bound.item_id))
            return parent, item, [resource, item]
        except Exception:
            os.close(parent)
            raise

    def unlink_committed_tombstone_entry(
        self, bound: BoundSurface, name: str, *, allow_missing: bool
    ) -> None:
        if name not in {entry["name"] for entry in bound.deletion_plan}:
            raise AdapterMaintenanceArtifactError("artifact_ownership_mismatch")
        parent, directory, opened = self._open_bound(bound)
        try:
            expected_directory = SurfaceIdentity.from_json(bound.tombstone_identity["directory"])
            if not _same_directory_identity(
                SurfaceIdentity.from_stat(os.fstat(directory)), expected_directory
            ):
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
            except FileNotFoundError:
                if allow_missing:
                    return
                raise AdapterMaintenanceArtifactError("artifact_authority_changed")
            try:
                actual = SurfaceIdentity.from_stat(os.fstat(descriptor))
                expected = SurfaceIdentity.from_json(
                    next(
                        entry["identity"] for entry in bound.deletion_plan if entry["name"] == name
                    )
                )
                if actual != expected:
                    raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                current_entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not _same(current_entry, os.fstat(descriptor)):
                    raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                # Keep the verified descriptor open until the unlink and
                # directory fsync complete. The descriptor is never reopened
                # through a pathname after authorization.
                os.unlink(name, dir_fd=directory)
                _fsync(directory)
            finally:
                os.close(descriptor)
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)
            os.close(parent)

    def remove_committed_tombstone_directory(
        self, bound: BoundSurface, *, allow_missing: bool = False
    ) -> None:
        resource_parent = _open_dir(
            self._deleting_fds[bound.surface_type], str(bound.department_id)
        )
        try:
            resource = _open_dir(resource_parent, str(bound.resource_id))
            try:
                try:
                    directory = _open_dir(resource, str(bound.item_id))
                except FileNotFoundError:
                    if allow_missing:
                        return
                    raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                try:
                    if os.listdir(directory):
                        raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                    expected = SurfaceIdentity.from_json(bound.tombstone_identity["directory"])
                    if not _same_directory_identity(
                        SurfaceIdentity.from_stat(os.fstat(directory)), expected
                    ):
                        raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                    current_entry = os.stat(
                        str(bound.item_id), dir_fd=resource, follow_symlinks=False
                    )
                    if not _same(current_entry, os.fstat(directory)):
                        raise AdapterMaintenanceArtifactError("artifact_authority_changed")
                    os.rmdir(str(bound.item_id), dir_fd=resource)
                    _fsync(resource)
                finally:
                    os.close(directory)
            finally:
                os.close(resource)
        finally:
            os.close(resource_parent)


class AdapterPurgeArtifactStore(AdapterMaintenanceArtifactStore):
    """Descriptor-bound adapter store using the independent purge namespace."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir, tombstone_root_name=".purge-deleting")


__all__ = [
    "AdapterMaintenanceArtifactError",
    "AdapterMaintenanceArtifactStore",
    "AdapterPurgeArtifactStore",
    "BoundSurface",
    "InspectedSurface",
    "PhysicalSurfaceIdentifier",
    "physical_surface_identifier",
    "SOURCE_STAGE_FILES",
    "REGISTRY_STAGE_FILES",
    "SOURCE_FINAL_FILES",
    "REGISTRY_FINAL_FILES",
    "SURFACES",
]
