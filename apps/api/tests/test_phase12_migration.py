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


def test_phase12_1c_migration_is_self_contained_and_frozen() -> None:
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0011_phase12_adapter_registry.py"
    source = path.read_text(encoding="utf-8")
    assert "app.models" not in source
    assert "app.adapter_registry" not in source
    spec = importlib.util.spec_from_file_location("phase12_1c_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0011_phase12_adapter_registry"
    assert module.down_revision == "0010_phase12_adapter_sources"
    assert "adapter_registry_attempts" in source
    assert "adapter_upstream_dependencies" in source
    assert "fk_adapter_import_source_claimed_adapter_scope" in source
