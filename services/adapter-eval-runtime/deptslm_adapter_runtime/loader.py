"""Fail-closed registry verification and ephemeral adapter loading."""

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
    AdapterContractError,
    validate_adapter_config,
    validate_safetensors_header,
)
from app.adapter_registry_domain import canonical_json_bytes, parse_registry_manifest
from app.authorization import DepartmentScope
from app.generation_contract import validate_generation_context_contract
from app.model_store import validate_generation_model_store


class AdapterRuntimeError(RuntimeError):
    SAFE_CODES = frozenset(
        {
            "candidate_adapter_load_failed",
            "adapter_artifact_missing",
            "adapter_artifact_mismatch",
            "adapter_authority_changed",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if code in self.SAFE_CODES else "candidate_adapter_load_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AdapterSessionKey:
    department_id: UUID
    adapter_id: UUID
    adapter_version: int
    base_model_revision: str
    registry_publication_attempt_id: UUID
    config_sha256: str
    config_byte_size: int
    model_sha256: str
    model_byte_size: int


@dataclass(slots=True)
class VerifiedAdapterCopy:
    directory: Path
    config_path: Path
    model_path: Path
    key: AdapterSessionKey

    def close(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=False)

    def __enter__(self) -> VerifiedAdapterCopy:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Closed identity snapshot for one reviewed source file descriptor."""

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
        return (
            metadata.st_dev == self.st_dev
            and metadata.st_ino == self.st_ino
            and metadata.st_mode == self.st_mode
            and metadata.st_uid == self.st_uid
            and metadata.st_nlink == self.st_nlink
            and metadata.st_size == self.st_size
            and metadata.st_mtime_ns == self.st_mtime_ns
            and metadata.st_ctime_ns == self.st_ctime_ns
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
    """Verify the exact registry final and copy bytes through retained descriptors."""

    try:
        scope = DepartmentScope(department_id)
        key = AdapterSessionKey(
            department_id,
            adapter_id,
            adapter_version,
            BASE_MODEL_REVISION,
            registry_publication_attempt_id,
            expected_config_sha256,
            expected_config_byte_size,
            expected_model_sha256,
            expected_model_byte_size,
        )
        if (
            type(expected_config_byte_size) is not int
            or not 1 <= expected_config_byte_size <= 65_536
            or type(expected_model_byte_size) is not int
            or not 1 <= expected_model_byte_size <= 44_040_192
        ):
            raise AdapterRuntimeError("adapter_artifact_mismatch")
        root_fd = _open_private_directory(registry_root)
        department_fd: int | None = None
        final_fd: int | None = None
        config_fd: int | None = None
        model_fd: int | None = None
        try:
            department_fd = _open_private_child(root_fd, str(scope.value))
            final_fd = _open_private_child(department_fd, str(adapter_id))
            if set(os.listdir(final_fd)) != {
                "manifest.json",
                "adapter_config.json",
                "adapter_model.safetensors",
            }:
                raise AdapterRuntimeError("adapter_authority_changed")
            manifest_fd = _open_private_file(final_fd, "manifest.json")
            try:
                manifest_meta = os.fstat(manifest_fd)
                if manifest_meta.st_size > 256 * 1024:
                    raise AdapterRuntimeError("adapter_artifact_mismatch")
                manifest_raw = os.pread(manifest_fd, manifest_meta.st_size, 0)
                manifest = parse_registry_manifest(manifest_raw)
                _verify_entry(final_fd, "manifest.json", manifest_fd)
            finally:
                os.close(manifest_fd)
            if (
                hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
                != expected_manifest_sha256
                or manifest.get("department_id") != str(department_id)
                or manifest.get("adapter_id") != str(adapter_id)
                or manifest.get("attempt_number") != registry_attempt_number
                or manifest.get("publication_attempt_id") != str(registry_publication_attempt_id)
            ):
                raise AdapterRuntimeError("adapter_authority_changed")
            manifest_files = manifest.get("files")
            if not isinstance(manifest_files, dict):
                raise AdapterRuntimeError("adapter_authority_changed")
            config_fd = _open_private_file(final_fd, "adapter_config.json")
            model_fd = _open_private_file(final_fd, "adapter_model.safetensors")
            config_identity = SourceIdentity.capture(config_fd, expected_config_byte_size)
            model_identity = SourceIdentity.capture(model_fd, expected_model_byte_size)
            config_digest = _digest_fd(config_fd)
            model_digest = _digest_fd(model_fd)
            config_meta = manifest_files.get("adapter_config.json")
            model_meta = manifest_files.get("adapter_model.safetensors")
            if (
                not isinstance(config_meta, dict)
                or config_meta.get("sha256") != expected_config_sha256
                or config_meta.get("byte_size") != expected_config_byte_size
                or not isinstance(model_meta, dict)
                or model_meta.get("sha256") != expected_model_sha256
                or model_meta.get("byte_size") != expected_model_byte_size
            ):
                raise AdapterRuntimeError("adapter_authority_changed")
            if config_digest != (expected_config_sha256, expected_config_byte_size):
                raise AdapterRuntimeError("adapter_artifact_mismatch")
            if model_digest != (expected_model_sha256, expected_model_byte_size):
                raise AdapterRuntimeError("adapter_artifact_mismatch")
            config_raw = os.pread(config_fd, expected_config_byte_size, 0)
            if len(config_raw) != expected_config_byte_size:
                raise AdapterRuntimeError("adapter_authority_changed")
            validate_adapter_config(config_raw)
            model_meta = os.fstat(model_fd)
            with os.fdopen(os.dup(model_fd), "rb", closefd=True) as model_file:
                validate_safetensors_header(model_file, model_meta.st_size)
            directory = _make_ephemeral_copy_directory()
            os.chmod(directory, 0o700)
            config_path = directory / "adapter_config.json"
            model_path = directory / "adapter_model.safetensors"
            try:
                copied_config = _copy_descriptor(
                    config_fd,
                    config_path,
                    expected_config_byte_size,
                    copy_hook=copy_hook,
                )
                copied_model = _copy_descriptor(
                    model_fd,
                    model_path,
                    expected_model_byte_size,
                    copy_hook=copy_hook,
                )
                if (
                    copied_config != (expected_config_sha256, expected_config_byte_size)
                    or copied_model != (expected_model_sha256, expected_model_byte_size)
                    or not config_identity.matches(config_fd)
                    or not model_identity.matches(model_fd)
                ):
                    raise AdapterRuntimeError("adapter_authority_changed")
                _verify_entry(final_fd, "adapter_config.json", config_fd)
                _verify_entry(final_fd, "adapter_model.safetensors", model_fd)
                if set(os.listdir(final_fd)) != {
                    "manifest.json",
                    "adapter_config.json",
                    "adapter_model.safetensors",
                }:
                    raise AdapterRuntimeError("adapter_authority_changed")
                _verify_ephemeral_copy(
                    directory,
                    expected_config_sha256,
                    expected_config_byte_size,
                    expected_model_sha256,
                    expected_model_byte_size,
                )
                return VerifiedAdapterCopy(directory, config_path, model_path, key)
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        finally:
            for descriptor in (config_fd, model_fd, final_fd, department_fd, root_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
    except AdapterRuntimeError:
        raise
    except (
        AdapterContractError,
        FileNotFoundError,
        OSError,
        ValueError,
    ) as error:
        raise AdapterRuntimeError("adapter_artifact_mismatch") from error


def load_candidate_model(
    copy: VerifiedAdapterCopy,
    data_dir: Path,
    *,
    tokenizer_limit: object | None = None,
):
    """Load one exact adapter into the private runtime; never fall back to base."""

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
            base,
            str(copy.directory),
            local_files_only=True,
            is_trainable=False,
        )
    except Exception as error:
        raise AdapterRuntimeError("candidate_adapter_load_failed") from error


def _copy_descriptor(source: int, destination: Path, size: int, *, copy_hook=None):
    if size <= 0:
        raise AdapterRuntimeError("adapter_artifact_mismatch")
    digest = hashlib.sha256()
    total = 0
    with destination.open("xb") as handle:
        if copy_hook is not None:
            copy_hook(destination, 0)
        remaining = size
        offset = 0
        while remaining:
            block = os.pread(source, min(1024 * 1024, remaining), offset)
            if not block:
                raise AdapterRuntimeError("adapter_authority_changed")
            handle.write(block)
            digest.update(block)
            offset += len(block)
            remaining -= len(block)
            total += len(block)
        if os.pread(source, 1, size):
            raise AdapterRuntimeError("adapter_authority_changed")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(destination, 0o600)
    if copy_hook is not None:
        copy_hook(destination, total)
    return digest.hexdigest(), total


def _verify_ephemeral_copy(
    directory: Path,
    config_sha256: str,
    config_size: int,
    model_sha256: str,
    model_size: int,
) -> None:
    directory_fd = _open_private_directory(directory)
    config_fd = None
    model_fd = None
    try:
        if set(os.listdir(directory_fd)) != {
            "adapter_config.json",
            "adapter_model.safetensors",
        }:
            raise AdapterRuntimeError("adapter_authority_changed")
        config_fd = _open_private_file(directory_fd, "adapter_config.json")
        model_fd = _open_private_file(directory_fd, "adapter_model.safetensors")
        if _digest_fd(config_fd) != (config_sha256, config_size):
            raise AdapterRuntimeError("adapter_authority_changed")
        if _digest_fd(model_fd) != (model_sha256, model_size):
            raise AdapterRuntimeError("adapter_authority_changed")
        config_raw = os.pread(config_fd, config_size, 0)
        validate_adapter_config(config_raw)
        model_meta = os.fstat(model_fd)
        with os.fdopen(os.dup(model_fd), "rb", closefd=True) as model_file:
            validate_safetensors_header(model_file, model_meta.st_size)
        _verify_entry(directory_fd, "adapter_config.json", config_fd)
        _verify_entry(directory_fd, "adapter_model.safetensors", model_fd)
    finally:
        for descriptor in (config_fd, model_fd, directory_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _make_ephemeral_copy_directory() -> Path:
    scratch = Path("/tmp/adapter-eval")
    try:
        metadata = scratch.lstat()
    except FileNotFoundError:
        scratch_directory = None
    else:
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise AdapterRuntimeError("adapter_authority_changed")
        scratch_directory = str(scratch)
    return Path(tempfile.mkdtemp(prefix="deptslm-adapter-eval-", dir=scratch_directory))


def _open_private_directory(path: Path) -> int:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
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
        name,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
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
    if (
        current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
        or current.st_mode != opened.st_mode
        or current.st_uid != opened.st_uid
        or current.st_nlink != opened.st_nlink
        or current.st_size != opened.st_size
        or current.st_mtime_ns != opened.st_mtime_ns
        or current.st_ctime_ns != opened.st_ctime_ns
    ):
        raise AdapterRuntimeError("adapter_authority_changed")


def _digest_fd(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    offset = 0
    total = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        offset += len(chunk)
    return digest.hexdigest(), total
