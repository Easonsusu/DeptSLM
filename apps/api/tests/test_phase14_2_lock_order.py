"""Static sentinels for the Phase 14 execution advisory/row lock order."""

from __future__ import annotations

import ast
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def _function_nodes(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(path.read_text())
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _call_lines(
    node: ast.AST, *, attribute: str | None = None, name: str | None = None
) -> list[int]:
    lines: list[int] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        target = call.func
        if (
            attribute is not None
            and isinstance(target, ast.Attribute)
            and target.attr == attribute
        ):
            lines.append(call.lineno)
        if name is not None and isinstance(target, ast.Name) and target.id == name:
            lines.append(call.lineno)
    return sorted(lines)


def test_phase14_execution_paths_acquire_advisory_before_training_job_row() -> None:
    services = _function_nodes(APP_ROOT / "training_execution_services.py")
    queue = _function_nodes(APP_ROOT / "training_execution_queue.py")
    for functions, function_name in (
        (services, "_lock_job_first"),
        (queue, "_valid_claim"),
        (queue, "claim_next_training_execution"),
    ):
        node = functions[function_name]
        advisory = _call_lines(node, name="acquire_training_execution_serialization")
        row_lock = _call_lines(node, attribute="with_for_update")
        assert advisory, function_name
        assert row_lock, function_name
        assert advisory[0] < row_lock[0], function_name


def test_phase14_mutation_helpers_preserve_job_before_execution_order() -> None:
    services = _function_nodes(APP_ROOT / "training_execution_services.py")
    node = services["_authorize_execution_mutation"]
    assert _call_lines(node, name="_lock_job_first")[0] < _call_lines(
        node, name="_lock_execution"
    )[0]


def test_phase14_uses_one_blocking_advisory_helper_without_try_wait_split() -> None:
    production = "\n".join(path.read_text() for path in APP_ROOT.glob("training_execution*.py"))
    assert "pg_try_advisory_xact_lock" not in production
    assert production.count("pg_advisory_xact_lock") == 1
    helper = (APP_ROOT / "training_execution_locking.py").read_text()
    assert "pg_advisory_xact_lock" in helper
    assert "pg_try_advisory_xact_lock" not in helper
