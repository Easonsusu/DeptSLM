from pathlib import Path


def test_phase12_1e_b_contains_active_adapter_evaluation_fences():
    source = (Path(__file__).parents[1] / "app" / "adapter_purge.py").read_text(encoding="utf-8")
    assert "AdapterEvaluationRun" in source
    assert 'status.in_(("queued", "running"))' in source
    assert "Adapter purge conflicts with active evaluation" in source
