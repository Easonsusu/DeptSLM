"""Focused Phase 10 source, deterministic split, and private-artifact tests."""

from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.authorization import DepartmentScope
from app.sft_artifacts import DATASET_FILES, STAGE_MARKER, SftArtifactError, SftArtifactStore
from app.sft_domain import (
    SftContractError,
    canonical_json_bytes,
    parse_source_bundle,
    split_examples,
)
from app.sft_services import _same_stable_file
from app.sft_supervision import SftChildOperation, _frame, run_claimed_operation


def _source() -> tuple[bytes, bytes]:
    department_id = uuid4()
    examples = [
        {
            "example_id": str(uuid4()),
            "group_id": str(uuid4()),
            "instruction": "First\r\nquestion",
            "response": "First answer",
            "source_chunk_ids": [str(uuid4())],
        },
        {
            "example_id": str(uuid4()),
            "group_id": str(uuid4()),
            "instruction": "Second question",
            "response": "Second answer",
            "source_chunk_ids": [str(uuid4()), str(uuid4())],
        },
    ]
    payload = b"".join(canonical_json_bytes(example) + b"\n" for example in examples)
    manifest = {
        "artifact_contract_version": "phase10-sft-source-v1",
        "department_id": str(department_id),
        "source_bundle_id": str(uuid4()),
        "import_attempt_id": str(uuid4()),
        "stage_id": str(uuid4()),
        "normalization_version": "phase10-sft-normalization-v1",
        "example_contract_version": "phase10-sft-example-v1",
        "example_count": 2,
        "group_count": 2,
        "source_reference_count": 3,
        "files": {
            "examples.jsonl": {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
            }
        },
    }
    return canonical_json_bytes(manifest) + b"\n", payload


def test_source_contract_normalizes_and_splits_deterministically() -> None:
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    assert parsed.examples[0].instruction == "First\nquestion"
    assert split_examples(parsed, build_id=uuid4())[0]
    build_id = uuid4()
    assert split_examples(parsed, build_id=build_id) == split_examples(parsed, build_id=build_id)


def test_source_contract_rejects_duplicate_canonical_pair() -> None:
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    duplicate = (
        canonical_json_bytes(
            {
                "example_id": str(uuid4()),
                "group_id": str(uuid4()),
                "instruction": parsed.examples[0].instruction,
                "response": parsed.examples[0].response,
                "source_chunk_ids": [str(uuid4())],
            }
        )
        + b"\n"
    )
    with pytest.raises(SftContractError):
        parse_source_bundle(manifest, examples + duplicate)


def test_source_contract_rejects_unsafe_unicode() -> None:
    manifest, examples = _source()
    assert b"First" in examples
    with pytest.raises(SftContractError):
        parse_source_bundle(manifest, examples.replace(b"First", "\u034f".encode("utf-8"), 1))


def test_private_artifacts_are_published_and_read_from_external_root(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    store = SftArtifactStore(tmp_path)
    scope = DepartmentScope(parsed.department_id)
    source = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    store.publish(source, allowlist=frozenset({"manifest.json", "examples.jsonl"}))
    assert store.read_source(scope, parsed.source_bundle_id) == (manifest, examples)

    build_id, attempt_id = uuid4(), uuid4()
    train = b'{"example_id":"x","messages":[]}\n'
    validation = b'{"example_id":"y","messages":[]}\n'
    provenance = b'{"example_id":"x","group_id":"x","split":"train","source_chunk_ids":[]}\n'
    dataset_manifest = (
        canonical_json_bytes(
            {
                "files": {
                    "train.jsonl": {
                        "sha256": hashlib.sha256(train).hexdigest(),
                        "byte_size": len(train),
                    },
                    "validation.jsonl": {
                        "sha256": hashlib.sha256(validation).hexdigest(),
                        "byte_size": len(validation),
                    },
                    "provenance.jsonl": {
                        "sha256": hashlib.sha256(provenance).hexdigest(),
                        "byte_size": len(provenance),
                    },
                }
            }
        )
        + b"\n"
    )
    dataset = store.stage_dataset(
        scope,
        build_id,
        attempt_id,
        manifest=dataset_manifest,
        train=train,
        validation=validation,
        provenance=provenance,
    )
    published = store.publish(dataset, allowlist=DATASET_FILES)
    assert published.resource_id == build_id


def test_final_removal_requires_exact_manifest_ownership(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    store = SftArtifactStore(tmp_path)
    staged = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    store.publish(staged, allowlist=frozenset({"manifest.json", "examples.jsonl"}))
    with pytest.raises(SftArtifactError):
        store.remove_owned_source_final(
            scope,
            parsed.source_bundle_id,
            parsed.import_attempt_id,
            expected={key: value for key, value in parsed.manifest.items() if key != "files"},
        )
    assert store.remove_owned_source_final(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        expected=parsed.manifest,
    )
    assert not store.remove_owned_source_final(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        expected=parsed.manifest,
    )


def test_stage_cleanup_is_exact_attempt_scoped(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    store = SftArtifactStore(tmp_path)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    source_id, attempt_id = parsed.source_bundle_id, parsed.import_attempt_id
    store.stage_source(scope, source_id, attempt_id, manifest=manifest, examples=examples)
    assert store.remove_owned_source_stage(scope, source_id, attempt_id)
    assert not store.remove_owned_source_stage(scope, source_id, attempt_id)


def test_stage_cleanup_recovers_partial_marker_without_parsing_it(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    store = SftArtifactStore(tmp_path)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    staged = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    staged.close()
    marker = (
        tmp_path
        / "training_datasets/.staging/sources"
        / str(scope.value)
        / str(parsed.source_bundle_id)
        / str(parsed.import_attempt_id)
        / STAGE_MARKER
    )
    marker.write_bytes(b"")
    assert store.remove_owned_source_stage(scope, parsed.source_bundle_id, parsed.import_attempt_id)


def test_stage_cleanup_recovers_zero_byte_actual_marker_and_partial_payload(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    store = SftArtifactStore(tmp_path)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    staged = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    staged.close()
    directory = (
        tmp_path
        / "training_datasets/.staging/sources"
        / str(scope.value)
        / str(parsed.source_bundle_id)
        / str(parsed.import_attempt_id)
    )
    (directory / STAGE_MARKER).write_bytes(b"partial")
    (directory / "partial-sensitive-payload").write_bytes(b"not parsed")
    assert store.remove_owned_source_stage(scope, parsed.source_bundle_id, parsed.import_attempt_id)


def test_stage_substitution_is_detected_without_deleting_replacement(tmp_path, monkeypatch) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    store = SftArtifactStore(tmp_path)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    staged = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    staged.close()
    directory = (
        tmp_path
        / "training_datasets/.staging/sources"
        / str(scope.value)
        / str(parsed.source_bundle_id)
        / str(parsed.import_attempt_id)
    )
    from app import sft_artifacts

    original = sft_artifacts._remove_contents

    def replace_after_empty(descriptor: int) -> None:
        original(descriptor)
        os.rmdir(directory)
        directory.mkdir(mode=0o700)

    monkeypatch.setattr(sft_artifacts, "_remove_contents", replace_after_empty)
    with pytest.raises(SftArtifactError, match="artifact_ownership_mismatch"):
        store.remove_owned_source_stage(scope, parsed.source_bundle_id, parsed.import_attempt_id)
    assert directory.is_dir()
    assert not any(directory.iterdir())


def test_stage_symlink_is_never_followed_or_removed(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    store = SftArtifactStore(tmp_path)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    staged = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    staged.close()
    directory = (
        tmp_path
        / "training_datasets/.staging/sources"
        / str(scope.value)
        / str(parsed.source_bundle_id)
        / str(parsed.import_attempt_id)
    )
    target = tmp_path / "foreign"
    target.mkdir(mode=0o700)
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(SftArtifactError):
        store.remove_owned_source_stage(scope, parsed.source_bundle_id, parsed.import_attempt_id)
    assert directory.is_symlink()
    assert target.is_dir()


def test_retained_final_descriptor_rejects_path_substitution(tmp_path) -> None:
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    store = SftArtifactStore(tmp_path)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    staged = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    published = store.publish(
        staged,
        allowlist=frozenset({"manifest.json", "examples.jsonl"}),
        expected=parsed.manifest,
        retain=True,
    )
    verification = store.verify_retained_final(
        published,
        allowlist=frozenset({"manifest.json", "examples.jsonl"}),
        expected=parsed.manifest,
    )
    target = (
        tmp_path / "training_datasets/sources" / str(scope.value) / str(parsed.source_bundle_id)
    )
    replacement = target.with_name("replacement")
    target.rename(replacement)
    target.mkdir(mode=0o700)
    try:
        with pytest.raises(SftArtifactError, match="artifact_ownership_mismatch"):
            verification.recheck_identity()
        assert target.is_dir()
    finally:
        verification.close()


def test_source_stability_ignores_atime_only_changes(tmp_path) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"content")
    before = os.stat(path)
    atime_changed = SimpleNamespace(
        st_mode=before.st_mode,
        st_dev=before.st_dev,
        st_ino=before.st_ino,
        st_uid=before.st_uid,
        st_nlink=before.st_nlink,
        st_size=before.st_size,
        st_mtime_ns=before.st_mtime_ns,
        st_ctime_ns=before.st_ctime_ns,
        st_atime_ns=before.st_atime_ns + 60_000_000_000,
    )
    assert _same_stable_file(before, atime_changed)


class _OperationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _child_request(tmp_path):
    (tmp_path / "training_datasets").mkdir(mode=0o700)
    manifest, examples = _source()
    parsed = parse_source_bundle(manifest, examples)
    scope = DepartmentScope(parsed.department_id)
    store = SftArtifactStore(tmp_path)
    source = store.stage_source(
        scope,
        parsed.source_bundle_id,
        parsed.import_attempt_id,
        manifest=manifest,
        examples=examples,
    )
    store.publish(source, allowlist=frozenset({"manifest.json", "examples.jsonl"})).close()
    source_fd = store.open_source_directory(scope, parsed.source_bundle_id)
    stage = store.prepare_dataset_stage(scope, uuid4(), uuid4())
    assert stage.stage_fd is not None
    authority = []
    for chunk_id in sorted(
        {chunk_id for item in parsed.examples for chunk_id in item.source_chunk_ids},
        key=lambda value: value.bytes,
    ):
        authority.append(
            {
                "document_id": str(uuid4()),
                "extraction_id": str(uuid4()),
                "indexing_id": str(uuid4()),
                "chunk_id": str(chunk_id),
                "vector_attempt_id": str(uuid4()),
            }
        )
    request = {
        "source_fd": source_fd,
        "stage_fd": stage.stage_fd,
        "department_id": str(parsed.department_id),
        "source_bundle_id": str(parsed.source_bundle_id),
        "build_id": str(stage.resource_id),
        "publication_attempt_id": str(stage.attempt_id),
        "attempt_number": 1,
        "code_revision": "a" * 40,
        "authority_fingerprint": "b" * 64,
        "authority": authority,
    }
    return store, source_fd, stage, request


def test_supervision_exec_child_heartbeats_and_returns_content_free_result(tmp_path) -> None:
    heartbeats: list[bool] = []
    store, source_fd, stage, request = _child_request(tmp_path)
    try:
        result = run_claimed_operation(
            timeout_seconds=5,
            heartbeat_seconds=1,
            should_stop=lambda: False,
            heartbeat=lambda: heartbeats.append(True),
            error=_OperationError,
            operation=SftChildOperation.BUILD_DATASET,
            request=request,
            pass_fds=(source_fd, stage.stage_fd),
        )
        assert set(result) == {
            "source",
            "publication_manifest",
            "files",
            "train_count",
            "validation_count",
            "authority_fingerprint",
        }
        assert b"First" not in repr(result).encode("utf-8")
        assert heartbeats
    finally:
        stage.close()
        os.close(source_fd)
        store.close()


def test_supervision_timeout_terminates_before_unbounded_child_io() -> None:
    with pytest.raises(_OperationError, match="worker_timeout"):
        run_claimed_operation(
            timeout_seconds=0,
            heartbeat_seconds=1,
            should_stop=lambda: False,
            heartbeat=lambda: None,
            error=_OperationError,
            operation=SftChildOperation.BUILD_DATASET,
            request={},
            pass_fds=(),
        )


def test_supervision_shutdown_prevents_child_publication() -> None:
    with pytest.raises(_OperationError, match="worker_shutdown"):
        run_claimed_operation(
            timeout_seconds=2,
            heartbeat_seconds=1,
            should_stop=lambda: True,
            heartbeat=lambda: None,
            error=_OperationError,
            operation=SftChildOperation.BUILD_DATASET,
            request={},
            pass_fds=(),
        )


def test_supervision_frame_bound_rejects_large_request_before_child_start() -> None:
    with pytest.raises(ValueError):
        _frame({"value": "x" * (64 * 1024 * 1024)}, 64 * 1024 * 1024)


def test_exec_child_has_no_parent_secret_or_unrelated_descriptor(monkeypatch) -> None:
    sentinel_read, sentinel_write = os.pipe()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://not-forwarded")
    monkeypatch.setenv("DEPTSLM_PHASE10_SENTINEL", "not-forwarded")
    try:
        result = run_claimed_operation(
            timeout_seconds=5,
            heartbeat_seconds=1,
            should_stop=lambda: False,
            heartbeat=lambda: None,
            error=_OperationError,
            operation=SftChildOperation.BOUNDARY_PROBE,
            request={"probe_fd": sentinel_read},
            pass_fds=(),
        )
    finally:
        os.close(sentinel_read)
        os.close(sentinel_write)
    assert result == {
        "database_url_present": False,
        "sentinel_present": False,
        "probe_fd_open": False,
        "environment_keys": ["PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"],
    }
