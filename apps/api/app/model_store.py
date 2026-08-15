"""Shared validated model-store boundaries used by production and candidate runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.rag_domain import (
    GENERATION_MODEL_CONTEXT_TOKENS,
    GENERATION_MODEL_ID,
    GENERATION_MODEL_REVISION,
    GENERATION_NEW_TOKEN_RESERVE,
    MAX_GENERATION_INPUT_TOKENS,
)

MANIFEST_NAME = "deptslm-model-manifest.json"
GENERATION_MODEL_DIRECTORY = f"qwen3-0.6b-{GENERATION_MODEL_REVISION}"
FORBIDDEN_SUFFIXES = {
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


class ModelStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelLocation:
    path: Path
    revision: str


def generation_model_directory(data_dir: Path) -> Path:
    return data_dir / "model_cache" / GENERATION_MODEL_DIRECTORY


def validate_generation_model_store(data_dir: Path) -> ModelLocation:
    expected = {
        "model_id": GENERATION_MODEL_ID,
        "revision": GENERATION_MODEL_REVISION,
        "library": "transformers",
        "safetensors_only": True,
        "trust_remote_code": False,
        "enable_thinking": False,
        "context_tokens": GENERATION_MODEL_CONTEXT_TOKENS,
        "maximum_input_tokens": MAX_GENERATION_INPUT_TOKENS,
        "maximum_new_tokens": GENERATION_NEW_TOKEN_RESERVE,
    }
    return _validate_store(
        data_dir,
        generation_model_directory(data_dir),
        expected,
        GENERATION_MODEL_REVISION,
    )


def _validate_store(
    data_dir: Path, location: Path, expected: dict[str, object], revision: str
) -> ModelLocation:
    _real_directory(data_dir / "model_cache")
    _real_directory(location)
    manifest_path = location / MANIFEST_NAME
    try:
        manifest_metadata = manifest_path.lstat()
        if stat.S_ISLNK(manifest_metadata.st_mode) or not stat.S_ISREG(manifest_metadata.st_mode):
            raise ModelStoreError("generation_model_unavailable")
        manifest = json.loads(_read_file(manifest_path, manifest_metadata).decode("utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ModelStoreError("generation_model_unavailable") from error
    if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in expected.items()):
        raise ModelStoreError("generation_model_unavailable")
    files = manifest.get("files")
    if not isinstance(files, dict) or "model.safetensors" not in files:
        raise ModelStoreError("generation_model_unavailable")
    actual_names: set[str] = set()
    try:
        paths = tuple(location.rglob("*"))
    except OSError as error:
        raise ModelStoreError("generation_model_unavailable") from error
    for path in paths:
        relative = path.relative_to(location).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ModelStoreError("generation_model_unavailable")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ModelStoreError("generation_model_unavailable")
        if relative == MANIFEST_NAME:
            continue
        actual_names.add(relative)
        expected_file = files.get(relative)
        if not isinstance(expected_file, dict):
            raise ModelStoreError("generation_model_unavailable")
        if expected_file.get("size") != metadata.st_size:
            raise ModelStoreError("generation_model_unavailable")
        if expected_file.get("sha256") != _sha256(path, metadata):
            raise ModelStoreError("generation_model_unavailable")
    if actual_names != set(files):
        raise ModelStoreError("generation_model_unavailable")
    return ModelLocation(location, revision)


def _real_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ModelStoreError("generation_model_unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ModelStoreError("generation_model_unavailable")


def _read_file(path: Path, expected: os.stat_result) -> bytes:
    descriptor = _open_verified(path, expected)
    try:
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sha256(path: Path, expected: os.stat_result) -> str:
    digest = hashlib.sha256()
    descriptor = _open_verified(path, expected)
    try:
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _open_verified(path: Path, expected: os.stat_result) -> int:
    if expected.st_nlink != 1:
        raise ModelStoreError("generation_model_unavailable")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        actual = os.fstat(descriptor)
    except OSError as error:
        raise ModelStoreError("generation_model_unavailable") from error
    if (
        not stat.S_ISREG(actual.st_mode)
        or actual.st_dev != expected.st_dev
        or actual.st_ino != expected.st_ino
        or actual.st_size != expected.st_size
        or actual.st_nlink != 1
    ):
        os.close(descriptor)
        raise ModelStoreError("generation_model_unavailable")
    return descriptor
