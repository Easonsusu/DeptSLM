"""Unit coverage for the Phase 12.4 immutable routing boundary."""

from __future__ import annotations

import json
import sys
from pathlib import Path
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

from deptslm_adapter_runtime.child import _validate_generation  # noqa: E402
from deptslm_adapter_runtime.main import _validate_request  # noqa: E402
from deptslm_adapter_runtime.settings import (  # noqa: E402
    AdapterRuntimeConfigurationError,
    AdapterRuntimeSettings,
)  # noqa: E402
from deptslm_adapter_runtime.supervisor import _target_key  # noqa: E402

from app.adapter_runtime_client import AdapterRuntimeClient  # noqa: E402
from app.adapter_runtime_contract import RuntimeTarget  # noqa: E402
from app.rag_domain import (  # noqa: E402
    ANSWER_CONTRACT_VERSION,
    PROMPT_VERSION,
    EvidenceSource,
    RagContractError,
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
    for forbidden in ("seed", "temperature", "adapter_path", "model_path", "fallback"):
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


def test_supervisor_target_key_includes_department_and_artifact_authority() -> None:
    target = _target()
    payload = {"operation": "generate", "target": target.adapter_request_fields()}
    key = _target_key(payload)
    assert key[0] == str(target.department_id)
    assert key[1] == str(target.adapter_id)
    assert key[-1] == target.fingerprint
