"""Descriptor-bound storage for Phase 12.1B adapter source bundles.

This module intentionally stores the two externally supplied bytes unchanged,
plus a DeptSLM-generated content-free manifest.  It never imports a model or
deserializes a tensor.  All mutable paths are below the pre-existing private
``DEPTSLM_DATA_DIR/adapters`` directory and all publication is no-replace.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.adapter_contract import (
    ADAPTER_CONFIG_CONTRACT_VERSION,
    ADAPTER_INTAKE_CONTRACT_VERSION,
    ADAPTER_SOURCE_CONTRACT_VERSION,
    ADAPTER_TENSOR_CONTRACT_VERSION,
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    MAX_ADAPTER_FILE_BYTES,
    MAX_CONFIG_BYTES,
    PEFT_FORMAT_REFERENCE_VERSION,
    SAFE_ERROR_CODES,
    SAFETENSORS_FORMAT_REFERENCE_VERSION,
)
from app.authorization import DepartmentScope

FINAL_FILES = frozenset(
    {"intake_manifest.json", "adapter_config.json", "adapter_model.safetensors"}
)
STAGE_MARKER = ".deptslm-adapter-stage-owner"
_MARKER_BYTES = b"deptslm-adapter-stage-v1\n"
_CHUNK_BYTES = 1024 * 1024
_MANIFEST_KEYS = frozenset(
    {
        "source_contract_version",
        "intake_contract_version",
        "config_contract_version",
        "tensor_contract_version",
        "department_id",
        "source_bundle_id",
        "import_attempt_id",
        "publication_attempt_id",
        "attempt_number",
        "imported_by_user_id",
        "code_revision",
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "peft_version",
        "safetensors_format",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
        "files",
    }
)


class AdapterSourceArtifactError(RuntimeError):
    """Fixed, content-free artifact/storage error."""

    SAFE_CODES = frozenset(
        set(SAFE_ERROR_CODES)
        | {
            "adapter_input_invalid",
            "adapter_input_unsafe",
            "adapter_source_changed",
            "adapter_source_publication_failed",
            "adapter_source_authority_changed",
            "department_unavailable",
            "requester_unauthorized",
            "database_unavailable",
        }
    )

    def __init__(self, code: str = "adapter_source_publication_failed") -> None:
        self.code = code if code in self.SAFE_CODES else "adapter_source_publication_failed"
        super().__init__(self.code)


@dataclass(slots=True)
class ExternalAdapterInput:
    """One retained descriptor; the source pathname is not retained."""

    descriptor: int
    size: int
    device: int
    inode: int
    name: str
    mode: int
    uid: int
    nlink: int

    def close(self) -> None:
        try:
            os.close(self.descriptor)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class AdapterArtifactDigest:
    sha256: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class DescriptorAuthority:
    """Retained, content-free identity for one published descriptor."""

    name: str
    device: int
    inode: int
    mode: int
    uid: int
    nlink: int
    byte_size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PublishedAdapterAuthority:
    """Post-rename authority retained without reopening final pathnames."""

    directory_device: int
    directory_inode: int
    directory_mode: int
    directory_uid: int
    directory_nlink: int
    directory_mtime_ns: int
    directory_ctime_ns: int
    entries: tuple[DescriptorAuthority, ...]


@dataclass(slots=True)
class StagedAdapterSource:
    department: DepartmentScope
    source_bundle_id: UUID
    import_attempt_id: UUID
    publication_attempt_id: UUID
    attempt_number: int
    stage_parent_fd: int
    stage_fd: int
    final_parent_fd: int
    file_descriptors: tuple[tuple[str, int, os.stat_result], ...]
    digests: tuple[tuple[str, AdapterArtifactDigest], ...]
    published_authority: PublishedAdapterAuthority | None = None
    renamed: bool = False

    def close(self) -> None:
        descriptors = [descriptor for _name, descriptor, _meta in self.file_descriptors]
        descriptors.extend((self.stage_fd, self.stage_parent_fd, self.final_parent_fd))
        seen: set[int] = set()
        for descriptor in descriptors:
            if descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass

    @property
    def digest_map(self) -> dict[str, AdapterArtifactDigest]:
        return dict(self.digests)

    def recheck_identity(self) -> None:
        """Recheck retained identities without reopening any pathname."""

        _require_private_directory(self.stage_fd, writable=True)
        _entry_matches(self.final_parent_fd, str(self.source_bundle_id), self.stage_fd)
        names = set(os.listdir(self.stage_fd))
        if names != set(FINAL_FILES):
            raise AdapterSourceArtifactError("adapter_source_authority_changed")
        expected = self.digest_map
        for name, descriptor, before in self.file_descriptors:
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=self.stage_fd, follow_symlinks=False)
            if not _same_file(before, after) or not _same_file(before, current):
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
            if expected[name].byte_size != after.st_size:
                raise AdapterSourceArtifactError("adapter_source_authority_changed")

    def recheck_retained_authority(self) -> None:
        """Recheck retained post-rename descriptor identities without hashing."""

        authority = self.published_authority
        if authority is None or not self.renamed:
            raise AdapterSourceArtifactError("adapter_source_authority_changed")
        directory = _require_private_directory(self.stage_fd, writable=True)
        if (
            directory.st_dev != authority.directory_device
            or directory.st_ino != authority.directory_inode
            or stat.S_IMODE(directory.st_mode) != authority.directory_mode
            or directory.st_uid != authority.directory_uid
            or directory.st_nlink != authority.directory_nlink
            or directory.st_mtime_ns != authority.directory_mtime_ns
            or directory.st_ctime_ns != authority.directory_ctime_ns
        ):
            raise AdapterSourceArtifactError("adapter_source_authority_changed")
        _entry_matches(self.final_parent_fd, str(self.source_bundle_id), self.stage_fd)
        names = set(os.listdir(self.stage_fd))
        if names != set(FINAL_FILES) or {entry.name for entry in authority.entries} != set(
            FINAL_FILES
        ):
            raise AdapterSourceArtifactError("adapter_source_authority_changed")
        descriptors = {name: descriptor for name, descriptor, _metadata in self.file_descriptors}
        digests = self.digest_map
        if set(descriptors) != set(FINAL_FILES) or set(digests) != set(FINAL_FILES):
            raise AdapterSourceArtifactError("adapter_source_authority_changed")
        for entry in authority.entries:
            descriptor = descriptors.get(entry.name)
            expected = digests.get(entry.name)
            if (
                descriptor is None
                or expected is None
                or expected != AdapterArtifactDigest(entry.sha256, entry.byte_size)
            ):
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
            retained = os.fstat(descriptor)
            named = os.stat(entry.name, dir_fd=self.stage_fd, follow_symlinks=False)
            if not _descriptor_matches(entry, retained) or not _descriptor_matches(entry, named):
                raise AdapterSourceArtifactError("adapter_source_authority_changed")

    def verify_published_digests(self) -> None:
        """Hash the retained final descriptors once after the no-replace rename."""

        expected = self.digest_map
        entries: list[DescriptorAuthority] = []
        for name, descriptor, before in self.file_descriptors:
            digest = hashlib.sha256()
            offset = 0
            while True:
                block = os.pread(descriptor, _CHUNK_BYTES, offset)
                if not block:
                    break
                digest.update(block)
                offset += len(block)
            current = os.fstat(descriptor)
            if (
                not _same_file(before, current)
                or offset != expected[name].byte_size
                or digest.hexdigest() != expected[name].sha256
            ):
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
            entries.append(
                DescriptorAuthority(
                    name=name,
                    device=current.st_dev,
                    inode=current.st_ino,
                    mode=stat.S_IMODE(current.st_mode),
                    uid=current.st_uid,
                    nlink=current.st_nlink,
                    byte_size=current.st_size,
                    mtime_ns=current.st_mtime_ns,
                    ctime_ns=current.st_ctime_ns,
                    sha256=digest.hexdigest(),
                )
            )
        self.published_authority = self._capture_published_authority(entries)

    def _capture_published_authority(
        self, entries: list[DescriptorAuthority]
    ) -> PublishedAdapterAuthority:
        """Bind every final directory entry to its retained descriptor."""

        _entry_matches(self.final_parent_fd, str(self.source_bundle_id), self.stage_fd)
        names = set(os.listdir(self.stage_fd))
        if names != set(FINAL_FILES) or {entry.name for entry in entries} != set(FINAL_FILES):
            raise AdapterSourceArtifactError("adapter_source_authority_changed")
        descriptors = {name: descriptor for name, descriptor, _metadata in self.file_descriptors}
        if set(descriptors) != set(FINAL_FILES):
            raise AdapterSourceArtifactError("adapter_source_authority_changed")
        for entry in entries:
            descriptor = descriptors.get(entry.name)
            if descriptor is None:
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
            retained = os.fstat(descriptor)
            named = os.stat(entry.name, dir_fd=self.stage_fd, follow_symlinks=False)
            if not _descriptor_matches(entry, retained) or not _descriptor_matches(entry, named):
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
        directory = _require_private_directory(self.stage_fd, writable=True)
        return PublishedAdapterAuthority(
            directory_device=directory.st_dev,
            directory_inode=directory.st_ino,
            directory_mode=stat.S_IMODE(directory.st_mode),
            directory_uid=directory.st_uid,
            directory_nlink=directory.st_nlink,
            directory_mtime_ns=directory.st_mtime_ns,
            directory_ctime_ns=directory.st_ctime_ns,
            entries=tuple(sorted(entries, key=lambda item: item.name)),
        )


class AdapterSourceArtifactStore:
    """Open the external adapters root once and publish exact UUID surfaces."""

    def __init__(self, data_dir: Path) -> None:
        if not isinstance(data_dir, Path) or not data_dir.is_absolute() or not data_dir.is_dir():
            raise AdapterSourceArtifactError("adapter_input_unsafe")
        try:
            self._root_fd = _open_directory_path(data_dir / "adapters", writable=True)
        except OSError as error:
            raise AdapterSourceArtifactError("adapter_input_unsafe") from error
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor = getattr(self, "_root_fd", None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> AdapterSourceArtifactStore:
        return self

    def __exit__(self, *_ignored: object) -> None:
        self.close()

    @property
    def root_fd(self) -> int:
        if self._closed:
            raise AdapterSourceArtifactError("adapter_input_unsafe")
        return self._root_fd

    def open_external_inputs(
        self, config_path: Path, model_path: Path
    ) -> tuple[ExternalAdapterInput, ExternalAdapterInput]:
        """Open exactly two regular source files once and retain both handles."""

        config = self._open_external(config_path, "adapter_config.json", MAX_CONFIG_BYTES)
        try:
            model = self._open_external(
                model_path, "adapter_model.safetensors", MAX_ADAPTER_FILE_BYTES
            )
        except Exception:
            config.close()
            raise
        if (config.device, config.inode) == (model.device, model.inode):
            config.close()
            model.close()
            raise AdapterSourceArtifactError("adapter_input_unsafe")
        return config, model

    def _open_external(self, path: Path, expected_name: str, maximum: int) -> ExternalAdapterInput:
        if not isinstance(path, Path) or path.name != expected_name:
            raise AdapterSourceArtifactError("adapter_input_invalid")
        try:
            descriptor = os.open(
                os.fspath(path),
                os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise AdapterSourceArtifactError("adapter_input_invalid") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or not 1 <= metadata.st_size <= maximum
        ):
            os.close(descriptor)
            raise AdapterSourceArtifactError("adapter_input_unsafe")
        return ExternalAdapterInput(
            descriptor=descriptor,
            size=metadata.st_size,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            name=expected_name,
            mode=stat.S_IMODE(metadata.st_mode),
            uid=metadata.st_uid,
            nlink=metadata.st_nlink,
        )

    def stage(
        self,
        department: DepartmentScope,
        source_bundle_id: UUID,
        import_attempt_id: UUID,
        publication_attempt_id: UUID,
        attempt_number: int,
        config: ExternalAdapterInput,
        model: ExternalAdapterInput,
        manifest: dict[str, object],
        expected_config: AdapterArtifactDigest | None = None,
        expected_model: AdapterArtifactDigest | None = None,
    ) -> StagedAdapterSource:
        """Copy the two retained descriptors into an exclusive private stage."""

        _validate_uuid_components(department.value, source_bundle_id, import_attempt_id)
        if type(attempt_number) is not int or attempt_number <= 0:
            raise AdapterSourceArtifactError("adapter_input_invalid")
        final_parent = None
        stage_parent = None
        stage_fd = None
        files: list[tuple[str, int, os.stat_result]] = []
        try:
            imports = _ensure_private_child(self.root_fd, "imports")
            staging = _ensure_private_child(self.root_fd, ".staging")
            stage_imports = _ensure_private_child(staging, "imports")
            stage_department = _ensure_private_child(stage_imports, str(department.value))
            stage_parent = _ensure_private_child(stage_department, str(source_bundle_id))
            stage_fd = _create_private_child(stage_parent, str(import_attempt_id))
            final_root = _ensure_private_child(imports, str(department.value))
            # The final source directory itself is the no-replace rename target;
            # retain its department parent, and never pre-create the target.
            final_parent = final_root
            for descriptor in (imports, staging, stage_imports, stage_department, final_root):
                if descriptor != final_parent:
                    os.close(descriptor)

            _validate_manifest(manifest)
            if (
                manifest["department_id"] != str(department.value)
                or manifest["source_bundle_id"] != str(source_bundle_id)
                or manifest["import_attempt_id"] != str(import_attempt_id)
                or manifest["publication_attempt_id"] != str(publication_attempt_id)
                or manifest["attempt_number"] != attempt_number
            ):
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
            expected_files = manifest.get("files")
            if not isinstance(expected_files, dict) or set(expected_files) != {
                "adapter_config.json",
                "adapter_model.safetensors",
            }:
                raise AdapterSourceArtifactError("adapter_source_publication_failed")
            manifest_config = _manifest_digest(expected_files, "adapter_config.json")
            manifest_model = _manifest_digest(expected_files, "adapter_model.safetensors")
            expected_config = expected_config or manifest_config
            expected_model = expected_model or manifest_model
            if expected_config != manifest_config or expected_model != manifest_model:
                raise AdapterSourceArtifactError("adapter_source_authority_changed")
            _write_exact(stage_fd, STAGE_MARKER, _MARKER_BYTES)
            config_digest = _copy_external(
                config, stage_fd, "adapter_config.json", expected=expected_config
            )
            model_digest = _copy_external(
                model, stage_fd, "adapter_model.safetensors", expected=expected_model
            )
            if not _manifest_digest_matches(expected_files, "adapter_config.json", config_digest):
                raise AdapterSourceArtifactError("adapter_source_changed")
            if not _manifest_digest_matches(
                expected_files, "adapter_model.safetensors", model_digest
            ):
                raise AdapterSourceArtifactError("adapter_source_changed")
            manifest_bytes = _canonical_manifest(manifest)
            manifest_digest = _write_exact(stage_fd, "intake_manifest.json", manifest_bytes)
            _fsync(stage_fd)
            _fsync(stage_parent)
            names = set(os.listdir(stage_fd))
            if names != set(FINAL_FILES) | {STAGE_MARKER}:
                raise AdapterSourceArtifactError("adapter_source_publication_failed")
            for name in sorted(FINAL_FILES):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=stage_fd,
                )
                metadata = _require_private_file(descriptor)
                files.append((name, descriptor, metadata))
            return StagedAdapterSource(
                department=department,
                source_bundle_id=source_bundle_id,
                import_attempt_id=import_attempt_id,
                publication_attempt_id=publication_attempt_id,
                attempt_number=attempt_number,
                stage_parent_fd=stage_parent,
                stage_fd=stage_fd,
                final_parent_fd=final_parent,
                file_descriptors=tuple(files),
                digests=(
                    ("adapter_config.json", config_digest),
                    ("adapter_model.safetensors", model_digest),
                    ("intake_manifest.json", manifest_digest),
                ),
            )
        except AdapterSourceArtifactError:
            _close_many(files, stage_fd, stage_parent, final_parent)
            raise
        except (OSError, ValueError, TypeError) as error:
            _close_many(files, stage_fd, stage_parent, final_parent)
            raise AdapterSourceArtifactError("adapter_source_publication_failed") from error

    def publish(self, staged: StagedAdapterSource) -> StagedAdapterSource:
        """Publish one stage by an atomic no-replace directory rename."""

        try:
            _verify_stage_marker(staged.stage_fd)
            os.unlink(STAGE_MARKER, dir_fd=staged.stage_fd)
            _fsync(staged.stage_fd)
            if set(os.listdir(staged.stage_fd)) != set(FINAL_FILES):
                raise AdapterSourceArtifactError("adapter_source_publication_failed")
            _rename_no_replace(
                staged.stage_parent_fd,
                str(staged.import_attempt_id),
                staged.final_parent_fd,
                str(staged.source_bundle_id),
            )
            staged.renamed = True
            _fsync(staged.stage_parent_fd)
            _fsync(staged.final_parent_fd)
            staged.verify_published_digests()
            return staged
        except AdapterSourceArtifactError:
            raise
        except (OSError, ValueError) as error:
            raise AdapterSourceArtifactError("adapter_source_publication_failed") from error


def _validate_uuid_components(department: UUID, source: UUID, attempt: UUID) -> None:
    if any(
        not isinstance(value, UUID) or value.int == 0 for value in (department, source, attempt)
    ):
        raise AdapterSourceArtifactError("adapter_input_invalid")


def _canonical_manifest(value: dict[str, object]) -> bytes:
    if not isinstance(value, dict):
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def parse_source_manifest(raw: bytes | bytearray | memoryview | str) -> dict[str, object]:
    """Parse the exact closed Phase 12.1B manifest bytes.

    Reconciliation must validate the bytes retained on the final surface, not
    merely compare a JSON object reconstructed from PostgreSQL.  Duplicate
    keys, unknown fields, non-canonical ordering, and alternate encodings are
    rejected before the reviewed manifest contract is applied.
    """

    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    elif isinstance(raw, (bytearray, memoryview)):
        raw = bytes(raw)
    if not isinstance(raw, bytes) or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise AdapterSourceArtifactError("adapter_source_publication_failed")

    def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AdapterSourceArtifactError("adapter_source_publication_failed")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw[:-1].decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                AdapterSourceArtifactError("adapter_source_publication_failed")
            ),
        )
    except (UnicodeDecodeError, UnicodeError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, AdapterSourceArtifactError):
            raise
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error
    if not isinstance(value, dict) or _canonical_manifest(value) != raw:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    _validate_manifest(value)
    return value


def _validate_manifest(value: dict[str, object]) -> None:
    if set(value) != set(_MANIFEST_KEYS):
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    if (
        value.get("source_contract_version") != ADAPTER_SOURCE_CONTRACT_VERSION
        or value.get("intake_contract_version") != ADAPTER_INTAKE_CONTRACT_VERSION
        or value.get("config_contract_version") != ADAPTER_CONFIG_CONTRACT_VERSION
        or value.get("tensor_contract_version") != ADAPTER_TENSOR_CONTRACT_VERSION
        or value.get("base_model_id") != BASE_MODEL_ID
        or value.get("base_model_revision") != BASE_MODEL_REVISION
        or value.get("base_model_license") != BASE_MODEL_LICENSE
        or value.get("peft_version") != PEFT_FORMAT_REFERENCE_VERSION
        or value.get("safetensors_format") != SAFETENSORS_FORMAT_REFERENCE_VERSION
        or type(value.get("attempt_number")) is not int
        or value.get("attempt_number", 0) <= 0
        or value.get("tensor_dtype") not in {"F16", "BF16", "F32"}
        or value.get("tensor_count") != 392
        or value.get("tensor_element_count") != 10_092_544
        or value.get("tensor_payload_byte_size") not in {20_185_088, 40_370_176}
        or (
            value.get("tensor_dtype") in {"F16", "BF16"}
            and value.get("tensor_payload_byte_size") != 20_185_088
        )
        or (
            value.get("tensor_dtype") == "F32"
            and value.get("tensor_payload_byte_size") != 40_370_176
        )
    ):
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    for key in (
        "department_id",
        "source_bundle_id",
        "import_attempt_id",
        "publication_attempt_id",
        "imported_by_user_id",
    ):
        raw = value.get(key)
        try:
            if not isinstance(raw, str) or UUID(raw).int == 0 or str(UUID(raw)) != raw:
                raise ValueError
        except (TypeError, ValueError):
            raise AdapterSourceArtifactError("adapter_source_publication_failed") from None
    code_revision = value.get("code_revision")
    if (
        not isinstance(code_revision, str)
        or len(code_revision) != 40
        or code_revision != code_revision.lower()
        or any(char not in "0123456789abcdef" for char in code_revision)
    ):
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    files = value.get("files")
    if not isinstance(files, dict) or set(files) != {
        "adapter_config.json",
        "adapter_model.safetensors",
    }:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    for descriptor in files.values():
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"sha256", "byte_size"}
            or not isinstance(descriptor.get("sha256"), str)
            or len(descriptor["sha256"]) != 64
            or descriptor["sha256"] != descriptor["sha256"].lower()
            or type(descriptor.get("byte_size")) is not int
            or descriptor["byte_size"] <= 0
        ):
            raise AdapterSourceArtifactError("adapter_source_publication_failed")


def _open_directory_path(path: Path, *, writable: bool) -> int:
    descriptor = os.open(
        os.fspath(path),
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _require_private_directory(descriptor, writable=writable)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _require_private_directory(descriptor: int, *, writable: bool) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or (writable and not metadata.st_mode & stat.S_IWUSR)
    ):
        raise AdapterSourceArtifactError("adapter_input_unsafe")
    return metadata


def _require_private_file(descriptor: int) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077
        or metadata.st_size <= 0
    ):
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    return metadata


def _verify_stage_marker(stage_fd: int) -> None:
    """Require the exact private housekeeping marker before publication."""

    if set(os.listdir(stage_fd)) != set(FINAL_FILES) | {STAGE_MARKER}:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    try:
        descriptor = os.open(
            STAGE_MARKER,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=stage_fd,
        )
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != len(_MARKER_BYTES)
            or os.pread(descriptor, len(_MARKER_BYTES), 0) != _MARKER_BYTES
        ):
            raise AdapterSourceArtifactError("adapter_source_publication_failed")
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _safe_name(name: str) -> None:
    if not name or "/" in name or name in {".", ".."}:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")


def _ensure_private_child(parent_fd: int, name: str) -> int:
    _safe_name(name)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            _fsync(parent_fd)
        except FileExistsError:
            pass
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            _require_private_directory(descriptor, writable=True)
            _entry_matches(parent_fd, name, descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error


def _create_private_child(parent_fd: int, name: str) -> int:
    _safe_name(name)
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        _fsync(parent_fd)
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            _require_private_directory(descriptor, writable=True)
            _entry_matches(parent_fd, name, descriptor)
            return descriptor
        except Exception:
            os.close(descriptor)
            raise
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error


def _entry_matches(parent_fd: int, name: str, child_fd: int) -> None:
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    opened = os.fstat(child_fd)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise AdapterSourceArtifactError("adapter_source_authority_changed")


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_uid == second.st_uid
        and first.st_size == second.st_size
        and first.st_nlink == second.st_nlink
    )


def _descriptor_matches(expected: DescriptorAuthority, metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == expected.device
        and metadata.st_ino == expected.inode
        and stat.S_IMODE(metadata.st_mode) == expected.mode
        and metadata.st_uid == expected.uid
        and metadata.st_nlink == expected.nlink
        and metadata.st_size == expected.byte_size
        and metadata.st_mtime_ns == expected.mtime_ns
        and metadata.st_ctime_ns == expected.ctime_ns
    )


def _copy_external(
    source: ExternalAdapterInput,
    directory_fd: int,
    name: str,
    *,
    expected: AdapterArtifactDigest,
) -> AdapterArtifactDigest:
    try:
        destination = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error
    digest = hashlib.sha256()
    total = 0
    try:
        before = os.fstat(source.descriptor)
        if not _source_identity(source, before):
            raise AdapterSourceArtifactError("adapter_source_changed")
        offset = 0
        while offset < source.size:
            block = os.pread(source.descriptor, min(_CHUNK_BYTES, source.size - offset), offset)
            if not block:
                raise AdapterSourceArtifactError("adapter_source_changed")
            offset += len(block)
            total += len(block)
            digest.update(block)
            view = memoryview(block)
            written = 0
            while written < len(block):
                count = os.write(destination, view[written:])
                if count <= 0:
                    raise AdapterSourceArtifactError("adapter_source_publication_failed")
                written += count
        after = os.fstat(source.descriptor)
        result = AdapterArtifactDigest(digest.hexdigest(), total)
        if total != source.size or not _source_identity(source, after) or result != expected:
            raise AdapterSourceArtifactError("adapter_source_changed")
        os.fsync(destination)
        current = os.fstat(destination)
        if current.st_size != source.size:
            raise AdapterSourceArtifactError("adapter_source_publication_failed")
        source_digest = _digest_source(source)
        if source_digest != expected:
            raise AdapterSourceArtifactError("adapter_source_changed")
        return result
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error
    finally:
        try:
            os.close(destination)
        except OSError:
            pass


def _source_identity(source: ExternalAdapterInput, metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == source.device
        and metadata.st_ino == source.inode
        and metadata.st_size == source.size
        and stat.S_IMODE(metadata.st_mode) == source.mode
        and metadata.st_uid == source.uid
        and metadata.st_nlink == source.nlink == 1
    )


def _digest_source(source: ExternalAdapterInput) -> AdapterArtifactDigest:
    digest = hashlib.sha256()
    offset = 0
    while offset < source.size:
        block = os.pread(source.descriptor, min(_CHUNK_BYTES, source.size - offset), offset)
        if not block:
            raise AdapterSourceArtifactError("adapter_source_changed")
        digest.update(block)
        offset += len(block)
    if not _source_identity(source, os.fstat(source.descriptor)):
        raise AdapterSourceArtifactError("adapter_source_changed")
    return AdapterArtifactDigest(digest.hexdigest(), offset)


def _manifest_digest_matches(
    files: dict[str, object], name: str, digest: AdapterArtifactDigest
) -> bool:
    value = files.get(name)
    return (
        isinstance(value, dict)
        and set(value) == {"sha256", "byte_size"}
        and value.get("sha256") == digest.sha256
        and value.get("byte_size") == digest.byte_size
    )


def _manifest_digest(files: dict[str, object], name: str) -> AdapterArtifactDigest:
    value = files.get(name)
    if (
        not isinstance(value, dict)
        or set(value) != {"sha256", "byte_size"}
        or not isinstance(value.get("sha256"), str)
        or len(value["sha256"]) != 64
        or type(value.get("byte_size")) is not int
        or value["byte_size"] <= 0
    ):
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    return AdapterArtifactDigest(value["sha256"], value["byte_size"])


def _write_exact(directory_fd: int, name: str, value: bytes) -> AdapterArtifactDigest:
    _safe_name(name)
    if not isinstance(value, bytes) or not value:
        raise AdapterSourceArtifactError("adapter_source_publication_failed")
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(value)
            written = 0
            while written < len(value):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise AdapterSourceArtifactError("adapter_source_publication_failed")
                written += count
            os.fsync(descriptor)
            metadata = _require_private_file(descriptor)
            return AdapterArtifactDigest(hashlib.sha256(value).hexdigest(), metadata.st_size)
        finally:
            os.close(descriptor)
    except AdapterSourceArtifactError:
        raise
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error


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
            raise AdapterSourceArtifactError("adapter_source_publication_failed")
        operation.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
        operation.restype = c_int
        result = operation(source_parent_fd, source, destination_parent_fd, destination, 0x00000004)
    else:
        operation = getattr(libc, "renameat2", None)
        if operation is None:
            raise AdapterSourceArtifactError("adapter_source_publication_failed")
        operation.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
        operation.restype = c_int
        result = operation(source_parent_fd, source, destination_parent_fd, destination, 1)
    if result != 0:
        number = get_errno()
        raise OSError(number or 1, os.strerror(number or 1))


def _fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_publication_failed") from error


def _close_many(files: list[tuple[str, int, os.stat_result]], *descriptors: int | None) -> None:
    seen: set[int] = set()
    for _name, descriptor, _metadata in files:
        if descriptor in seen:
            continue
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
    "FINAL_FILES",
    "STAGE_MARKER",
    "AdapterSourceArtifactError",
    "ExternalAdapterInput",
    "AdapterArtifactDigest",
    "DescriptorAuthority",
    "PublishedAdapterAuthority",
    "StagedAdapterSource",
    "AdapterSourceArtifactStore",
]
