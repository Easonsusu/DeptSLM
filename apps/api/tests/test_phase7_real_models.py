"""Explicitly opt-in, offline smoke coverage for both exact Phase 7 models."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from deptslm_runtime.models import RuntimeModels
from deptslm_worker.model_store import (
    generation_model_directory,
    model_directory,
)

from app.rag_domain import GENERATION_MODEL_REVISION
from app.vector_index_domain import EMBEDDING_DIMENSION, EMBEDDING_MODEL_REVISION

pytestmark = pytest.mark.unit


def test_exact_two_model_runtime_offline_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("DEPTSLM_PHASE7_REAL_MODEL_SMOKE") != "1":
        pytest.skip("opt-in Phase 7 real-model smoke test is disabled")
    raw_root = os.getenv("DEPTSLM_DATA_DIR", "").strip()
    if not raw_root:
        pytest.skip("external Phase 7 model cache is not configured")
    root = Path(raw_root).expanduser().resolve(strict=True)
    repository = Path(__file__).resolve().parents[3]
    if repository == root or repository in root.parents or root in repository.parents:
        pytest.fail("real-model smoke data must be external to the repository")
    if not model_directory(root).is_dir() or not generation_model_directory(root).is_dir():
        pytest.skip("both exact prepared Phase 7 model directories are required")

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")
    monkeypatch.setenv("DEPTSLM_EMBEDDING_MODEL_REVISION", EMBEDDING_MODEL_REVISION)
    monkeypatch.setenv("DEPTSLM_GENERATION_MODEL_REVISION", GENERATION_MODEL_REVISION)
    models = RuntimeModels(root, "real")
    vector = models.embed_question("What policy is supported?")
    assert len(vector) == EMBEDDING_DIMENSION
    result = models.generate(
        "What is approved?",
        [{"source_id": "S1", "text": "The synthetic policy says testing is approved."}],
    )
    assert result["status"] in {"answered", "insufficient_information"}
