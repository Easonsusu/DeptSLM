"""Focused Phase 11 fixed-contract and external-artifact tests."""

from __future__ import annotations

import json
import os
import time
from hashlib import sha256
from uuid import uuid4

import pytest

from app import training_job_queue
from app.authorization import DepartmentScope
from app.sft_artifacts import SftArtifactError, SftArtifactStore
from app.training_job_child import _build as build_child_bundle
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
        dataset=validate_phase10_records(_records(), _records()),
    )


def _publish_job_final(tmp_path):
    """Publish one exact synthetic job bundle for tombstone-bound deletion tests."""

    (tmp_path / "training_datasets").mkdir(mode=0o700)
    department_id, job_id, attempt_id = uuid4(), uuid4(), uuid4()
    scope = DepartmentScope(department_id)
    bundle = build_bundle(
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
        dataset=validate_phase10_records(_records(), _records()),
    )
    expected = parse_job_manifest(bundle.manifest)
    with SftArtifactStore(tmp_path) as store:
        staged = store.prepare_training_job_stage(scope, job_id, attempt_id)
        assert staged.stage_fd is not None
        for name, value in (
            ("manifest.json", bundle.manifest),
            ("training.yaml", bundle.training_yaml),
            ("dataset_info.json", bundle.dataset_info),
            ("train.jsonl", _records()),
            ("validation.jsonl", _records()),
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
        store.verify_preverified_final(verified).close()
    return scope, job_id, attempt_id, expected


def test_bundle_is_fixed_and_content_free_except_descriptor_metadata() -> None:
    bundle = _bundle()
    manifest = parse_job_manifest(bundle.manifest)
    assert manifest["base_model_id"] == BASE_MODEL_ID
    assert manifest["base_model_revision"] == BASE_MODEL_REVISION
    assert (
        manifest["files"]["train.jsonl"]["sha256"]
        == validate_phase10_records(_records(), _records()).train_sha256
    )
    assert b"trust_remote_code: false" in bundle.training_yaml
    assert b"template: qwen3_nothink" in bundle.training_yaml
    assert b"packing: false" in bundle.training_yaml
    assert b"neat_packing: false" in bundle.training_yaml
    assert b"enable_liger_kernel: false" in bundle.training_yaml
    assert b"use_unsloth: false" in bundle.training_yaml
    assert b"use_liger_kernel:" not in bundle.training_yaml
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
        dataset=validate_phase10_records(_records(), _records()),
    ).training_yaml
    assert b"quantization_bit: 4" in values
    assert b"quantization_method: bnb" in values
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
        dataset=validate_phase10_records(_records(), _records()),
    )
    expected = parse_job_manifest(rebuilt.manifest)
    with SftArtifactStore(tmp_path) as store:
        staged = store.prepare_training_job_stage(scope, job_id, attempt_id)
        assert staged.stage_fd is not None
        for name, value in (
            ("manifest.json", rebuilt.manifest),
            ("training.yaml", rebuilt.training_yaml),
            ("dataset_info.json", rebuilt.dataset_info),
            ("train.jsonl", _records()),
            ("validation.jsonl", _records()),
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


def test_authorized_final_deletion_requires_complete_manifest_before_tombstone(tmp_path) -> None:
    scope, job_id, attempt_id, expected = _publish_job_final(tmp_path)
    operation_id = uuid4()
    with SftArtifactStore(tmp_path) as store:
        with pytest.raises(SftArtifactError, match="artifact_manifest_invalid"):
            store.prepare_authorized_training_job_tombstone(
                scope, job_id, attempt_id, operation_id, expected={}
            )
        assert store.verify_training_job_final(scope, job_id, expected=expected)
    assert not (
        tmp_path
        / "training_datasets"
        / ".deleting"
        / "jobs"
        / str(scope.value)
        / str(job_id)
        / str(operation_id)
    ).exists()


@pytest.mark.parametrize("crash_after_unlink", range(1, len(TRAINING_JOB_FILES) + 1))
def test_bound_tombstone_recovers_after_each_file_unlink_across_store_instances(
    tmp_path, crash_after_unlink: int
) -> None:
    scope, job_id, attempt_id, expected = _publish_job_final(tmp_path)
    operation_id = uuid4()
    with SftArtifactStore(tmp_path) as store:
        identity = store.prepare_authorized_training_job_tombstone(
            scope, job_id, attempt_id, operation_id, expected=expected
        )
    assert isinstance(identity, dict)
    names = list(identity["remaining_files"])
    for index, name in enumerate(names):
        step = dict(identity)
        step["remaining_files"] = list(identity["remaining_files"])
        step["unlinking_file"] = name
        with SftArtifactStore(tmp_path) as store:
            assert store.unlink_bound_training_job_tombstone_file(
                scope, job_id, operation_id, identity=step, name=name
            )
        if index == crash_after_unlink - 1:
            # Simulate a restart after the unlink but before its database
            # progress record. The durable in-flight member is the only
            # accepted missing-file state on retry.
            with SftArtifactStore(tmp_path) as store:
                assert store.unlink_bound_training_job_tombstone_file(
                    scope, job_id, operation_id, identity=step, name=name
                )
        identity = dict(step)
        identity["remaining_files"] = [value for value in names if value not in names[: index + 1]]
        identity["unlinking_file"] = None
    final = tmp_path / "training_datasets" / "jobs" / str(scope.value) / str(job_id)
    tombstone = (
        tmp_path
        / "training_datasets"
        / ".deleting"
        / "jobs"
        / str(scope.value)
        / str(job_id)
        / str(operation_id)
    )
    assert not final.exists() and tombstone.is_dir()
    with SftArtifactStore(tmp_path) as store:
        assert store.remove_bound_training_job_tombstone_directory(
            scope, job_id, operation_id, identity=identity
        )
    assert not tombstone.exists()


def test_tombstone_rejects_substituted_private_directory_without_deleting(tmp_path) -> None:
    scope, job_id, attempt_id, expected = _publish_job_final(tmp_path)
    operation_id = uuid4()
    with SftArtifactStore(tmp_path) as store:
        identity = store.prepare_authorized_training_job_tombstone(
            scope, job_id, attempt_id, operation_id, expected=expected
        )
    assert isinstance(identity, dict)
    tombstone = (
        tmp_path
        / "training_datasets"
        / ".deleting"
        / "jobs"
        / str(scope.value)
        / str(job_id)
        / str(operation_id)
    )
    parked = tombstone.parent / "parked"
    os.rename(tombstone, parked)
    tombstone.mkdir(mode=0o700)
    sentinel = tombstone / "manifest.json"
    sentinel.write_bytes(b"replacement")
    sentinel.chmod(0o600)
    name = identity["remaining_files"][0]
    step = dict(identity)
    step["unlinking_file"] = name
    with SftArtifactStore(tmp_path) as store:
        with pytest.raises(SftArtifactError, match="final_deletion_recovery_required"):
            store.unlink_bound_training_job_tombstone_file(
                scope, job_id, operation_id, identity=step, name=name
            )
    assert sentinel.exists() and parked.is_dir()


def test_tombstone_rejects_same_uid_allowlisted_file_substitution(tmp_path) -> None:
    scope, job_id, attempt_id, expected = _publish_job_final(tmp_path)
    operation_id = uuid4()
    with SftArtifactStore(tmp_path) as store:
        identity = store.prepare_authorized_training_job_tombstone(
            scope, job_id, attempt_id, operation_id, expected=expected
        )
    assert isinstance(identity, dict)
    tombstone = (
        tmp_path
        / "training_datasets"
        / ".deleting"
        / "jobs"
        / str(scope.value)
        / str(job_id)
        / str(operation_id)
    )
    target = tombstone / "training.yaml"
    target.unlink()
    target.write_bytes(b"same-uid replacement")
    target.chmod(0o600)
    name = "training.yaml"
    step = dict(identity)
    step["unlinking_file"] = name
    with SftArtifactStore(tmp_path) as store:
        with pytest.raises(SftArtifactError, match="final_deletion_recovery_required"):
            store.unlink_bound_training_job_tombstone_file(
                scope, job_id, operation_id, identity=step, name=name
            )
    assert target.exists() and tombstone.exists()


def test_partial_unbound_tombstone_is_fenced_and_never_cleaned(tmp_path) -> None:
    scope, job_id, attempt_id, expected = _publish_job_final(tmp_path)
    operation_id = uuid4()
    with SftArtifactStore(tmp_path) as store:
        identity = store.prepare_authorized_training_job_tombstone(
            scope, job_id, attempt_id, operation_id, expected=expected
        )
    assert isinstance(identity, dict)
    tombstone = (
        tmp_path
        / "training_datasets"
        / ".deleting"
        / "jobs"
        / str(scope.value)
        / str(job_id)
        / str(operation_id)
    )
    (tombstone / "training.yaml").unlink()
    with SftArtifactStore(tmp_path) as restarted_store:
        with pytest.raises(SftArtifactError, match="final_deletion_recovery_required"):
            restarted_store.prepare_authorized_training_job_tombstone(
                scope, job_id, attempt_id, operation_id, expected=expected
            )
    assert tombstone.is_dir() and not (tombstone / "training.yaml").exists()


def test_tombstone_parent_substitution_is_detected_without_touching_replacement(tmp_path) -> None:
    scope, job_id, attempt_id, expected = _publish_job_final(tmp_path)
    operation_id = uuid4()
    job_parent = (
        tmp_path / "training_datasets" / ".deleting" / "jobs" / str(scope.value) / str(job_id)
    )
    with SftArtifactStore(tmp_path) as store:
        identity = store.prepare_authorized_training_job_tombstone(
            scope, job_id, attempt_id, operation_id, expected=expected
        )
    assert isinstance(identity, dict)
    parked = job_parent.parent / "parked"
    os.rename(job_parent, parked)
    job_parent.mkdir(mode=0o700)
    replacement = job_parent / str(operation_id)
    replacement.mkdir(mode=0o700)
    sentinel = replacement / "foreign"
    sentinel.write_bytes(b"x")
    sentinel.chmod(0o600)
    name = identity["remaining_files"][0]
    step = dict(identity)
    step["unlinking_file"] = name
    with SftArtifactStore(tmp_path) as store:
        with pytest.raises(SftArtifactError, match="final_deletion_recovery_required"):
            store.unlink_bound_training_job_tombstone_file(
                scope, job_id, operation_id, identity=step, name=name
            )
    assert (job_parent / str(operation_id) / "foreign").exists()


def test_child_streams_only_exact_retained_dataset_descriptors(tmp_path) -> None:
    """The child receives files, not a dataset directory it could reopen by name."""

    (tmp_path / "training_datasets").mkdir(mode=0o700)
    department_id, job_id, dataset_id, attempt_id, scope_id = (uuid4() for _ in range(5))
    train = _records() * 256
    validation = _records() * 128
    provenance = b'{"source_example_id":"11111111-1111-1111-1111-111111111111"}\n'
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    descriptors = {
        "train.jsonl": {"sha256": sha256(train).hexdigest(), "byte_size": len(train)},
        "validation.jsonl": {
            "sha256": sha256(validation).hexdigest(),
            "byte_size": len(validation),
        },
        "provenance.jsonl": {
            "sha256": sha256(provenance).hexdigest(),
            "byte_size": len(provenance),
        },
    }
    manifest = {
        "artifact_contract_version": "phase10-sft-dataset-v1",
        "department_id": str(department_id),
        "source_bundle_id": str(uuid4()),
        "build_id": str(dataset_id),
        "publication_attempt_id": str(uuid4()),
        "attempt_number": 1,
        "code_revision": "a" * 40,
        "normalization_version": "phase10-sft-normalization-v1",
        "example_contract_version": "phase10-sft-example-v1",
        "split_version": "phase10-sft-group-split-v1",
        "validation_ratio": "0.10",
        "source_example_count": 384,
        "source_group_count": 384,
        "source_reference_count": 384,
        "train_example_count": 256,
        "validation_example_count": 128,
        "files": descriptors,
    }
    values = {
        "manifest.json": json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        "train.jsonl": train,
        "validation.jsonl": validation,
        "provenance.jsonl": provenance,
    }
    for name, value in values.items():
        path = source / name
        path.write_bytes(value)
        path.chmod(0o600)
    fds = {name: os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW) for name in values}
    try:
        with SftArtifactStore(tmp_path) as store:
            stage = store.prepare_training_job_stage(
                DepartmentScope(department_id), job_id, attempt_id
            )
            assert stage.stage_fd is not None
            request = {
                "manifest_fd": fds["manifest.json"],
                "train_fd": fds["train.jsonl"],
                "validation_fd": fds["validation.jsonl"],
                "provenance_fd": fds["provenance.jsonl"],
                "stage_fd": stage.stage_fd,
                "department_id": str(department_id),
                "training_job_id": str(job_id),
                "dataset_build_id": str(dataset_id),
                "publication_attempt_id": str(attempt_id),
                "execution_scope_id": str(scope_id),
                "attempt_number": 1,
                "code_revision": "a" * 40,
                "dataset_build_version": 1,
                "dataset_manifest_sha256": sha256(values["manifest.json"]).hexdigest(),
                "dataset_source_bundle_id": manifest["source_bundle_id"],
                "dataset_status": "succeeded",
                "dataset_review_status": "approved",
                "dataset_publication_attempt_id": manifest["publication_attempt_id"],
                "dataset_publication_attempt_number": 1,
                "dataset_code_revision": "a" * 40,
                "dataset_artifact_contract_version": "phase10-sft-dataset-v1",
                "dataset_example_contract_version": "phase10-sft-example-v1",
                "dataset_normalization_version": "phase10-sft-normalization-v1",
                "dataset_split_version": "phase10-sft-group-split-v1",
                "profile_id": "phase11-qwen3-0.6b-lora-v1",
                "dataset_rights_attested": True,
                "evaluation_contamination_reviewed": True,
                "dataset_train_example_count": 256,
                "dataset_validation_example_count": 128,
                "dataset_source_example_count": 384,
                "dataset_source_group_count": 384,
                "dataset_source_reference_count": 384,
                **{
                    f"expected_{name.removesuffix('.jsonl').removesuffix('.json')}_sha256": sha256(
                        value
                    ).hexdigest()
                    for name, value in values.items()
                },
                **{
                    f"expected_{name.removesuffix('.jsonl').removesuffix('.json')}_byte_size": len(
                        value
                    )
                    for name, value in values.items()
                },
            }
            result = build_child_bundle(request)
            assert result["train_count"] == 256
            assert result["validation_count"] == 128
            assert set(os.listdir(stage.stage_fd)) == set(TRAINING_JOB_FILES) | {
                ".deptslm-stage-owner"
            }
            assert "provenance.jsonl" not in os.listdir(stage.stage_fd)
            copied = os.open("train.jsonl", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage.stage_fd)
            try:
                assert os.read(copied, len(train)) == train
            finally:
                os.close(copied)
            stage.close()
    finally:
        for descriptor in fds.values():
            os.close(descriptor)


def test_final_transaction_check_never_renews_an_elapsed_parent_lease(monkeypatch) -> None:
    """Final checks are local-only so a locked final transaction cannot self-block."""

    renewals: list[object] = []
    monkeypatch.setattr(
        training_job_queue,
        "renew_lease",
        lambda *_args: renewals.append(object()),
    )
    lease = training_job_queue._Lease(  # noqa: SLF001 - exercise the lease boundary.
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        lease_seconds=1,
        operation_seconds=10,
        should_stop=lambda: False,
    )
    lease.last_heartbeat = time.monotonic() - 10
    lease.final_transaction_check()
    assert not renewals
    lease.renew_before_final_transaction()
    assert len(renewals) == 1


def test_each_parent_operation_receives_a_fresh_independent_deadline(monkeypatch) -> None:
    """An exhausted operation must not consume the next operation's budget."""

    first = training_job_queue._new_operation_lease(  # noqa: SLF001
        None,
        None,
        lease_seconds=1,
        operation_seconds=10,
        should_stop=lambda: False,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(training_job_queue.time, "monotonic", lambda: first.deadline + 1)
    with pytest.raises(training_job_queue.TrainingJobQueueError, match="worker_timeout"):
        first.final_transaction_check()
    second = training_job_queue._new_operation_lease(  # noqa: SLF001
        None,
        None,
        lease_seconds=1,
        operation_seconds=10,
        should_stop=lambda: False,  # type: ignore[arg-type]
    )
    second.final_transaction_check()
    assert second.deadline > first.deadline
