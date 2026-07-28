"""Focused Phase 10 source, deterministic split, and private-artifact tests."""

from __future__ import annotations

import hashlib
import os
import time
from uuid import uuid4

import pytest

from app.authorization import DepartmentScope
from app.sft_artifacts import DATASET_FILES, SftArtifactError, SftArtifactStore
from app.sft_domain import (
    SftContractError,
    canonical_json_bytes,
    parse_source_bundle,
    split_examples,
)
from app.sft_supervision import run_claimed_operation


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
        / ".ownership"
    )
    marker.write_bytes(b"")
    assert store.remove_owned_source_stage(scope, parsed.source_bundle_id, parsed.import_attempt_id)


class _OperationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def test_supervision_heartbeats_and_returns_child_result() -> None:
    heartbeats: list[bool] = []
    result = run_claimed_operation(
        timeout_seconds=2,
        heartbeat_seconds=1,
        should_stop=lambda: False,
        heartbeat=lambda: heartbeats.append(True),
        error=_OperationError,
        operation=lambda: "complete",
    )
    assert result == "complete"
    assert heartbeats


def test_supervision_timeout_terminates_blocked_child_group() -> None:
    with pytest.raises(_OperationError, match="worker_timeout"):
        run_claimed_operation(
            timeout_seconds=1,
            heartbeat_seconds=1,
            should_stop=lambda: False,
            heartbeat=lambda: None,
            error=_OperationError,
            operation=lambda: time.sleep(5),
        )


def test_supervision_shutdown_prevents_child_publication() -> None:
    with pytest.raises(_OperationError, match="worker_shutdown"):
        run_claimed_operation(
            timeout_seconds=2,
            heartbeat_seconds=1,
            should_stop=lambda: True,
            heartbeat=lambda: None,
            error=_OperationError,
            operation=lambda: os.getpid(),
        )
