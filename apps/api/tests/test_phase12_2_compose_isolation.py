import re
from pathlib import Path


def _service(source: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n.*?(?=^  [a-z][a-z0-9-]*:|\Z)",
        source,
    )
    assert match is not None
    return match.group(0)


def test_phase12_2_compose_services_have_isolated_mounts_and_networks():
    source = (Path(__file__).parents[3] / "docker-compose.yml").read_text(encoding="utf-8")
    evaluator = _service(source, "adapter-evaluator")
    runtime = _service(source, "adapter-eval-runtime")
    assert 'profiles: ["adapter-evaluation"]' in evaluator
    assert "/extracted_text" in evaluator and "/eval_results" in evaluator
    assert "/model_cache" not in evaluator and "/adapters/registry" not in evaluator
    assert "/model_cache" in runtime and "/adapters/registry" in runtime
    assert "/eval_results" not in runtime and "DATABASE_URL" not in runtime
    assert "ports:" not in runtime
    assert "rag-internal" in evaluator and "rag-internal" in runtime
