"""Regression checks for the 2026 dependency and secret hardening pass."""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_compose_requires_one_postgres_secret_and_keeps_database_urls_in_sync() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "DEPTSLM_POSTGRES_PASSWORD:?" in compose
    assert "postgresql+psycopg://deptslm:${DEPTSLM_POSTGRES_PASSWORD" in compose
    assert "postgresql+psycopg://deptslm:deptslm@" not in compose
    assert re.search(r'"127\.0\.0\.1:5432:5432"', compose)
    assert "POSTGRES_PASSWORD: deptslm" not in compose


def test_qdrant_server_and_client_are_patched_without_broadening_the_boundary() -> None:
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    requirements = (REPOSITORY_ROOT / "apps/api/requirements.txt").read_text(encoding="utf-8")
    worker = (REPOSITORY_ROOT / "services/rag-worker/pyproject.toml").read_text(encoding="utf-8")

    assert "qdrant/qdrant:v1.16.3@sha256:" in compose
    assert "qdrant-client>=1.16.2,<1.17.0" in requirements
    assert "qdrant-client>=1.16.2,<1.17.0" in worker
    assert "127.0.0.1:6333:6333" in compose
    assert "127.0.0.1:6334:6334" in compose
    assert "DepartmentScope" in (
        REPOSITORY_ROOT / "services/rag-worker/deptslm_worker/qdrant_adapter.py"
    ).read_text(encoding="utf-8")


def test_runtime_images_are_digest_pinned() -> None:
    dockerfiles = list(REPOSITORY_ROOT.glob("apps/*/Dockerfile")) + list(
        REPOSITORY_ROOT.glob("services/*/Dockerfile")
    )
    assert dockerfiles
    for dockerfile in dockerfiles:
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM "):
                base_image = line.split()[1]
                if base_image in {"worker-source", "extraction"}:
                    continue
                assert "@sha256:" in line, dockerfile

    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for image in ("postgres:16-alpine", "qdrant/qdrant:v1.16.3"):
        assert re.search(rf"image: {re.escape(image)}@sha256:[0-9a-f]{{64}}", compose)


def test_github_actions_are_pinned_to_immutable_commits() -> None:
    workflows = list((REPOSITORY_ROOT / ".github/workflows").glob("*.yml"))
    assert workflows
    for workflow in workflows:
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if " uses:" not in line:
                continue
            reference = line.split("uses:", 1)[1].strip().split()[0]
            assert re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*@[0-9a-f]{40}", reference), (
                workflow,
                line,
            )


def test_node_lock_uses_exact_patched_transitive_overrides() -> None:
    workspace = (REPOSITORY_ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8")
    lock = (REPOSITORY_ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")

    for value in ("nanoid: 3.3.18", "postcss: 8.5.26", "sharp: 0.35.3"):
        assert value in workspace
    for value in ("nanoid@3.3.18", "postcss@8.5.26", "sharp@0.35.3"):
        assert value in lock


def test_linux_model_locks_include_hashed_cuda_platform_dependencies() -> None:
    required = (
        "nvidia-cublas-cu12==12.6.4.1",
        "nvidia-cuda-cupti-cu12==12.6.80",
        "nvidia-cuda-nvrtc-cu12==12.6.77",
        "nvidia-cuda-runtime-cu12==12.6.77",
        "nvidia-cudnn-cu12==9.5.1.17",
        "nvidia-cufft-cu12==11.3.0.4",
        "nvidia-cufile-cu12==1.11.1.6",
        "nvidia-curand-cu12==10.3.7.77",
        "nvidia-cusolver-cu12==11.7.1.2",
        "nvidia-cusparse-cu12==12.5.4.2",
        "nvidia-cusparselt-cu12==0.6.3",
        "nvidia-nccl-cu12==2.26.2",
        "nvidia-nvjitlink-cu12==12.6.85",
        "nvidia-nvtx-cu12==12.6.77",
        "triton==3.3.1",
    )
    lock_paths = (
        REPOSITORY_ROOT / "apps/api/requirements-ci.lock",
        REPOSITORY_ROOT / "services/rag-worker/requirements-vector.lock",
        REPOSITORY_ROOT / "services/rag-runtime/requirements.lock",
        REPOSITORY_ROOT / "services/adapter-runtime/requirements.lock",
        REPOSITORY_ROOT / "services/adapter-eval-runtime/requirements.lock",
    )

    for lock_path in lock_paths:
        lock = lock_path.read_text(encoding="utf-8")
        for package in required:
            assert (
                f'{package} ; platform_system == "Linux" and platform_machine == "x86_64"'
            ) in lock, (lock_path, package)


def test_model_loading_security_contract_remains_pinned_and_offline() -> None:
    runtime = (
        REPOSITORY_ROOT / "services/training-runtime/deptslm_training_runtime/contract.py"
    ).read_text(encoding="utf-8")
    store = (
        REPOSITORY_ROOT / "services/training-runtime/deptslm_training_runtime/model_store.py"
    ).read_text(encoding="utf-8")
    model_runtimes = "\n".join(
        (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
        for relative_path in (
            "services/rag-runtime/deptslm_runtime/models.py",
            "services/adapter-runtime/deptslm_adapter_runtime/loader.py",
            "services/adapter-eval-runtime/deptslm_adapter_runtime/loader.py",
        )
    )
    lock = (REPOSITORY_ROOT / "services/training-runtime/requirements.lock").read_text(
        encoding="utf-8"
    )

    for value in (
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "local_files_only",
        "trust_remote_code",
        "use_safetensors",
    ):
        assert value in runtime or value in store or value in model_runtimes, value
    assert "transformers==4.55.0" in lock
    assert "peft==0.18.1" in lock
    assert "safetensors==0.7.0" in lock
