"""Static guardrails for the Phase 14.0/14.1 execution boundary."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERSIONS = ROOT / "apps" / "api" / "alembic" / "versions"


def test_phase14_1_advances_the_alembic_head_once() -> None:
    revisions = sorted(path.stem for path in VERSIONS.glob("*.py"))
    assert revisions[-1] == "0018_phase14_training_execution_control_plane"
    migration = (VERSIONS / "0018_phase14_training_execution_control_plane.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision = "0017_phase12_adapter_runtime_routing"' in migration
    assert "0019" not in migration


def test_phase14_1_has_only_the_control_plane_and_no_runtime_service() -> None:
    assert not (ROOT / "services" / "training-execution").exists()
    assert not (ROOT / "services" / "training-runtime").exists()
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s+(?:training-execution|training-runtime):", compose)

    routes = (ROOT / "apps" / "api" / "app" / "routes.py").read_text(encoding="utf-8")
    assert "/training/executions" in routes
    assert "/training/executions/{execution_id}/cancel" in routes
    assert "/training/executions/{execution_id}/retry" in routes


def test_phase14_1_does_not_install_or_execute_llamafactory() -> None:
    api_requirements = (
        (ROOT / "apps" / "api" / "requirements.txt").read_text(encoding="utf-8").lower()
    )
    web_package = json.loads((ROOT / "apps" / "web" / "package.json").read_text(encoding="utf-8"))
    assert "llamafactory" not in api_requirements
    assert "llamafactory" not in json.dumps(web_package).lower()

    executable_roots = (ROOT / "apps" / "api" / "app", ROOT / "services")
    executable_files = [
        path
        for root in executable_roots
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh"}
    ]
    training_command = re.compile(r"llamafactory-cli\s+train", re.IGNORECASE)
    assert not any(
        training_command.search(path.read_text(encoding="utf-8", errors="ignore"))
        for path in executable_files
    )


def test_phase14_1_does_not_add_training_credentials_or_docker_socket() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in compose
    training_worker = compose.split("  training-worker:", 1)[1]
    assert "HF_TOKEN" not in training_worker
    assert "HUGGING_FACE_HUB_TOKEN" not in training_worker


def test_phase14_1_contract_and_phase11_identity_are_documented() -> None:
    contract = (ROOT / "docs" / "training-execution.md").read_text(encoding="utf-8")
    roadmap = (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    assert (
        "Phase 11 currently generates an immutable reviewable LlamaFactory job bundle." in contract
    )
    assert "Phase 14.0 is complete." in contract
    assert "Phase 14.1 implements only the metadata control" in contract
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


def test_phase14_1_reuses_static_adapter_validation_and_forbids_automation() -> None:
    contract = (ROOT / "docs" / "training-execution.md").read_text(encoding="utf-8")
    assert "existing Phase 12.1A model-free static adapter validator" in contract
    assert re.search(r"automatic\s+adapter intake", contract)
    assert "automatic" in contract
    assert "Phase 14.1 does not install or invoke LlamaFactory" in contract
