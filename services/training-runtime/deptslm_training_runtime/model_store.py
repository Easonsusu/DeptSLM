"""Exact offline authority validation for the prepared Qwen3 model cache.

The validator keeps directory descriptors open while it validates and hashes
the manifest and every model file. A pathname is only a lookup key; authority
comes from the retained no-follow descriptor identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .contract import BASE_MODEL_ID, BASE_MODEL_REVISION

MANIFEST_NAME = "deptslm-model-manifest.json"
MODEL_DIRECTORY = f"qwen3-0.6b-{BASE_MODEL_REVISION}"
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".py",
        ".pyc",
        ".pyo",
        ".bin",
        ".pt",
        ".pth",
        ".pkl",
        ".pickle",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".sh",
    }
)


class ModelStoreError(RuntimeError):
    def __init__(self, code: str = "runtime_model_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _Identity:
    """The no-follow entry/descriptor identity used during one validation."""

    st_dev: int
    st_ino: int
    st_mode: int
    st_uid: int
    st_gid: int
    st_nlink: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int

    @classmethod
    def capture(cls, metadata: os.stat_result, *, regular_file: bool) -> _Identity:
        if regular_file and metadata.st_nlink != 1:
            raise ModelStoreError()
        return cls(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def matches(self, metadata: os.stat_result | _Identity) -> bool:
        if isinstance(metadata, os.stat_result):
            other = _Identity.capture(metadata, regular_file=False)
        else:
            other = metadata
        return self == other


def validate_model_directory(path: Path) -> Path:
    expected_path = Path(f"/runtime/deptslm/model_cache/{MODEL_DIRECTORY}")
    if path != expected_path or not path.is_absolute():
        raise ModelStoreError()
    root_fd = _open_absolute_directory(path)
    root_identity = _require_private_directory(root_fd)
    try:
        manifest_bytes = _read_verified_file(root_fd, MANIFEST_NAME)
        manifest = _parse_manifest(manifest_bytes)
        files = manifest["files"]
        if not isinstance(files, dict) or "model.safetensors" not in files:
            raise ModelStoreError()
        expected_directories = {""}
        for name in files:
            if isinstance(name, str):
                parts = name.split("/")
                expected_directories.update(
                    "/".join(parts[:index]) for index in range(1, len(parts))
                )
        actual: set[str] = set()
        _verify_tree(root_fd, "", files, expected_directories, actual)
        if actual != set(files):
            raise ModelStoreError()
        _verify_descriptor_identity(root_fd, root_identity)
    finally:
        os.close(root_fd)
    return path


def _open_absolute_directory(path: Path) -> int:
    """Open the fixed absolute path one no-follow component at a time."""

    descriptor = os.open("/", _directory_flags())
    try:
        for component in path.parts[1:]:
            if not component or component in {".", ".."} or "/" in component:
                raise ModelStoreError()
            entry = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            entry_identity = _require_directory_entry(entry)
            next_descriptor = os.open(
                component,
                _directory_flags(),
                dir_fd=descriptor,
            )
            # Absolute ancestors such as /runtime are mount-point plumbing;
            # only the reviewed model root and its tree directories require
            # private ownership/mode.  Their entry/FD identity is still
            # compared at every no-follow component.
            child_identity = _require_directory_descriptor(next_descriptor)
            if not entry_identity.matches(child_identity):
                os.close(next_descriptor)
                raise ModelStoreError()
            os.close(descriptor)
            descriptor = next_descriptor
        _require_private_directory(descriptor)
        return descriptor
    except (OSError, ModelStoreError) as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if isinstance(error, ModelStoreError):
            raise
        raise ModelStoreError() from error


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _require_directory_entry(metadata: os.stat_result) -> _Identity:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ModelStoreError()
    return _Identity.capture(metadata, regular_file=False)


def _require_private_directory(descriptor: int) -> _Identity:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ModelStoreError() from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise ModelStoreError()
    # st_nlink == 1 is enforced only for authoritative regular files below.
    # Directory link counts have different Unix semantics, so a directory with
    # st_nlink > 1 is valid. O_DIRECTORY/O_NOFOLLOW, private ownership/mode,
    # retained descriptors, parent-entry identity checks, and the immutable
    # tree-entry comparison provide the directory anti-substitution boundary.
    # The directory link count remains part of identity for mutation detection.
    return _Identity.capture(metadata, regular_file=False)


def _require_directory_descriptor(descriptor: int) -> _Identity:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ModelStoreError() from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ModelStoreError()
    return _Identity.capture(metadata, regular_file=False)


def _require_private_file(metadata: os.stat_result) -> _Identity:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise ModelStoreError()
    return _Identity.capture(metadata, regular_file=True)


def _verify_entry(parent_fd: int, name: str, expected: _Identity) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ModelStoreError() from error
    if not expected.matches(metadata):
        raise ModelStoreError()


def _verify_descriptor_identity(descriptor: int, expected: _Identity) -> None:
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        raise ModelStoreError() from error
    if not expected.matches(metadata):
        raise ModelStoreError()


def _parse_manifest(raw: bytes) -> dict[str, object]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelStoreError() from error
    expected = {
        "model_id": BASE_MODEL_ID,
        "revision": BASE_MODEL_REVISION,
        "library": "transformers",
        "safetensors_only": True,
        "trust_remote_code": False,
        "enable_thinking": False,
        "context_tokens": 40960,
        "maximum_input_tokens": 8192,
        "maximum_new_tokens": 512,
    }
    if not isinstance(manifest, dict) or any(
        manifest.get(key) != value for key, value in expected.items()
    ):
        raise ModelStoreError()
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or "model.safetensors" not in files:
        raise ModelStoreError()
    for name, descriptor in files.items():
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("/")
            or "\\" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or not isinstance(descriptor, dict)
            or set(descriptor) != {"size", "sha256"}
            or type(descriptor["size"]) is not int
            or descriptor["size"] < 1
            or not isinstance(descriptor["sha256"], str)
            or len(descriptor["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in descriptor["sha256"])
        ):
            raise ModelStoreError()
    return manifest


def _verify_tree(
    parent_fd: int,
    prefix: str,
    expected_files: dict[str, object],
    expected_directories: set[str],
    actual: set[str],
) -> None:
    try:
        names = sorted(os.listdir(parent_fd))
    except OSError as error:
        raise ModelStoreError() from error
    for name in names:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ModelStoreError()
        relative = f"{prefix}/{name}" if prefix else name
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise ModelStoreError() from error
        if stat.S_ISLNK(metadata.st_mode):
            raise ModelStoreError()
        if stat.S_ISDIR(metadata.st_mode):
            if relative not in expected_directories:
                raise ModelStoreError()
            entry_identity = _require_directory_entry(metadata)
            child_fd = os.open(
                name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
            try:
                child_identity = _require_private_directory(child_fd)
                if not entry_identity.matches(child_identity):
                    raise ModelStoreError()
                _verify_tree(child_fd, relative, expected_files, expected_directories, actual)
                _verify_entry(parent_fd, name, child_identity)
            finally:
                os.close(child_fd)
            continue
        if Path(name).suffix.lower() in FORBIDDEN_SUFFIXES or relative == MANIFEST_NAME:
            raise ModelStoreError()
        entry_identity = _require_private_file(metadata)
        expected_file = expected_files.get(relative)
        if (
            not isinstance(expected_file, dict)
            or expected_file.get("size") != entry_identity.st_size
        ):
            raise ModelStoreError()
        digest = _hash_verified_file(parent_fd, name, entry_identity)
        if expected_file.get("sha256") != digest:
            raise ModelStoreError()
        _verify_entry(parent_fd, name, entry_identity)
        actual.add(relative)
    try:
        final_names = sorted(os.listdir(parent_fd))
    except OSError as error:
        raise ModelStoreError() from error
    if final_names != names:
        raise ModelStoreError()


def _read_verified_file(parent_fd: int, name: str) -> bytes:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        identity = _require_private_file(metadata)
        if identity.st_size > 2 * 1024 * 1024:
            raise ModelStoreError()
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except (OSError, ModelStoreError) as error:
        if isinstance(error, ModelStoreError):
            raise
        raise ModelStoreError() from error
    try:
        actual = os.fstat(descriptor)
        if not identity.matches(actual):
            raise ModelStoreError()
        chunks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            chunks.append(block)
        value = b"".join(chunks)
        if len(value) != identity.st_size:
            raise ModelStoreError()
        _verify_entry(parent_fd, name, identity)
        return value
    except OSError as error:
        raise ModelStoreError() from error
    finally:
        os.close(descriptor)


def _hash_verified_file(parent_fd: int, name: str, expected: _Identity) -> str:
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except OSError as error:
        raise ModelStoreError() from error
    digest = hashlib.sha256()
    try:
        actual = os.fstat(descriptor)
        if not expected.matches(actual):
            raise ModelStoreError()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
    except OSError as error:
        raise ModelStoreError() from error
    finally:
        os.close(descriptor)
    _verify_entry(parent_fd, name, expected)
    return digest.hexdigest()
