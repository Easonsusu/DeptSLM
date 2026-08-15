from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_governance_worker_compose_mount_is_read_only_and_narrow():
    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = source.split("  adapter-governance-worker:", 1)[1].split("\n  adapter-maintenance:", 1)[
        0
    ]
    assert "read_only: true" in block
    assert "target: /runtime/deptslm/adapters/registry" in block
    assert "read_only: true" in block
    assert "/adapters/registry" in block
    for forbidden in (
        "qdrant",
        "model_cache",
        "uploads",
        "extracted_text",
        "training_datasets",
        "eval_results",
    ):
        assert forbidden not in block.lower()
    assert 'DEPTSLM_STORAGE_READ_ONLY: "1"' in block


def test_governance_worker_image_has_no_model_or_vector_stack():
    dockerfile = (
        (ROOT / "services/adapter-governance-worker/Dockerfile").read_text(encoding="utf-8").lower()
    )
    entrypoint = (
        (ROOT / "services/adapter-governance-worker/entrypoint.sh")
        .read_text(encoding="utf-8")
        .lower()
    )
    for forbidden in ("torch", "transformers", "peft", "qdrant", "llamaindex", "huggingface"):
        assert forbidden not in dockerfile
        assert forbidden not in entrypoint


def test_governance_worker_uses_only_the_narrow_registry_reader():
    source = (ROOT / "apps/api/app/adapter_governance_worker.py").read_text(encoding="utf-8")
    assert "AdapterRegistryFinalReader" in source
    assert "AdapterRegistryArtifactStore" not in source
    assert "from app.settings import Settings" not in source
