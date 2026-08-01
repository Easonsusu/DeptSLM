"""Historical immutability checks for the Phase 12.1B migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_phase12_migration_is_self_contained() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0010_phase12_adapter_sources.py"
    source = path.read_text(encoding="utf-8")
    assert "app.adapter_contract" not in source
    spec = importlib.util.spec_from_file_location("phase12_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0010_phase12_adapter_sources"
    assert module.down_revision == "0009_phase11_training_jobs"
