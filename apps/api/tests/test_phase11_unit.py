"""Focused Phase 11 fixed-contract and external-artifact tests."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from app.authorization import DepartmentScope
from app.sft_artifacts import SftArtifactError, SftArtifactStore
from app.training_job_domain import (
    BASE_MODEL_ID,
    BASE_MODEL_REVISION,
    TRAINING_JOB_FILES,
    TrainingJobContractError,
    build_bundle,
    parse_job_manifest,
    validate_phase10_records,
)


def _records() -> bytes:
    return (
        b'{"example_id":"11111111-1111-1111-1111-111111111111","messages":'
        b'[{"role":"user","content":"Synthetic question"},{"role":"assistant",'
        b'"content":"Synthetic answer"}]}\n'
    )


def _bundle():
    return build_bundle(
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
        profile_id="phase11-qwen3-0.6b-lora-v1",
        dataset_rights_attested=True,
        evaluation_contamination_reviewed=True,
        train=_records(),
        validation=_records(),
    )


def test_bundle_is_fixed_and_content_free_except_required_dataset_copies() -> None:
    bundle = _bundle()
    manifest = parse_job_manifest(bundle.manifest)
    assert manifest["base_model_id"] == BASE_MODEL_ID
    assert manifest["base_model_revision"] == BASE_MODEL_REVISION
    assert bundle.train == _records()
    assert bundle.validation == _records()
    assert b"trust_remote_code: false" in bundle.training_yaml
    assert b"template: qwen3_nothink" in bundle.training_yaml
    assert b"output_dir: /runtime/deptslm/adapters/.unregistered/" in bundle.training_yaml
    assert set(json.loads(bundle.dataset_info)) == {"deptslm_train", "deptslm_validation"}


def test_qlora_profile_has_only_reviewed_nf4_additions() -> None:
    values = build_bundle(
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
        profile_id="phase11-qwen3-0.6b-qlora-nf4-v1",
        dataset_rights_attested=True,
        evaluation_contamination_reviewed=True,
        train=_records(),
        validation=_records(),
    ).training_yaml
    assert b"quantization_bit: 4" in values
    assert b"quantization_method: bitsandbytes" in values
    assert b"quantization_type: nf4" in values
    assert b"double_quantization: true" in values


@pytest.mark.parametrize(
    "record",
    [
        b'{"example_id":"11111111-1111-1111-1111-111111111111","messages":[]}\n',
        b'{"example_id":"11111111-1111-1111-1111-111111111111","messages":[{"role":"user","content":"x"},{"role":"system","content":"y"}]}\n',
        b'{"example_id":"11111111-1111-1111-1111-111111111111","messages":[{"role":"user","content":"\xcd\x8f"},{"role":"assistant","content":"y"}]}\n',
    ],
)
def test_phase10_records_fail_closed(record: bytes) -> None:
    with pytest.raises(TrainingJobContractError):
        validate_phase10_records(record, record)


def test_job_manifest_rejects_unknown_or_contentful_field() -> None:
    manifest = json.loads(_bundle().manifest)
    manifest["dataset_text"] = "not allowed"
    with pytest.raises(TrainingJobContractError):
        parse_job_manifest(json.dumps(manifest).encode())


def test_private_training_job_artifact_requires_exact_manifest(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    bundle = _bundle()
    manifest = parse_job_manifest(bundle.manifest)
    department_id = uuid4()
    job_id = uuid4()
    attempt_id = uuid4()
    scope = DepartmentScope(department_id)
    manifest["department_id"] = str(department_id)
    manifest["training_job_id"] = str(job_id)
    manifest["publication_attempt_id"] = str(attempt_id)
    rebuilt = build_bundle(
        department_id=department_id,
        training_job_id=job_id,
        dataset_build_id=uuid4(),
        publication_attempt_id=attempt_id,
        execution_scope_id=uuid4(),
        attempt_number=1,
        code_revision="a" * 40,
        dataset_build_version=1,
        dataset_manifest_sha256="b" * 64,
        dataset_artifact_contract_version="phase10-sft-dataset-v1",
        dataset_example_contract_version="phase10-sft-example-v1",
        dataset_normalization_version="phase10-sft-normalization-v1",
        dataset_split_version="phase10-sft-group-split-v1",
        profile_id="phase11-qwen3-0.6b-lora-v1",
        dataset_rights_attested=True,
        evaluation_contamination_reviewed=True,
        train=_records(),
        validation=_records(),
    )
    expected = parse_job_manifest(rebuilt.manifest)
    with SftArtifactStore(tmp_path) as store:
        staged = store.prepare_training_job_stage(scope, job_id, attempt_id)
        assert staged.stage_fd is not None
        for name, value in (
            ("manifest.json", rebuilt.manifest),
            ("training.yaml", rebuilt.training_yaml),
            ("dataset_info.json", rebuilt.dataset_info),
            ("train.jsonl", rebuilt.train),
            ("validation.jsonl", rebuilt.validation),
        ):
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=staged.stage_fd
            )
            try:
                os.write(descriptor, value)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        verified = store.preverify_staged(staged, allowlist=TRAINING_JOB_FILES, expected=expected)
        store.transition_stage_marker(verified)
        store.rename_preverified_stage(verified)
        final = store.verify_preverified_final(verified)
        final.close()
        assert store.verify_training_job_final(scope, job_id, expected=expected)
        with pytest.raises(SftArtifactError):
            store.remove_owned_training_job_final(scope, job_id, attempt_id, expected={})
        assert store.remove_owned_training_job_final(scope, job_id, attempt_id, expected=expected)
