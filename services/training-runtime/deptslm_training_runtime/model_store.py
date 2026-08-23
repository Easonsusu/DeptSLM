"""Exact offline authority validation for the prepared Qwen3 model cache."""

from __future__ import annotations

import hashlib
import json
import os
import stat
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


def validate_model_directory(path: Path) -> Path:
    expected_path = Path(f"/runtime/deptslm/model_cache/{MODEL_DIRECTORY}")
    if path != expected_path or not path.is_absolute():
        raise ModelStoreError()
    _private_directory(path)
    manifest_path = path / MANIFEST_NAME
    try:
        manifest_stat = manifest_path.lstat()
        if (
            stat.S_ISLNK(manifest_stat.st_mode)
            or not stat.S_ISREG(manifest_stat.st_mode)
            or manifest_stat.st_nlink != 1
        ):
            raise ModelStoreError()
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
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
    if not isinstance(files, dict) or "model.safetensors" not in files:
        raise ModelStoreError()
    actual: set[str] = set()
    for item in path.rglob("*"):
        relative = item.relative_to(path).as_posix()
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ModelStoreError()
        if stat.S_ISDIR(metadata.st_mode):
            _private_directory(item)
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or item.suffix.lower() in FORBIDDEN_SUFFIXES
            or relative == MANIFEST_NAME
        ):
            raise ModelStoreError()
        expected_file = files.get(relative)
        if not isinstance(expected_file, dict) or expected_file.get("size") != metadata.st_size:
            raise ModelStoreError()
        digest = _hash_file(item, metadata)
        if expected_file.get("sha256") != digest:
            raise ModelStoreError()
        actual.add(relative)
    if actual != set(files):
        raise ModelStoreError()
    return path


def _private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ModelStoreError() from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise ModelStoreError()


def _hash_file(path: Path, expected: os.stat_result) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    digest = hashlib.sha256()
    try:
        actual = os.fstat(descriptor)
        if (
            actual.st_dev != expected.st_dev
            or actual.st_ino != expected.st_ino
            or actual.st_size != expected.st_size
            or actual.st_nlink != 1
        ):
            raise ModelStoreError()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
    except OSError as error:
        raise ModelStoreError() from error
    finally:
        os.close(descriptor)
    return digest.hexdigest()
