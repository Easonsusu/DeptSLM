"""Opt-in parser compatibility for a separately provisioned LlamaFactory 0.9.5."""

from __future__ import annotations

import importlib.metadata
import os
from copy import deepcopy
from uuid import uuid4

import pytest

from app.training_job_domain import (
    LLAMAFACTORY_VERSION,
    build_bundle,
    validate_phase10_records,
)


def _records() -> bytes:
    return (
        b'{"example_id":"11111111-1111-1111-1111-111111111111","messages":'
        b'[{"role":"user","content":"Synthetic question"},{"role":"assistant",'
        b'"content":"Synthetic answer"}]}\n'
    )


def _config(profile_id: str) -> dict[str, object]:
    bundle = build_bundle(
        department_id=uuid4(),
        training_job_id=uuid4(),
        dataset_build_id=uuid4(),
        publication_attempt_id=uuid4(),
        execution_scope_id=uuid4(),
        attempt_number=1,
        code_revision="a" * 40,
        dataset_build_version=1,
        dataset_manifest_sha256="b" * 64,
        dataset_artifact_contract_version="phase10-sft-dataset-v1",
        dataset_example_contract_version="phase10-sft-example-v1",
        dataset_normalization_version="phase10-sft-normalization-v1",
        dataset_split_version="phase10-sft-group-split-v1",
        profile_id=profile_id,
        dataset_rights_attested=True,
        evaluation_contamination_reviewed=True,
        dataset=validate_phase10_records(_records(), _records()),
    )
    yaml = pytest.importorskip("yaml")
    parsed = yaml.safe_load(bundle.training_yaml)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.skipif(
    os.getenv("DEPTSLM_RUN_LLAMFACTORY_CONFIG_SMOKE") != "1",
    reason="set DEPTSLM_RUN_LLAMFACTORY_CONFIG_SMOKE=1 in an isolated LlamaFactory environment",
)
@pytest.mark.parametrize(
    ("profile_id", "is_qlora"),
    [
        ("phase11-qwen3-0.6b-lora-v1", False),
        ("phase11-qwen3-0.6b-qlora-nf4-v1", True),
    ],
)
def test_opt_in_llamafactory_parser_accepts_exact_generated_profiles(
    monkeypatch, profile_id: str, is_qlora: bool
) -> None:
    """Parse generated YAML only; never initialize a tokenizer, model, or trainer."""

    assert importlib.metadata.version("llamafactory") == LLAMAFACTORY_VERSION
    monkeypatch.delenv("ALLOW_EXTRA_ARGS", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("HF_HOME", "/nonexistent/deptslm-llamafactory-smoke-cache")
    from llamafactory.hparams.parser import get_train_args

    config = _config(profile_id)
    assert config["packing"] is False
    assert config["neat_packing"] is False
    assert config["enable_liger_kernel"] is False
    assert config["use_unsloth"] is False
    assert "use_liger_kernel" not in config
    parsed = get_train_args(config)
    model_args, data_args, _training_args, finetuning_args, _generation_args = parsed
    assert data_args.packing is False
    assert data_args.neat_packing is False
    assert finetuning_args.finetuning_type == "lora"
    assert model_args.enable_liger_kernel is False
    assert model_args.use_unsloth is False
    if is_qlora:
        assert model_args.quantization_bit == 4
        assert model_args.quantization_method == "bnb"
        assert model_args.quantization_type == "nf4"
        assert model_args.double_quantization is True
    else:
        assert model_args.quantization_bit is None

    for key, value in (("use_liger_kernel", False), ("unexpected_key", True)):
        invalid = deepcopy(config)
        invalid[key] = value
        with pytest.raises(ValueError):
            get_train_args(invalid)
    invalid = deepcopy(_config("phase11-qwen3-0.6b-qlora-nf4-v1"))
    invalid["quantization_method"] = "bitsandbytes"
    with pytest.raises(ValueError):
        get_train_args(invalid)
