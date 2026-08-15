from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _module():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0015_phase12_adapter_evaluation.py"
    spec = spec_from_file_location("phase12_2_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def test_phase12_2_migration_is_self_contained_and_has_one_head():
    path, module = _module()
    source = path.read_text(encoding="utf-8")
    assert "app.models" not in source
    assert "app.adapter_contract" not in source
    assert module.revision == "0015_phase12_adapter_evaluation"
    assert module.down_revision == "0014_phase12_adapter_purge"
    for table in (
        "adapter_evaluation_runs",
        "adapter_evaluation_attempts",
        "adapter_evaluation_evidence",
        "adapter_evaluation_case_results",
    ):
        assert table in source


def test_phase12_2_migration_keeps_content_out_of_schema():
    path, _ = _module()
    source = path.read_text(encoding="utf-8")
    for forbidden in (
        'sa.Column("question"',
        'sa.Column("accepted_answer"',
        'sa.Column("generated_answer"',
        'sa.Column("prompt"',
        'sa.Column("evidence_text"',
        'sa.Column("vector"',
        'sa.Column("adapter_bytes"',
    ):
        assert forbidden not in source
