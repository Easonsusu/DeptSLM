"""Static guardrails for the Roadmap v2 Phase 14 execution boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERSIONS = ROOT / "apps" / "api" / "alembic" / "versions"


def test_phase14_2_advances_the_alembic_head_once() -> None:
    revisions = sorted(path.stem for path in VERSIONS.glob("*.py"))
    assert revisions[-1] == "0019_phase14_training_runtime"
    migration = (VERSIONS / "0019_phase14_training_runtime.py").read_text(encoding="utf-8")
    assert 'down_revision = "0018_phase14_training_execution_control_plane"' in migration
    assert "0019_phase14_training_runtime" in migration


def test_phase14_2_adds_only_the_private_runtime_service() -> None:
    assert not (ROOT / "services" / "training-execution").exists()
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s+training-execution-worker:", compose)
    assert re.search(r"(?m)^\s+training-runtime:", compose)

    routes = (ROOT / "apps" / "api" / "app" / "routes.py").read_text(encoding="utf-8")
    assert "/training/executions" in routes
    assert "/training/executions/{execution_id}/cancel" in routes
    assert "/training/executions/{execution_id}/retry" in routes


def test_phase14_2_keeps_the_real_training_stack_out_of_the_api() -> None:
    api_requirements = (
        (ROOT / "apps" / "api" / "requirements.txt").read_text(encoding="utf-8").lower()
    )
    web_package = json.loads((ROOT / "apps" / "web" / "package.json").read_text(encoding="utf-8"))
    assert "llamafactory" not in api_requirements
    assert "llamafactory" not in json.dumps(web_package).lower()

    runtime_requirements = (
        (ROOT / "services" / "training-runtime" / "requirements.lock")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "llamafactory==0.9.5" in runtime_requirements
    runtime = (ROOT / "services" / "training-runtime" / "deptslm_training_runtime").rglob("*.py")
    assert any("llamafactory-cli" in path.read_text(encoding="utf-8") for path in runtime)


def test_phase14_1_does_not_add_training_credentials_or_docker_socket() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in compose
    training_worker = compose.split("  training-worker:", 1)[1]
    assert "HF_TOKEN" not in training_worker
    assert "HUGGING_FACE_HUB_TOKEN" not in training_worker


def test_phase14_2_contract_and_phase11_identity_are_documented() -> None:
    contract = (ROOT / "docs" / "training-execution.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    assert (
        "Phase 11 currently generates an immutable reviewable LlamaFactory job bundle." in contract
    )
    assert "Phase 14.0 is complete." in contract
    assert "Phase 14.2" in contract
    assert "## Roadmap v2" in roadmap
    assert "Phase 14.0" in roadmap
    assert "Phase 14.1" in roadmap
    assert "Phase 14.2" in roadmap
    assert "Phase 14.3" in roadmap
    assert "Phase 15 has no implementation scope" in roadmap

    for value in (
        "Qwen/Qwen3-0.6B",
        "c1899de289a04d12100db370d81485cdf75e47ca",
        "phase11-qwen3-0.6b-lora-v1",
        "phase11-qwen3-0.6b-qlora-nf4-v1",
        "LlamaFactory `0.9.5`",
    ):
        assert value in contract


def test_phase14_2_preserves_the_future_adapter_handoff_boundary() -> None:
    contract = (ROOT / "docs" / "training-execution.md").read_text(encoding="utf-8")
    assert "existing Phase 12.1A model-free static adapter validator" in contract
    assert re.search(r"automatic\s+adapter intake", contract)
    assert "automatic" in contract
    assert "Phase 14.3" in contract
