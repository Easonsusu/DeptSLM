"""Typed, non-escaping paths beneath external DeptSLM runtime storage."""

from __future__ import annotations

import os
import stat
from enum import StrEnum
from pathlib import Path

from app.authorization import DepartmentScope


class ArtifactArea(StrEnum):
    """Known top-level runtime artifact areas."""

    UPLOADS = "uploads"
    EXTRACTED_TEXT = "extracted_text"
    VECTOR_SNAPSHOTS = "vector_snapshots"
    TRAINING_DATASETS = "training_datasets"
    ADAPTERS = "adapters"
    MODEL_CACHE = "model_cache"
    EVAL_RESULTS = "eval_results"
    LOGS = "logs"
    EXPORTS = "exports"


def department_artifact_path(
    data_root: Path,
    area: ArtifactArea,
    department: DepartmentScope,
    *children: str,
) -> Path:
    """Return a lexical department path beneath a typed runtime artifact area."""

    if not isinstance(area, ArtifactArea):
        raise TypeError("artifact area must be an ArtifactArea")
    resolved_root = data_root.resolve(strict=True)
    area_root = resolved_root / area.value
    department_root = area_root / str(department)
    candidate = department_root
    for child in children:
        _validate_path_segment(child)
        candidate /= child

    normalized = Path(os.path.abspath(candidate))
    if not normalized.is_relative_to(department_root):
        raise ValueError("department artifact path escapes its department root")
    if not normalized.is_relative_to(resolved_root):
        raise ValueError("department artifact path escapes DEPTSLM_DATA_DIR")
    _reject_existing_symlinks(area_root, normalized)
    return normalized


def _validate_path_segment(value: str) -> None:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("artifact path segment is unsafe")
    if "\x00" in value or "/" in value or "\\" in value or Path(value).is_absolute():
        raise ValueError("artifact path segment is unsafe")


def _reject_existing_symlinks(area_root: Path, candidate: Path) -> None:
    current = area_root
    try:
        if stat.S_ISLNK(current.lstat().st_mode):
            raise ValueError("artifact area is a symlink")
    except FileNotFoundError:
        pass
    for part in candidate.relative_to(area_root).parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("artifact path contains a symlink")
