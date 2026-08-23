"""Descriptor-relative private output inspection used by the runtime."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutputEvidence:
    fingerprint: str
    file_count: int
    total_bytes: int


def inspect_output_stage(
    descriptor: int,
    *,
    max_files: int = 4096,
    max_total_bytes: int = 8 * 1024 * 1024 * 1024,
    max_file_bytes: int = 2 * 1024 * 1024 * 1024,
    max_depth: int = 16,
) -> OutputEvidence:
    root = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(root.st_mode)
        or stat.S_IMODE(root.st_mode) != 0o700
        or root.st_uid != os.getuid()
    ):
        raise ValueError("output_invalid")
    entries: list[tuple[str, int, str]] = []
    total = 0

    def walk(parent: int, prefix: str, depth: int) -> None:
        nonlocal total
        if depth > max_depth:
            raise ValueError("output_limit_exceeded")
        for name in sorted(os.listdir(parent)):
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("output_invalid")
            relative = f"{prefix}/{name}" if prefix else name
            metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("output_invalid")
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
                    raise ValueError("output_invalid")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    actual = os.fstat(child)
                    if actual.st_dev != metadata.st_dev or actual.st_ino != metadata.st_ino:
                        raise ValueError("output_invalid")
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("output_invalid")
            if metadata.st_size > max_file_bytes:
                raise ValueError("output_limit_exceeded")
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            digest = hashlib.sha256()
            size = 0
            try:
                actual = os.fstat(child)
                if (
                    actual.st_dev != metadata.st_dev
                    or actual.st_ino != metadata.st_ino
                    or actual.st_nlink != 1
                ):
                    raise ValueError("output_invalid")
                while block := os.read(child, 1024 * 1024):
                    size += len(block)
                    if size > max_file_bytes or total + size > max_total_bytes:
                        raise ValueError("output_limit_exceeded")
                    digest.update(block)
            finally:
                os.close(child)
            if size != metadata.st_size:
                raise ValueError("output_invalid")
            total += size
            entries.append((relative, size, digest.hexdigest()))
            if len(entries) > max_files:
                raise ValueError("output_limit_exceeded")

    walk(descriptor, "", 0)
    encoded = json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode()
    return OutputEvidence(hashlib.sha256(encoded).hexdigest(), len(entries), total)
