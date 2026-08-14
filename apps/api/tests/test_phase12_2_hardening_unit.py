"""Deterministic Phase 12.2 copy-authority and model-contract tests."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SERVICE_ROOT = Path(__file__).parents[2] / "services" / "adapter-eval-runtime"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from deptslm_adapter_runtime import (  # noqa: E402
    candidate_child,  # noqa: E402
    loader,  # noqa: E402
)
from deptslm_adapter_runtime.loader import (  # noqa: E402
    AdapterRuntimeError,
    VerifiedAdapterCopy,
)
from deptslm_runtime import models as production_models  # noqa: E402
from deptslm_worker import adapter_evaluation_pipeline  # noqa: E402

from app import generation_contract  # noqa: E402
from app.adapter_evaluation_queue import (  # noqa: E402
    AdapterEvaluationQueueError,
)
from app.model_store import ModelLocation  # noqa: E402


def _registry_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    department_id = uuid4()
    adapter_id = uuid4()
    publication_id = uuid4()
    registry = tmp_path / "registry"
    final = registry / str(department_id) / str(adapter_id)
    final.mkdir(parents=True, mode=0o700)
    os.chmod(registry, 0o700)
    os.chmod(registry / str(department_id), 0o700)
    config = b'{"r":1}'
    model = b"safe-model-bytes"
    config_sha = hashlib.sha256(config).hexdigest()
    model_sha = hashlib.sha256(model).hexdigest()
    manifest = {
        "department_id": str(department_id),
        "adapter_id": str(adapter_id),
        "publication_attempt_id": str(publication_id),
        "attempt_number": 1,
        "files": {
            "adapter_config.json": {"sha256": config_sha, "byte_size": len(config)},
            "adapter_model.safetensors": {"sha256": model_sha, "byte_size": len(model)},
        },
    }
    (final / "manifest.json").write_bytes(b"manifest\n")
    (final / "adapter_config.json").write_bytes(config)
    (final / "adapter_model.safetensors").write_bytes(model)
    for path in final.iterdir():
        os.chmod(path, 0o600)
    monkeypatch.setattr(loader, "parse_registry_manifest", lambda _raw: manifest)
    monkeypatch.setattr(loader, "validate_adapter_config", lambda _raw: None)
    monkeypatch.setattr(loader, "validate_safetensors_header", lambda _file, _size: None)
    return registry, final, department_id, adapter_id, publication_id, config, model, manifest


def _copy(registry, department_id, adapter_id, publication_id, config, model, **kwargs):
    return loader.verify_and_copy_adapter(
        registry,
        department_id=department_id,
        adapter_id=adapter_id,
        adapter_version=1,
        registry_publication_attempt_id=publication_id,
        registry_attempt_number=1,
        expected_manifest_sha256=hashlib.sha256(
            loader.canonical_json_bytes(kwargs.pop("manifest"))
        ).hexdigest(),
        expected_config_sha256=hashlib.sha256(config).hexdigest(),
        expected_config_byte_size=len(config),
        expected_model_sha256=hashlib.sha256(model).hexdigest(),
        expected_model_byte_size=len(model),
        **kwargs,
    )


def test_verified_copy_hashes_and_sizes_the_exact_loaded_bytes(tmp_path, monkeypatch):
    values = _registry_fixture(tmp_path, monkeypatch)
    registry, _final, department_id, adapter_id, publication_id, config, model, manifest = values
    copy = _copy(
        registry,
        department_id,
        adapter_id,
        publication_id,
        config,
        model,
        manifest=manifest,
    )
    try:
        assert copy.config_path.read_bytes() == config
        assert copy.model_path.read_bytes() == model
        assert copy.config_path.stat().st_size == len(config)
        assert copy.model_path.stat().st_size == len(model)
    finally:
        copy.close()


@pytest.mark.parametrize("target", ["adapter_config.json", "adapter_model.safetensors"])
def test_same_inode_same_size_source_mutation_fails_closed(tmp_path, monkeypatch, target):
    values = _registry_fixture(tmp_path, monkeypatch)
    registry, final, department_id, adapter_id, publication_id, config, model, manifest = values
    mutated = False

    def mutate(_destination: Path, total: int) -> None:
        nonlocal mutated
        if not mutated and _destination.name == target and total == 0:
            source = final / target
            descriptor = os.open(source, os.O_WRONLY)
            try:
                os.pwrite(descriptor, b"X", 0)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            mutated = True

    with pytest.raises(AdapterRuntimeError, match="adapter_authority_changed"):
        _copy(
            registry,
            department_id,
            adapter_id,
            publication_id,
            config,
            model,
            manifest=manifest,
            copy_hook=mutate,
        )


def test_source_directory_entry_replacement_fails_closed(tmp_path, monkeypatch):
    values = _registry_fixture(tmp_path, monkeypatch)
    registry, final, department_id, adapter_id, publication_id, config, model, manifest = values

    def replace(_destination: Path, total: int) -> None:
        if _destination.name == "adapter_config.json" and total == 0:
            old = final / "adapter_config.json"
            old.rename(final / "parked")
            replacement = final / "adapter_config.json"
            replacement.write_bytes(config)
            os.chmod(replacement, 0o600)

    with pytest.raises(AdapterRuntimeError, match="adapter_authority_changed"):
        _copy(
            registry,
            department_id,
            adapter_id,
            publication_id,
            config,
            model,
            manifest=manifest,
            copy_hook=replace,
        )
    assert (final / "parked").read_bytes() == config
    assert (final / "adapter_config.json").read_bytes() == config


def test_ephemeral_copy_mutation_is_rejected_before_model_load(tmp_path, monkeypatch):
    values = _registry_fixture(tmp_path, monkeypatch)
    registry, _final, department_id, adapter_id, publication_id, config, model, manifest = values
    mutated = False

    def mutate(destination: Path, total: int) -> None:
        nonlocal mutated
        if not mutated and destination.name == "adapter_config.json" and total:
            with destination.open("r+b") as handle:
                handle.seek(0)
                handle.write(b"X")
                handle.flush()
                os.fsync(handle.fileno())
            mutated = True

    with pytest.raises(AdapterRuntimeError, match="adapter_authority_changed"):
        _copy(
            registry,
            department_id,
            adapter_id,
            publication_id,
            config,
            model,
            manifest=manifest,
            copy_hook=mutate,
        )


def test_copy_authority_failure_prevents_peft_load(tmp_path, monkeypatch):
    department_id = uuid4()
    adapter_id = uuid4()
    publication_id = uuid4()
    calls: list[object] = []

    def reject_copy(*_args, **_kwargs):
        raise AdapterRuntimeError("adapter_authority_changed")

    monkeypatch.setattr(candidate_child, "verify_and_copy_adapter", reject_copy)
    monkeypatch.setattr(
        candidate_child,
        "load_candidate_model",
        lambda *_args, **_kwargs: calls.append(True),
    )
    payload = {
        "target": "candidate",
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "department_id": str(department_id),
        "adapter_id": str(adapter_id),
        "adapter_version": 1,
        "registry_publication_attempt_id": str(publication_id),
        "registry_attempt_number": 1,
        "registry_manifest_sha256": "a" * 64,
        "adapter_config_sha256": "b" * 64,
        "adapter_config_byte_size": 2,
        "adapter_model_sha256": "c" * 64,
        "adapter_model_byte_size": 5,
        "question": "What is approved?",
        "evidence": [{"source_id": "S1", "text": "Approved."}],
        "prompt_version": "phase7-grounded-answer-prompt-v1",
        "answer_contract_version": "phase7-grounded-answer-v1",
        "seed": 7,
    }
    session = candidate_child.CandidateSession(tmp_path, "real")
    try:
        with pytest.raises(candidate_child.CandidateChildError) as caught:
            session.generate(payload)
        assert caught.value.code == "adapter_authority_changed"
        assert calls == []
    finally:
        session.close()


def test_candidate_base_load_uses_exact_validated_generation_path(monkeypatch, tmp_path):
    exact = tmp_path / "validated-generation"
    calls: dict[str, object] = {}

    def validate(data_dir: Path) -> ModelLocation:
        calls["data_dir"] = data_dir
        return ModelLocation(exact, "c1899de289a04d12100db370d81485cdf75e47ca")

    class FakeBase:
        config = SimpleNamespace(max_position_embeddings=40960)

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls["base"] = (args, kwargs)
            return FakeBase()

    class FakePeft:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls["peft"] = (args, kwargs)
            return "loaded"

    monkeypatch.setattr(loader, "validate_generation_model_store", validate)
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        types.SimpleNamespace(AutoModelForCausalLM=FakeAutoModel),
    )
    monkeypatch.setitem(sys.modules, "peft", types.SimpleNamespace(PeftModel=FakePeft))
    copy = VerifiedAdapterCopy(tmp_path / "copy", tmp_path / "config", tmp_path / "model", object())
    assert (
        loader.load_candidate_model(
            copy,
            tmp_path / "data",
            tokenizer_limit=40960,
        )
        == "loaded"
    )
    assert calls["base"] == (
        (str(exact),),
        {"local_files_only": True, "trust_remote_code": False, "use_safetensors": True},
    )
    assert calls["peft"][1] == {
        "local_files_only": True,
        "is_trainable": False,
    }


def test_candidate_context_mismatch_fails_closed_before_peft(monkeypatch, tmp_path):
    exact = tmp_path / "validated-generation"

    monkeypatch.setattr(
        loader,
        "validate_generation_model_store",
        lambda _data_dir: ModelLocation(
            exact,
            "c1899de289a04d12100db370d81485cdf75e47ca",
        ),
    )

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*_args, **_kwargs):
            return SimpleNamespace(config=SimpleNamespace(max_position_embeddings=40960))

    class FakePeft:
        @staticmethod
        def from_pretrained(*_args, **_kwargs):
            raise AssertionError("PEFT must not load after context rejection")

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        types.SimpleNamespace(AutoModelForCausalLM=FakeAutoModel),
    )
    monkeypatch.setitem(sys.modules, "peft", types.SimpleNamespace(PeftModel=FakePeft))
    copy = VerifiedAdapterCopy(tmp_path / "copy", tmp_path / "config", tmp_path / "model", object())
    with pytest.raises(AdapterRuntimeError, match="candidate_adapter_load_failed"):
        loader.load_candidate_model(copy, tmp_path / "data", tokenizer_limit=8192)


def test_generation_budget_and_seed_contract_are_shared(monkeypatch):
    class Tokenizer:
        def __init__(self, count: int):
            self.count = count

        def apply_chat_template(self, *_args, **_kwargs):
            return {"input_ids": [[0] * self.count]}

    messages = [{"role": "user", "content": "Question"}]
    generation_contract.tokenize_generation_input(Tokenizer(8192), messages)
    with pytest.raises(generation_contract.GenerationContractError, match="model_input_too_large"):
        generation_contract.tokenize_generation_input(Tokenizer(8193), messages)

    seeds: list[tuple[str, int]] = []
    fake_numpy = types.SimpleNamespace(
        random=types.SimpleNamespace(seed=lambda value: seeds.append(("numpy", value)))
    )
    fake_torch = types.SimpleNamespace(
        manual_seed=lambda value: seeds.append(("torch", value)),
        cuda=types.SimpleNamespace(
            is_available=lambda: True,
            manual_seed_all=lambda value: seeds.append(("cuda", value)),
        ),
    )
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(
        generation_contract.random,
        "seed",
        lambda value: seeds.append(("python", value)),
    )
    generation_contract.initialize_generation_seed(7)
    assert seeds == [("python", 7), ("numpy", 7), ("torch", 7), ("cuda", 7)]


def test_candidate_and_production_generation_parameters_share_one_contract():
    for name in (
        "GENERATION_DO_SAMPLE",
        "GENERATION_TEMPERATURE",
        "GENERATION_TOP_P",
        "GENERATION_TOP_K",
        "GENERATION_MIN_P",
    ):
        assert getattr(candidate_child, name) == getattr(generation_contract, name)
        assert getattr(production_models, name) == getattr(generation_contract, name)


def _supervised_child(connection, delay: float, message: tuple | None) -> None:
    os.setsid()
    connection.send(("ready",))
    if message is None:
        while True:
            time.sleep(1)
    if delay:
        time.sleep(delay)
    connection.send(message)


def _start_supervised_child(delay: float, message: tuple | None):
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_supervised_child, args=(child, delay, message))
    process.start()
    child.close()
    return parent, process


def _supervision_settings(timeout: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        evaluation=SimpleNamespace(
            operation_timeout_seconds=timeout,
            heartbeat_seconds=0.05,
            lease_seconds=2,
        )
    )


def test_adapter_supervisor_heartbeats_and_reaps_completed_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, process = _start_supervised_child(0.22, ("result", "complete"))
    heartbeats: list[float] = []
    monkeypatch.setattr(
        adapter_evaluation_pipeline,
        "renew_lease",
        lambda *_args, **_kwargs: heartbeats.append(time.monotonic()),
    )
    result = adapter_evaluation_pipeline._wait_for_supervised_process(
        object(),
        _supervision_settings(),
        object(),
        lambda: False,
        parent,
        process,
        str,
    )
    assert result == "complete"
    assert len(heartbeats) >= 3
    assert not process.is_alive()


def test_adapter_supervisor_timeout_reaps_case_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, process = _start_supervised_child(0, None)
    monkeypatch.setattr(adapter_evaluation_pipeline, "renew_lease", lambda *_a, **_k: None)
    with pytest.raises(AdapterEvaluationQueueError) as caught:
        adapter_evaluation_pipeline._wait_for_supervised_process(
            object(),
            _supervision_settings(0.15),
            object(),
            lambda: False,
            parent,
            process,
            adapter_evaluation_pipeline._PairedCaseResult,
        )
    assert caught.value.code == "candidate_runtime_timeout"
    assert not process.is_alive()
    assert process.exitcode is not None


@pytest.mark.parametrize("stop_kind", ["claim_loss", "heartbeat_failure", "shutdown"])
def test_adapter_supervisor_terminates_on_claim_loss_heartbeat_or_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    stop_kind: str,
) -> None:
    parent, process = _start_supervised_child(0, None)
    calls = 0

    def renew(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if stop_kind == "claim_loss" and calls >= 2:
            raise AdapterEvaluationQueueError("claim_lost")
        if stop_kind == "heartbeat_failure" and calls >= 2:
            raise AdapterEvaluationQueueError("database_unavailable")

    monkeypatch.setattr(adapter_evaluation_pipeline, "renew_lease", renew)
    started = time.monotonic()
    with pytest.raises((AdapterEvaluationQueueError, adapter_evaluation_pipeline._WorkerStopped)):
        adapter_evaluation_pipeline._wait_for_supervised_process(
            object(),
            _supervision_settings(2),
            object(),
            lambda: stop_kind == "shutdown" and time.monotonic() - started > 0.08,
            parent,
            process,
            adapter_evaluation_pipeline._PairedCaseResult,
        )
    assert not process.is_alive()


def test_adapter_supervisor_rejects_malformed_child_result_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, process = _start_supervised_child(0, ("result", {"unexpected": True}))
    monkeypatch.setattr(adapter_evaluation_pipeline, "renew_lease", lambda *_a, **_k: None)
    with pytest.raises(AdapterEvaluationQueueError) as caught:
        adapter_evaluation_pipeline._wait_for_supervised_process(
            object(),
            _supervision_settings(),
            object(),
            lambda: False,
            parent,
            process,
            str,
        )
    assert caught.value.code == "database_unavailable"
    assert not process.is_alive()


def test_adapter_supervisor_rejects_unexpected_exit_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)

    def exit_child(connection):
        os.setsid()
        connection.close()

    process = context.Process(target=exit_child, args=(child,))
    process.start()
    child.close()
    monkeypatch.setattr(adapter_evaluation_pipeline, "renew_lease", lambda *_a, **_k: None)
    with pytest.raises(AdapterEvaluationQueueError) as caught:
        adapter_evaluation_pipeline._wait_for_supervised_process(
            object(),
            _supervision_settings(),
            object(),
            lambda: False,
            parent,
            process,
            str,
        )
    assert caught.value.code == "database_unavailable"
    assert not process.is_alive()
