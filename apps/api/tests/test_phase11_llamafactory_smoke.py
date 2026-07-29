"""Explicitly opt-in compatibility check for a separately provisioned LlamaFactory 0.9.5."""

from __future__ import annotations

import importlib.metadata
import os

import pytest

from app.training_job_domain import LLAMAFACTORY_VERSION, _dataset_info


@pytest.mark.skipif(
    os.getenv("DEPTSLM_RUN_LLAMFACTORY_CONFIG_SMOKE") != "1",
    reason="set DEPTSLM_RUN_LLAMFACTORY_CONFIG_SMOKE=1 in an isolated LlamaFactory environment",
)
def test_opt_in_llamafactory_config_smoke_uses_exact_pinned_version() -> None:
    assert importlib.metadata.version("llamafactory") == LLAMAFACTORY_VERSION
    dataset_info = _dataset_info()
    assert set(dataset_info) == {"deptslm_train", "deptslm_validation"}
    assert dataset_info["deptslm_train"]["formatting"] == "sharegpt"
