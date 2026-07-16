"""Explicit offline smoke test for pre-provisioned pinned model assets."""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
from deptslm_worker.embedding import EmbeddingProcess
from deptslm_worker.model_store import validate_model_store

from app.vector_index_domain import EMBEDDING_DIMENSION

pytestmark = pytest.mark.unit


def test_pinned_real_embedding_model_offline_smoke() -> None:
    if os.getenv("DEPTSLM_RUN_REAL_MODEL_TEST") != "1":
        pytest.skip("real pinned model smoke test is explicitly opt-in")
    raw_data_dir = os.getenv("DEPTSLM_DATA_DIR")
    if not raw_data_dir:
        pytest.fail("DEPTSLM_DATA_DIR is required for the opt-in model test")
    model = validate_model_store(Path(raw_data_dir)).path
    with EmbeddingProcess(
        model,
        provider="real",
        environment="test",
        timeout_seconds=300,
        heartbeat=lambda: True,
        should_stop=lambda: False,
    ) as process:
        vector = process.embed(["A short department document sentence."])[0]
    assert len(vector) == EMBEDDING_DIMENSION
    assert all(math.isfinite(value) for value in vector)
    assert sum(value * value for value in vector) == pytest.approx(1.0, abs=1e-3)
