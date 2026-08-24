"""Descriptor-bound model-cache verification tests."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
from deptslm_training_runtime import model_store
from deptslm_training_runtime.model_store import (
    ModelStoreError,
    _read_verified_file,
    _verify_tree,
)


def _verify(root: Path, files: dict[str, object]) -> None:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directories = {""}
        for name in files:
            parts = name.split("/")
            directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
        _verify_tree(descriptor, "", files, directories, set())
    finally:
        os.close(descriptor)


def test_exact_regular_files_and_private_nested_directories_are_accepted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    nested = root / "weights"
    nested.mkdir(mode=0o700)
    assert nested.stat().st_nlink > 1
    payload = b"synthetic-model-bytes"
    target = nested / "model.safetensors"
    target.write_bytes(payload)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _verify(
        root,
        {
            "weights/model.safetensors": {
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        },
    )


def test_unexpected_directory_symlink_and_hardlink_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    payload = root / "model.safetensors"
    payload.write_bytes(b"bytes")
    payload.chmod(stat.S_IRUSR | stat.S_IWUSR)
    descriptor = {"model.safetensors": {"size": 5, "sha256": hashlib.sha256(b"bytes").hexdigest()}}

    (root / "unexpected").mkdir(mode=0o700)
    with pytest.raises(ModelStoreError):
        _verify(root, descriptor)

    (root / "unexpected").rmdir()

    (root / "link").symlink_to(payload)
    with pytest.raises(ModelStoreError):
        _verify(root, descriptor)
    (root / "link").unlink()

    hardlink = root / "hardlink"
    hardlink.hardlink_to(payload)
    with pytest.raises(ModelStoreError):
        _verify(root, descriptor)


def test_nested_directory_substitution_after_open_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    nested = root / "weights"
    nested.mkdir(mode=0o700)
    payload = nested / "model.safetensors"
    payload.write_bytes(b"bytes")
    payload.chmod(stat.S_IRUSR | stat.S_IWUSR)
    descriptor = {
        "weights/model.safetensors": {
            "size": 5,
            "sha256": hashlib.sha256(b"bytes").hexdigest(),
        }
    }
    original_verify_entry = model_store._verify_entry
    parked = root / "weights.original"
    substituted = False

    def substitute(parent_fd: int, name: str, expected: object) -> None:
        nonlocal substituted
        if name == "weights" and not substituted:
            nested.rename(parked)
            nested.mkdir(mode=0o700)
            substituted = True
        original_verify_entry(parent_fd, name, expected)  # type: ignore[arg-type]

    monkeypatch.setattr(model_store, "_verify_entry", substitute)
    with pytest.raises(ModelStoreError):
        _verify(root, descriptor)
    assert substituted
    assert parked.is_dir()
    assert (parked / "model.safetensors").read_bytes() == b"bytes"
    assert nested.is_dir()
    assert not any(nested.iterdir())


def test_model_file_substitution_after_hash_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    payload = root / "model.safetensors"
    payload.write_bytes(b"bytes")
    payload.chmod(stat.S_IRUSR | stat.S_IWUSR)
    descriptor = {"model.safetensors": {"size": 5, "sha256": hashlib.sha256(b"bytes").hexdigest()}}
    original_verify_entry = model_store._verify_entry
    parked = root / "model.original"
    substituted = False

    def substitute(parent_fd: int, name: str, expected: object) -> None:
        nonlocal substituted
        if name == "model.safetensors" and not substituted:
            payload.rename(parked)
            payload.write_bytes(b"other")
            payload.chmod(stat.S_IRUSR | stat.S_IWUSR)
            substituted = True
        original_verify_entry(parent_fd, name, expected)  # type: ignore[arg-type]

    monkeypatch.setattr(model_store, "_verify_entry", substitute)
    with pytest.raises(ModelStoreError):
        _verify(root, descriptor)
    assert substituted
    assert parked.read_bytes() == b"bytes"
    assert payload.read_bytes() == b"other"


def test_model_file_mutation_after_hash_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    payload = root / "model.safetensors"
    payload.write_bytes(b"bytes")
    payload.chmod(stat.S_IRUSR | stat.S_IWUSR)
    descriptor = {"model.safetensors": {"size": 5, "sha256": hashlib.sha256(b"bytes").hexdigest()}}
    original_hash = model_store._hash_verified_file

    def mutate(parent_fd: int, name: str, expected: object) -> str:
        digest = original_hash(parent_fd, name, expected)  # type: ignore[arg-type]
        payload.write_bytes(b"other")
        payload.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return digest

    monkeypatch.setattr(model_store, "_hash_verified_file", mutate)
    with pytest.raises(ModelStoreError):
        _verify(root, descriptor)


def test_directory_entry_mutation_during_validation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    payload = root / "model.safetensors"
    payload.write_bytes(b"bytes")
    payload.chmod(stat.S_IRUSR | stat.S_IWUSR)
    descriptor = {"model.safetensors": {"size": 5, "sha256": hashlib.sha256(b"bytes").hexdigest()}}
    original_hash = model_store._hash_verified_file
    added = root / "late-entry"

    def mutate(parent_fd: int, name: str, expected: object) -> str:
        digest = original_hash(parent_fd, name, expected)  # type: ignore[arg-type]
        added.write_bytes(b"late")
        added.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return digest

    monkeypatch.setattr(model_store, "_hash_verified_file", mutate)
    with pytest.raises(ModelStoreError):
        _verify(root, descriptor)
    assert added.exists()


def test_manifest_substitution_after_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "model"
    root.mkdir(mode=0o700)
    manifest = root / "deptslm-model-manifest.json"
    manifest.write_bytes(b'{"model_id":"Qwen/Qwen3-0.6B"}')
    manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)
    original_verify_entry = model_store._verify_entry
    parked = root / "manifest.original"
    substituted = False

    def substitute(parent_fd: int, name: str, expected: object) -> None:
        nonlocal substituted
        if name == "deptslm-model-manifest.json" and not substituted:
            manifest.rename(parked)
            manifest.write_bytes(b"replacement")
            manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)
            substituted = True
        original_verify_entry(parent_fd, name, expected)  # type: ignore[arg-type]

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    monkeypatch.setattr(model_store, "_verify_entry", substitute)
    try:
        with pytest.raises(ModelStoreError):
            _read_verified_file(descriptor, manifest.name)
    finally:
        os.close(descriptor)
    assert substituted
    assert parked.read_bytes() == b'{"model_id":"Qwen/Qwen3-0.6B"}'
    assert manifest.read_bytes() == b"replacement"
