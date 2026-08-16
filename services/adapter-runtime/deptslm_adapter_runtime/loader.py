"""Descriptor-bound Phase 12.1A registry verification for production runtime.

The production service never gives PEFT the external registry path.  It verifies
the exact department/adapter surface through retained no-follow descriptors,
copies the bytes into a private directory, and only then permits model loading.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.adapter_contract import (
    BASE_MODEL_REVISION,
    validate_adapter_config,
    validate_safetensors_header,
)
from app.adapter_registry_domain import canonical_json_bytes, parse_registry_manifest
from app.generation_contract import validate_generation_context_contract
from app.model_store import validate_generation_model_store


class AdapterRuntimeError(RuntimeError):
    SAFE_CODES = frozenset(
        {
            "adapter_load_failed",
            "adapter_artifact_mismatch",
            "adapter_authority_changed",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if code in self.SAFE_CODES else "adapter_load_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AdapterSessionKey:
    department_id: UUID
    adapter_id: UUID
    adapter_version: int
    base_model_revision: str
    registry_publication_attempt_id: UUID
    registry_attempt_number: int
    config_sha256: str
    config_byte_size: int
    model_sha256: str
    model_byte_size: int


@dataclass(slots=True)
class VerifiedAdapterCopy:
    directory: Path
    key: AdapterSessionKey

    @property
    def config_path(self) -> Path:
        return self.directory / "adapter_config.json"

    @property
    def model_path(self) -> Path:
        return self.directory / "adapter_model.safetensors"

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=False)


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Closed identity captured for one externally verified registry file."""

    st_dev: int
    st_ino: int
    st_mode: int
    st_uid: int
    st_nlink: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int

    @classmethod
    def capture(cls, descriptor: int, expected_size: int) -> SourceIdentity:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != expected_size
            or expected_size <= 0
        ):
            raise AdapterRuntimeError("adapter_authority_changed")
        return cls(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def matches(self, descriptor: int) -> bool:
        metadata = os.fstat(descriptor)
        return all(
            getattr(metadata, field) == getattr(self, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        )


def verify_and_copy_adapter(
    registry_root: Path,
    *,
    department_id: UUID,
    adapter_id: UUID,
    adapter_version: int,
    registry_publication_attempt_id: UUID,
    registry_attempt_number: int,
    expected_manifest_sha256: str,
    expected_config_sha256: str,
    expected_config_byte_size: int,
    expected_model_sha256: str,
    expected_model_byte_size: int,
    copy_hook=None,
) -> VerifiedAdapterCopy:
    """Verify the exact final surface and copy it without reopening paths."""

    key = AdapterSessionKey(
        department_id,
        adapter_id,
        adapter_version,
        BASE_MODEL_REVISION,
        registry_publication_attempt_id,
        registry_attempt_number,
        expected_config_sha256,
        expected_config_byte_size,
        expected_model_sha256,
        expected_model_byte_size,
    )
    if not (1 <= expected_config_byte_size <= 65_536) or not (
        1 <= expected_model_byte_size <= 44_040_192
    ):
        raise AdapterRuntimeError("adapter_artifact_mismatch")
    root_fd = department_fd = final_fd = config_fd = model_fd = None
    directory: Path | None = None
    try:
        root_fd = _open_private_directory(registry_root)
        department_fd = _open_private_child(root_fd, str(department_id))
        final_fd = _open_private_child(department_fd, str(adapter_id))
        final_identity = os.fstat(final_fd)
        if set(os.listdir(final_fd)) != {
            "manifest.json",
            "adapter_config.json",
            "adapter_model.safetensors",
        }:
            raise AdapterRuntimeError("adapter_authority_changed")
        manifest_fd = _open_private_file(final_fd, "manifest.json")
        try:
            manifest_meta = os.fstat(manifest_fd)
            if not 1 <= manifest_meta.st_size <= 256 * 1024:
                raise AdapterRuntimeError("adapter_artifact_mismatch")
            manifest_raw = os.pread(manifest_fd, manifest_meta.st_size, 0)
            manifest = parse_registry_manifest(manifest_raw)
            _verify_entry(final_fd, "manifest.json", manifest_fd)
        finally:
            os.close(manifest_fd)
        if (
            hashlib.sha256(canonical_json_bytes(manifest)).hexdigest() != expected_manifest_sha256
            or manifest.get("department_id") != str(department_id)
            or manifest.get("adapter_id") != str(adapter_id)
            or manifest.get("publication_attempt_id") != str(registry_publication_attempt_id)
            or manifest.get("attempt_number") != registry_attempt_number
        ):
            raise AdapterRuntimeError("adapter_authority_changed")
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise AdapterRuntimeError("adapter_authority_changed")
        config_fd = _open_private_file(final_fd, "adapter_config.json")
        model_fd = _open_private_file(final_fd, "adapter_model.safetensors")
        config_identity = SourceIdentity.capture(config_fd, expected_config_byte_size)
        model_identity = SourceIdentity.capture(model_fd, expected_model_byte_size)
        if _digest_fd(config_fd) != (expected_config_sha256, expected_config_byte_size):
            raise AdapterRuntimeError("adapter_artifact_mismatch")
        if _digest_fd(model_fd) != (expected_model_sha256, expected_model_byte_size):
            raise AdapterRuntimeError("adapter_artifact_mismatch")
        if files.get("adapter_config.json") != {
            "sha256": expected_config_sha256,
            "byte_size": expected_config_byte_size,
        } or files.get("adapter_model.safetensors") != {
            "sha256": expected_model_sha256,
            "byte_size": expected_model_byte_size,
        }:
            raise AdapterRuntimeError("adapter_authority_changed")
        config_raw = os.pread(config_fd, expected_config_byte_size, 0)
        if len(config_raw) != expected_config_byte_size:
            raise AdapterRuntimeError("adapter_authority_changed")
        validate_adapter_config(config_raw)
        with os.fdopen(os.dup(model_fd), "rb", closefd=True) as model_file:
            validate_safetensors_header(model_file, os.fstat(model_fd).st_size)
        directory = _private_copy_directory()
        _copy_descriptor(
            config_fd,
            directory / "adapter_config.json",
            expected_config_byte_size,
            copy_hook=copy_hook,
        )
        _copy_descriptor(
            model_fd,
            directory / "adapter_model.safetensors",
            expected_model_byte_size,
            copy_hook=copy_hook,
        )
        if copy_hook is not None:
            copy_hook(directory, "after-copy")
        if not config_identity.matches(config_fd) or not model_identity.matches(model_fd):
            raise AdapterRuntimeError("adapter_authority_changed")
        _verify_copy(
            directory,
            expected_config_sha256,
            expected_config_byte_size,
            expected_model_sha256,
            expected_model_byte_size,
        )
        _verify_entry(final_fd, "adapter_config.json", config_fd)
        _verify_entry(final_fd, "adapter_model.safetensors", model_fd)
        if set(os.listdir(final_fd)) != {
            "manifest.json",
            "adapter_config.json",
            "adapter_model.safetensors",
        }:
            raise AdapterRuntimeError("adapter_authority_changed")
        _verify_entry(department_fd, str(adapter_id), final_fd)
        _verify_stat_identity(final_identity, os.fstat(final_fd))
        return VerifiedAdapterCopy(directory, key)
    except AdapterRuntimeError:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    except Exception as error:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise AdapterRuntimeError("adapter_artifact_mismatch") from error
    finally:
        for descriptor in (config_fd, model_fd, final_fd, department_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def load_adapter_model(copy: VerifiedAdapterCopy, data_dir: Path, *, tokenizer_limit=None):
    """Load the fixed local base plus the verified private adapter only."""

    try:
        from peft import PeftModel
        from transformers import AutoModelForCausalLM

        generation = validate_generation_model_store(data_dir)
        base = AutoModelForCausalLM.from_pretrained(
            str(generation.path),
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
        )
        validate_generation_context_contract(
            tokenizer_limit,
            getattr(getattr(base, "config", None), "max_position_embeddings", None),
        )
        return PeftModel.from_pretrained(
            base, str(copy.directory), local_files_only=True, is_trainable=False
        )
    except Exception as error:
        raise AdapterRuntimeError("adapter_load_failed") from error


def _private_copy_directory() -> Path:
    scratch = Path("/tmp/adapter-runtime")
    metadata = scratch.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AdapterRuntimeError("adapter_authority_changed")
    path = Path(tempfile.mkdtemp(prefix="target-", dir=scratch))
    os.chmod(path, 0o700)
    return path


def _open_private_directory(path: Path) -> int:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        _require_private_directory(descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_private_child(parent: int, name: str) -> int:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )
    try:
        _require_private_directory(descriptor)
        _verify_entry(parent, name, descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_private_file(parent: int, name: str) -> int:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise AdapterRuntimeError("adapter_authority_changed")
        _verify_entry(parent, name, descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _require_private_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AdapterRuntimeError("adapter_authority_changed")


def _verify_entry(parent: int, name: str, descriptor: int) -> None:
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    opened = os.fstat(descriptor)
    if any(
        getattr(current, field) != getattr(opened, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    ):
        raise AdapterRuntimeError("adapter_authority_changed")


def _verify_stat_identity(expected: os.stat_result, current: os.stat_result) -> None:
    if any(
        getattr(expected, field) != getattr(current, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    ):
        raise AdapterRuntimeError("adapter_authority_changed")


def _digest_fd(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    offset = 0
    total = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        total += len(chunk)
        offset += len(chunk)
    return digest.hexdigest(), total


def _copy_descriptor(
    source: int, destination: Path, size: int, *, copy_hook=None
) -> tuple[str, int]:
    digest = hashlib.sha256()
    offset = 0
    with destination.open("xb") as handle:
        if copy_hook is not None:
            copy_hook(destination, 0)
        while offset < size:
            chunk = os.pread(source, min(1024 * 1024, size - offset), offset)
            if not chunk:
                raise AdapterRuntimeError("adapter_authority_changed")
            handle.write(chunk)
            digest.update(chunk)
            offset += len(chunk)
        if os.pread(source, 1, size):
            raise AdapterRuntimeError("adapter_authority_changed")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(destination, 0o600)
    if copy_hook is not None:
        copy_hook(destination, size)
    return digest.hexdigest(), size


def _verify_copy(
    directory: Path,
    config_sha256: str,
    config_size: int,
    model_sha256: str,
    model_size: int,
) -> None:
    names = {item.name for item in directory.iterdir()}
    if names != {"adapter_config.json", "adapter_model.safetensors"}:
        raise AdapterRuntimeError("adapter_authority_changed")
    config = directory / "adapter_config.json"
    model = directory / "adapter_model.safetensors"
    if _digest_path(config) != (config_sha256, config_size) or _digest_path(model) != (
        model_sha256,
        model_size,
    ):
        raise AdapterRuntimeError("adapter_authority_changed")
    validate_adapter_config(config.read_bytes())
    with model.open("rb") as handle:
        validate_safetensors_header(handle, model.stat().st_size)


def _digest_path(path: Path) -> tuple[str, int]:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        total = 0
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
        return digest.hexdigest(), total
