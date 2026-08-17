"""Unit coverage for the Phase 12.4 immutable routing boundary."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

SERVICE_ROOT = Path(__file__).parents[3] / "services" / "adapter-runtime"
# Phase 12.2 uses the same import package name for its separate runtime.  Load
# this production package in isolation, then remove its module entries so the
# existing evaluation-runtime tests retain their own package boundary.
for _name in tuple(sys.modules):
    if _name == "deptslm_adapter_runtime" or _name.startswith("deptslm_adapter_runtime."):
        del sys.modules[_name]
sys.path.insert(0, str(SERVICE_ROOT))

from deptslm_adapter_runtime import child as adapter_child  # noqa: E402
from deptslm_adapter_runtime import supervisor as adapter_supervisor  # noqa: E402
from deptslm_adapter_runtime.child import (  # noqa: E402
    ChildError,
    Session,
    _validate_generation,
)
from deptslm_adapter_runtime.main import _validate_request  # noqa: E402
from deptslm_adapter_runtime.settings import (  # noqa: E402
    AdapterRuntimeConfigurationError,
    AdapterRuntimeSettings,
)  # noqa: E402
from deptslm_adapter_runtime.supervisor import (  # noqa: E402
    AdapterRuntimeSupervisor,
    AdapterRuntimeSupervisorError,
    _encode_frame,
    _target_authority,
    _target_key,
)

from app.adapter_runtime_client import AdapterRuntimeClient  # noqa: E402
from app.adapter_runtime_contract import RuntimeTarget  # noqa: E402
from app.rag_domain import (  # noqa: E402
    ANSWER_CONTRACT_VERSION,
    PROMPT_VERSION,
    EvidenceSource,
    RagContractError,
    runtime_generation_request,
)
from app.rag_runtime_router import RoutedRagRuntime  # noqa: E402

sys.path.remove(str(SERVICE_ROOT))
for _name in tuple(sys.modules):
    if _name == "deptslm_adapter_runtime" or _name.startswith("deptslm_adapter_runtime."):
        del sys.modules[_name]

pytestmark = pytest.mark.unit


def _target(*, department_id=None, adapter_version=3) -> RuntimeTarget:
    department_id = department_id or uuid4()
    return RuntimeTarget(
        department_id=department_id,
        target_kind="adapter",
        deployment_id=uuid4(),
        deployment_version=4,
        deployment_row_version=2,
        base_model_id="Qwen/Qwen3-0.6B",
        base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        adapter_id=uuid4(),
        adapter_version=adapter_version,
        review_id=uuid4(),
        review_version=5,
        evaluation_id=uuid4(),
        evaluation_version=6,
        suite_id=uuid4(),
        suite_version=7,
        registry_attempt_id=uuid4(),
        registry_attempt_version=8,
        registry_publication_attempt_id=uuid4(),
        registry_attempt_number=9,
        registry_execution_scope_id=uuid4(),
        registry_manifest_sha256="a" * 64,
        adapter_config_sha256="b" * 64,
        adapter_config_byte_size=100,
        adapter_model_sha256="c" * 64,
        adapter_model_byte_size=200,
        dependency_id=uuid4(),
        dependency_version=10,
    )


def test_runtime_target_fingerprint_is_closed_and_shape_is_fail_closed() -> None:
    target = _target()
    changed = _target(department_id=target.department_id)
    assert set(target.canonical_fields()) == {
        "department_id",
        "target_kind",
        "deployment_id",
        "deployment_version",
        "deployment_row_version",
        "base_model_id",
        "base_model_revision",
        "adapter_id",
        "adapter_version",
        "review_id",
        "review_version",
        "evaluation_id",
        "evaluation_version",
        "suite_id",
        "suite_version",
        "registry_attempt_id",
        "registry_attempt_version",
        "registry_publication_attempt_id",
        "registry_attempt_number",
        "registry_execution_scope_id",
        "registry_manifest_sha256",
        "adapter_config_sha256",
        "adapter_config_byte_size",
        "adapter_model_sha256",
        "adapter_model_byte_size",
        "dependency_id",
        "dependency_version",
    }
    assert target.fingerprint != changed.fingerprint
    with pytest.raises(ValueError, match="implicit base"):
        RuntimeTarget(
            department_id=uuid4(),
            target_kind="base",
            deployment_id=None,
            deployment_version=1,
            deployment_row_version=None,
            base_model_id="Qwen/Qwen3-0.6B",
            base_model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
        )


def test_routed_runtime_keeps_query_embedding_on_base_and_never_falls_back() -> None:
    class Base:
        def __init__(self):
            self.calls = []

        def query_embedding(self, question):
            self.calls.append(("embed", question))
            return [0.0]

        def generate(self, question, evidence, **kwargs):
            self.calls.append(("base", question, kwargs))
            return {"status": "answered"}

    class Adapter:
        def __init__(self):
            self.calls = []

        def generate(self, target, question, evidence):
            self.calls.append((target, question, evidence))
            return {"status": "answered"}

    base = Base()
    adapter = Adapter()
    target = _target()
    routed = RoutedRagRuntime(base, adapter, target)
    assert routed.query_embedding("q") == [0.0]
    assert routed.generate("q", ()) == {"status": "answered"}
    assert base.calls == [("embed", "q")]
    assert adapter.calls == [(target, "q", ())]

    with pytest.raises(RagContractError, match="adapter_runtime_unavailable"):
        RoutedRagRuntime(base, None, target).generate("q", ())
    assert not any(call[0] == "base" for call in base.calls)


def test_adapter_runtime_client_requires_served_fingerprint_and_closed_response() -> None:
    target = _target()
    evidence = (EvidenceSource("S1", "approved"),)

    def handler(request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)
        assert value["target_fingerprint"] == target.fingerprint
        assert "seed" not in value
        return httpx.Response(
            200,
            json={
                "status": "answered",
                "answer": "Approved [S1]",
                "citations": ["S1"],
                "served_target_fingerprint": target.fingerprint,
            },
        )

    client = AdapterRuntimeClient(
        "http://adapter-runtime:8012",
        "x" * 32,
        5,
        transport=httpx.MockTransport(handler),
    )
    assert client.generate(target, "question", evidence) == {
        "status": "answered",
        "answer": "Approved [S1]",
        "citations": ["S1"],
    }

    bad = AdapterRuntimeClient(
        "http://adapter-runtime:8012",
        "x" * 32,
        5,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"status": "answered", "answer": "Approved [S1]", "citations": ["S1"]},
            )
        ),
    )
    with pytest.raises(RagContractError, match="adapter_runtime_target_mismatch"):
        bad.generate(target, "question", evidence)

    failed = AdapterRuntimeClient(
        "http://adapter-runtime:8012",
        "x" * 32,
        5,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"code": "adapter_load_failed"})
        ),
    )
    with pytest.raises(RagContractError, match="adapter_load_failed"):
        failed.generate(target, "question", evidence)


def test_adapter_transport_timeout_is_separate_bounded_and_maps_timeout(monkeypatch) -> None:
    target = _target()
    evidence = (EvidenceSource("S1", "approved"),)
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return nullcontext(
                httpx.Response(
                    200,
                    json={
                        "status": "answered",
                        "answer": "Approved [S1]",
                        "citations": ["S1"],
                        "served_target_fingerprint": target.fingerprint,
                    },
                )
            )

    monkeypatch.setattr("app.adapter_runtime_client.httpx.Client", Client)
    client = AdapterRuntimeClient(
        "http://adapter-runtime:8012", "x" * 32, 450, transport=httpx.MockTransport(lambda _: None)
    )
    assert client.generate(target, "question", evidence)["status"] == "answered"
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 450
    assert timeout.connect == 10
    assert timeout.write == 10
    assert timeout.pool == 5

    class TimeoutClient(Client):
        def stream(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("outer adapter deadline")

    monkeypatch.setattr("app.adapter_runtime_client.httpx.Client", TimeoutClient)
    with pytest.raises(RagContractError, match="adapter_runtime_timeout"):
        client.generate(target, "question", evidence)


def test_runtime_http_request_is_exact_and_rejects_client_controls() -> None:
    target = _target()
    payload = {
        "operation": "generate",
        "target": "adapter",
        **target.adapter_request_fields(),
        "question": "question",
        "evidence": [{"source_id": "S1", "text": "approved"}],
        "prompt_version": PROMPT_VERSION,
        "answer_contract_version": ANSWER_CONTRACT_VERSION,
    }
    assert _validate_request(payload)["target_fingerprint"] == target.fingerprint
    assert _validate_generation(payload) == ("question", payload["evidence"])
    for forbidden in (
        "seed",
        "temperature",
        "adapter_path",
        "model_path",
        "fallback",
        "timeout",
        "request_timeout",
        "adapter_runtime_request_timeout_seconds",
    ):
        invalid = {**payload, forbidden: 1}
        with pytest.raises(Exception):
            _validate_request(invalid)


def test_runtime_settings_reject_fake_provider_and_token_reuse(monkeypatch, tmp_path) -> None:
    (tmp_path / "model_cache").mkdir()
    (tmp_path / "adapters" / "registry").mkdir(parents=True)
    common = {
        "ENVIRONMENT": "development",
        "DEPTSLM_DATA_DIR": str(tmp_path),
        "DEPTSLM_ADAPTER_RUNTIME_TOKEN": "x" * 32,
        "DEPTSLM_ADAPTER_RUNTIME_BASE_REVISION": "c1899de289a04d12100db370d81485cdf75e47ca",
    }
    for name, value in common.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DEPTSLM_ADAPTER_RUNTIME_PROVIDER", "fake")
    with pytest.raises(AdapterRuntimeConfigurationError):
        AdapterRuntimeSettings.from_environment()
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_RAG_RUNTIME_TOKEN", "x" * 32)
    with pytest.raises(AdapterRuntimeConfigurationError):
        AdapterRuntimeSettings.from_environment()


def test_child_runtime_settings_are_secret_free(monkeypatch, tmp_path) -> None:
    (tmp_path / "model_cache").mkdir()
    (tmp_path / "adapters" / "registry").mkdir(parents=True)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DEPTSLM_ADAPTER_RUNTIME_TOKEN", raising=False)
    monkeypatch.setenv(
        "DEPTSLM_ADAPTER_RUNTIME_BASE_REVISION",
        "c1899de289a04d12100db370d81485cdf75e47ca",
    )
    for name in (
        "DATABASE_URL",
        "DEPTSLM_QDRANT_URL",
        "DEPTSLM_QDRANT_API_KEY",
        "DEPTSLM_AUTH_SECRET",
        "DEPTSLM_RAG_RUNTIME_TOKEN",
        "DEPTSLM_ADAPTER_EVAL_RUNTIME_TOKEN",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = AdapterRuntimeSettings.from_environment(require_token=False)
    assert settings.token == ""
    assert "DEPTSLM_ADAPTER_RUNTIME_TOKEN" not in settings.child_environment()


def test_supervisor_target_key_includes_department_and_artifact_authority() -> None:
    target = _target()
    payload = {"operation": "generate", "target": target.adapter_request_fields()}
    key = _target_key(payload)
    assert key[0] == str(target.department_id)
    assert key[1] == str(target.adapter_id)
    assert key[-1] == target.fingerprint


def test_target_authority_is_closed_and_child_requires_target_load(monkeypatch, tmp_path) -> None:
    target = _target().adapter_request_fields()
    authority = _target_authority(target)
    assert authority["target"] == "adapter"
    assert set(authority) == {
        "target",
        "runtime_contract_version",
        "target_fingerprint",
        *target.keys(),
    }

    class Copy:
        def close(self):
            return None

    settings = AdapterRuntimeSettings(
        data_dir=tmp_path,
        model_cache=tmp_path / "model_cache",
        registry=tmp_path / "registry",
        token="",
        provider="fake",
    )
    session = Session(settings)
    calls = 0

    def verify(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Copy()

    monkeypatch.setattr(adapter_child, "verify_and_copy_adapter", verify)
    with pytest.raises(ChildError, match="adapter_runtime_target_mismatch"):
        session.generate({"operation": "generate", **target})
    assert (
        session.load_target(authority)["loaded_target_fingerprint"] == target["target_fingerprint"]
    )
    assert session.load_target(authority)["status"] == "target_ready"
    assert calls == 1
    response = session.generate(
        {
            "operation": "generate",
            "target": "adapter",
            **target,
            **runtime_generation_request("question", (EvidenceSource("S1", "approved"),)),
        }
    )
    assert response["served_target_fingerprint"] == target["target_fingerprint"]
    session.close()


def test_real_target_load_passes_one_exact_generation_path_to_both_loaders(monkeypatch, tmp_path):
    target = _target().adapter_request_fields()
    generation_path = tmp_path / "model_cache" / "qwen3-0.6b-reviewed"
    generation_path.mkdir(parents=True)
    settings = AdapterRuntimeSettings(
        data_dir=tmp_path,
        model_cache=tmp_path / "model_cache",
        registry=tmp_path / "registry",
        token="",
        provider="real",
    )

    class Copy:
        def close(self):
            return None

    loaded = []
    monkeypatch.setattr(adapter_child, "verify_and_copy_adapter", lambda *_args, **_kwargs: Copy())
    monkeypatch.setattr(
        adapter_child,
        "validate_generation_model_store",
        lambda data_dir: (
            loaded.append(("validate", data_dir)) or SimpleNamespace(path=generation_path)
        ),
    )
    monkeypatch.setattr(
        adapter_child,
        "_load_tokenizer",
        lambda path: loaded.append(("tokenizer", path)) or SimpleNamespace(model_max_length=8192),
    )
    monkeypatch.setattr(
        adapter_child,
        "load_adapter_model",
        lambda _copy, path, *, tokenizer_limit: (
            loaded.append(("model", path, tokenizer_limit)) or object()
        ),
    )
    session = Session(settings)
    result = session.load_target(_target_authority(target))
    assert result == {
        "status": "target_ready",
        "loaded_target_fingerprint": target["target_fingerprint"],
    }
    assert loaded == [
        ("validate", tmp_path),
        ("tokenizer", generation_path),
        ("model", generation_path, 8192),
    ]
    assert settings.model_cache != generation_path
    session.close()


class _ControlledRuntimeStdin:
    def __init__(self, process, *, load_delay=0.0, generation_delay=0.0):
        self.process = process
        self.load_delay = load_delay
        self.generation_delay = generation_delay
        self.closed = False

    def write(self, frame: bytes) -> None:
        request = json.loads(frame[4:].decode())
        self.process.operation = request["operation"]
        self.process.target = request["target"]

    async def drain(self) -> None:
        delay = (
            self.load_delay if self.process.operation == "load_target" else self.generation_delay
        )
        if delay:
            await asyncio.sleep(delay)
        if self.closed:
            return
        if self.process.operation == "load_target":
            value = {
                "status": "target_ready",
                "loaded_target_fingerprint": self.process.target["target_fingerprint"],
            }
        else:
            value = {
                "status": "answered",
                "answer": "Approved [S1]",
                "citations": ["S1"],
                "served_target_fingerprint": self.process.target["target_fingerprint"],
            }
        self.process.stdout.feed_data(_encode_frame(value))

    def close(self) -> None:
        self.closed = True


class _ControlledRuntimeProcess:
    _next_pid = 50_000

    def __init__(self, *, load_delay=0.0, generation_delay=0.0):
        _ControlledRuntimeProcess._next_pid += 1
        self.pid = _ControlledRuntimeProcess._next_pid
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.operation = None
        self.target = None
        self.stdin = _ControlledRuntimeStdin(
            self, load_delay=load_delay, generation_delay=generation_delay
        )
        self.waited = False

    async def wait(self):
        self.waited = True
        self.returncode = 0
        self.stdout.feed_eof()
        return 0


def test_supervised_target_load_has_its_own_clock_and_retires_changed_targets(monkeypatch):
    async def scenario():
        created = []
        delays = iter(((0.02, 0.0), (0.0, 0.0)))

        async def create(*_args, **_kwargs):
            load_delay, generation_delay = next(delays)
            process = _ControlledRuntimeProcess(
                load_delay=load_delay, generation_delay=generation_delay
            )
            process.stdout.feed_data(_encode_frame({"ready": True}))
            created.append(process)
            return process

        monkeypatch.setattr(adapter_supervisor.asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(adapter_supervisor.os, "killpg", lambda *_args: None)
        settings = SimpleNamespace(child_environment=lambda: {})
        supervisor = AdapterRuntimeSupervisor(
            settings,
            startup_timeout_seconds=0.1,
            target_load_timeout_seconds=0.1,
            generation_timeout_seconds=0.1,
        )
        target = _target().adapter_request_fields()
        await supervisor.start()
        assert supervisor.ready and not supervisor.target_ready
        result = await supervisor.request({"target": target})
        assert result["served_target_fingerprint"] == target["target_fingerprint"]
        assert supervisor.target_ready and len(created) == 1
        await supervisor.request({"target": target})
        assert len(created) == 1
        changed = _target(adapter_version=4).adapter_request_fields()
        await supervisor.request({"target": changed})
        assert len(created) == 2
        assert created[0].waited
        await supervisor.close()
        assert created[1].waited

    asyncio.run(scenario())


def test_supervised_target_load_timeout_reaps_without_fallback(monkeypatch):
    async def scenario():
        process = _ControlledRuntimeProcess(load_delay=0.2)
        process.stdout.feed_data(_encode_frame({"ready": True}))

        async def create(*_args, **_kwargs):
            return process

        monkeypatch.setattr(adapter_supervisor.asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(adapter_supervisor.os, "killpg", lambda *_args: None)
        supervisor = AdapterRuntimeSupervisor(
            SimpleNamespace(child_environment=lambda: {}),
            startup_timeout_seconds=0.1,
            target_load_timeout_seconds=0.01,
            generation_timeout_seconds=0.1,
        )
        with pytest.raises(AdapterRuntimeSupervisorError, match="adapter_runtime_timeout"):
            await supervisor.request({"target": _target().adapter_request_fields()})
        assert process.waited
        assert not supervisor.ready and not supervisor.target_ready
        await supervisor.close()

    asyncio.run(scenario())


def test_supervised_generation_timeout_is_separate_and_reaps(monkeypatch):
    async def scenario():
        process = _ControlledRuntimeProcess(generation_delay=0.2)
        process.stdout.feed_data(_encode_frame({"ready": True}))

        async def create(*_args, **_kwargs):
            return process

        monkeypatch.setattr(adapter_supervisor.asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(adapter_supervisor.os, "killpg", lambda *_args: None)
        supervisor = AdapterRuntimeSupervisor(
            SimpleNamespace(child_environment=lambda: {}),
            startup_timeout_seconds=0.1,
            target_load_timeout_seconds=0.1,
            generation_timeout_seconds=0.01,
        )
        with pytest.raises(AdapterRuntimeSupervisorError, match="adapter_runtime_timeout"):
            await supervisor.request({"target": _target().adapter_request_fields()})
        assert process.waited
        assert not supervisor.ready and not supervisor.target_ready

    asyncio.run(scenario())


def test_supervised_target_load_cancellation_reaps_child(monkeypatch):
    async def scenario():
        process = _ControlledRuntimeProcess(load_delay=0.2)
        process.stdout.feed_data(_encode_frame({"ready": True}))

        async def create(*_args, **_kwargs):
            return process

        monkeypatch.setattr(adapter_supervisor.asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(adapter_supervisor.os, "killpg", lambda *_args: None)
        supervisor = AdapterRuntimeSupervisor(
            SimpleNamespace(child_environment=lambda: {}),
            startup_timeout_seconds=0.1,
            target_load_timeout_seconds=0.5,
            generation_timeout_seconds=0.1,
        )
        task = asyncio.create_task(
            supervisor.request({"target": _target().adapter_request_fields()})
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.waited
        assert not supervisor.ready and not supervisor.target_ready
        await supervisor.close()

    asyncio.run(scenario())


def test_supervised_target_load_shutdown_reaps_child(monkeypatch):
    async def scenario():
        process = _ControlledRuntimeProcess(load_delay=0.2)
        process.stdout.feed_data(_encode_frame({"ready": True}))

        async def create(*_args, **_kwargs):
            return process

        monkeypatch.setattr(adapter_supervisor.asyncio, "create_subprocess_exec", create)
        monkeypatch.setattr(adapter_supervisor.os, "killpg", lambda *_args: None)
        supervisor = AdapterRuntimeSupervisor(
            SimpleNamespace(child_environment=lambda: {}),
            startup_timeout_seconds=0.1,
            target_load_timeout_seconds=0.5,
            generation_timeout_seconds=0.1,
        )
        task = asyncio.create_task(
            supervisor.request({"target": _target().adapter_request_fields()})
        )
        await asyncio.sleep(0.01)
        await supervisor.close()
        with pytest.raises(AdapterRuntimeSupervisorError):
            await task
        assert process.waited
        assert not supervisor.ready and not supervisor.target_ready

    asyncio.run(scenario())
