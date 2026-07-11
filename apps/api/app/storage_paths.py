"""Safe department-scoped external runtime paths."""

from __future__ import annotations

from pathlib import Path

from app.authorization import DepartmentScope


def department_storage_path(data_root: Path, department: DepartmentScope, *children: str) -> Path:
    """Return a non-escaping path beneath the validated department directory."""

    resolved_root = data_root.resolve(strict=True)
    department_root = resolved_root / "departments" / str(department)
    candidate = department_root
    for child in children:
        child_path = Path(child)
        if child_path.is_absolute() or ".." in child_path.parts:
            raise ValueError("department storage child path is unsafe")
        candidate /= child_path

    resolved_candidate = candidate.resolve(strict=False)
    if not resolved_candidate.is_relative_to(department_root.resolve(strict=False)):
        raise ValueError("department storage path escapes its department root")
    if not resolved_candidate.is_relative_to(resolved_root):
        raise ValueError("department storage path escapes DEPTSLM_DATA_DIR")
    return resolved_candidate
