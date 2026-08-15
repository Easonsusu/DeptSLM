from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0016_phase12_adapter_governance.py"
    spec = spec_from_file_location("phase12_3_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def test_phase12_3_migration_is_one_head_and_does_not_edit_phase12_2():
    path, module = _module()
    source = path.read_text(encoding="utf-8")
    assert module.revision == "0016_phase12_adapter_governance"
    assert module.down_revision == "0015_phase12_adapter_evaluation"
    assert "0014_phase12_adapter_purge" not in source
    for table in (
        "adapter_reviews",
        "department_adapter_deployments",
        "adapter_deployment_operations",
        "adapter_deployment_events",
        "adapter_rollback_retentions",
    ):
        assert f'"{table}"' in source


def test_phase12_3_migration_keeps_content_and_runtime_data_out_of_schema():
    path, _ = _module()
    source = path.read_text(encoding="utf-8")
    for forbidden in (
        'sa.Column("question"',
        'sa.Column("answer"',
        'sa.Column("prompt"',
        'sa.Column("evidence"',
        'sa.Column("vector"',
        'sa.Column("path"',
        'sa.Column("secret"',
        'sa.Column("token"',
    ):
        assert forbidden not in source


def test_phase12_3_migration_keeps_pointer_and_event_authority_in_sync():
    path, _ = _module()
    source = path.read_text(encoding="utf-8")
    assert 'sa.Column("suite_id", sa.Uuid())' in source
    assert "fk_adapter_deployment_evaluation_scope" in source
    assert "fk_adapter_deployment_operation_target_retention_exact" in source
    assert "fk_adapter_deployment_event_rollback_retention" in source
    assert "uq_adapter_rollback_retention_adapter_scope" in source
