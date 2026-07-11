"""Pure mandatory department scope for future vector retrieval."""

from __future__ import annotations

from dataclasses import dataclass

from app.authorization import DepartmentScope


@dataclass(frozen=True, slots=True)
class DepartmentVectorScope:
    department: DepartmentScope

    def __post_init__(self) -> None:
        if not isinstance(self.department, DepartmentScope):
            raise ValueError("department vector scope requires a DepartmentScope")

    def payload_filter(self) -> dict[str, object]:
        return {"must": [{"key": "department_id", "match": {"value": str(self.department)}}]}
