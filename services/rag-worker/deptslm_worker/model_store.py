"""Validated offline model-cache boundary for the pinned embedding model."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
)

MANIFEST_NAME = "deptslm-model-manifest.json"
MODEL_DIRECTORY = f"qwen3-embedding-0.6b-{EMBEDDING_MODEL_REVISION}"
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


def model_directory(data_dir: Path) -> Path:
    return data_dir / "model_cache" / MODEL_DIRECTORY


def validate_model_store(data_dir: Path) -> ModelLocation:
    root = data_dir / "model_cache"
    location = model_directory(data_dir)
    _real_directory(root)
    _real_directory(location)
    manifest_path = location / MANIFEST_NAME
    try:
        manifest_metadata = manifest_path.lstat()
        if stat.S_ISLNK(manifest_metadata.st_mode) or not stat.S_ISREG(manifest_metadata.st_mode):
            raise ModelStoreError("embedding_model_unavailable")
        manifest = json.loads(_read_file(manifest_path, manifest_metadata).decode("utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ModelStoreError("embedding_model_unavailable") from error
    expected = {
        "model_id": EMBEDDING_MODEL_ID,
        "revision": EMBEDDING_MODEL_REVISION,
        "dimension": EMBEDDING_DIMENSION,
        "library": "sentence-transformers",
        "pooling": "model-provided last-token pooling",
        "normalized": True,
        "trust_remote_code": False,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise ModelStoreError("embedding_model_unavailable")
    files = manifest.get("files")
    if not isinstance(files, dict) or "model.safetensors" not in files:
        raise ModelStoreError("embedding_model_unavailable")
    actual_names: set[str] = set()
    for path in location.rglob("*"):
        relative = path.relative_to(location).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ModelStoreError("embedding_model_unavailable")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ModelStoreError("embedding_model_unavailable")
        if relative == MANIFEST_NAME:
            continue
        actual_names.add(relative)
        expected_file = files.get(relative)
        if not isinstance(expected_file, dict):
            raise ModelStoreError("embedding_model_unavailable")
        if expected_file.get("size") != metadata.st_size:
            raise ModelStoreError("embedding_model_unavailable")
        if expected_file.get("sha256") != _sha256(path, metadata):
            raise ModelStoreError("embedding_model_unavailable")
    if actual_names != set(files):
        raise ModelStoreError("embedding_model_unavailable")
    return ModelLocation(location, EMBEDDING_MODEL_REVISION)


def build_manifest(location: Path) -> dict:
    files: dict[str, dict[str, int | str]] = {}
    for path in location.rglob("*"):
        relative = path.relative_to(location).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or (
            not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode)
        ):
            raise ModelStoreError("embedding_model_unavailable")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if path.suffix.lower() in FORBIDDEN_SUFFIXES or relative == MANIFEST_NAME:
            raise ModelStoreError("embedding_model_unavailable")
        files[relative] = {
            "size": metadata.st_size,
            "sha256": _sha256(path, metadata),
        }
    if "model.safetensors" not in files:
        raise ModelStoreError("embedding_model_unavailable")
    return {
        "model_id": EMBEDDING_MODEL_ID,
        "revision": EMBEDDING_MODEL_REVISION,
        "dimension": EMBEDDING_DIMENSION,
        "library": "sentence-transformers",
        "pooling": "model-provided last-token pooling",
        "normalized": True,
        "trust_remote_code": False,
        "files": files,
    }


def _real_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ModelStoreError("embedding_model_unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ModelStoreError("embedding_model_unavailable")


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
        raise ModelStoreError("embedding_model_unavailable")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        actual = os.fstat(descriptor)
    except OSError as error:
        raise ModelStoreError("embedding_model_unavailable") from error
    if (
        not stat.S_ISREG(actual.st_mode)
        or actual.st_dev != expected.st_dev
        or actual.st_ino != expected.st_ino
        or actual.st_size != expected.st_size
        or actual.st_nlink != 1
    ):
        os.close(descriptor)
        raise ModelStoreError("embedding_model_unavailable")
    return descriptor
