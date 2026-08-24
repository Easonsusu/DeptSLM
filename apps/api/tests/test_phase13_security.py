"""Phase 13 transport and public-surface security regressions."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from deptslm_runtime.models import RuntimeModels
from starlette.testclient import TestClient

from app.body_limit import (
    MAX_NON_UPLOAD_REQUEST_BODY_BYTES,
    NonUploadBodyLimitMiddleware,
)


async def _echo_app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": str(len(body)).encode()})


def test_non_upload_transport_rejects_declared_oversize_before_downstream() -> None:
    calls = 0

    async def app(scope, receive, send):
        nonlocal calls
        if scope["type"] == "http":
            calls += 1
        await _echo_app(scope, receive, send)

    with TestClient(NonUploadBodyLimitMiddleware(app)) as client:
        response = client.post(
            "/departments/not-an-upload-route",
            headers={"content-length": "65537"},
        )

    assert response.status_code == 413
    assert calls == 0


def test_non_upload_transport_bounds_chunked_body_and_replays_exact_limit() -> None:
    with TestClient(NonUploadBodyLimitMiddleware(_echo_app)) as client:
        response = client.post("/auth/me", content=b"x" * MAX_NON_UPLOAD_REQUEST_BODY_BYTES)
        assert response.status_code == 200
        assert response.text == str(MAX_NON_UPLOAD_REQUEST_BODY_BYTES)

        response = client.post("/auth/me", content=b"x" * (MAX_NON_UPLOAD_REQUEST_BODY_BYTES + 1))
        assert response.status_code == 413


def test_raw_upload_route_is_exempt_only_for_a_valid_department_uuid() -> None:
    with TestClient(NonUploadBodyLimitMiddleware(_echo_app)) as client:
        valid = "00000000-0000-0000-0000-000000000001"
        response = client.post(f"/departments/{valid}/documents", content=b"x" * 65537)
        assert response.status_code == 200
        assert response.text == "65537"

        response = client.post("/departments/not-a-uuid/documents", content=b"x" * 65537)
        assert response.status_code == 413


def test_phase13_route_inventory_covers_every_department_path() -> None:
    routes_path = Path(__file__).parents[1] / "app" / "routes.py"
    source = routes_path.read_text(encoding="utf-8")
    paths = set(re.findall(r'"(/departments/[^"\n]+)"', source))
    assert paths
    families = {
        "department": "/departments/{department_id}",
        "membership": "/memberships",
        "documents": "/documents",
        "extraction": "/extractions",
        "indexing": "/indexings",
        "rag": "/rag/answers",
        "feedback": "/feedback",
        "evaluation": "/evaluations",
        "sft": "/sft",
        "training": "/training-jobs",
        "adapter": "/adapters",
    }
    for path in paths:
        assert any(fragment in path for fragment in families.values()), path
    assert not any("/search" in path or "query_vector" in path for path in paths)


def test_phase13_route_surface_has_no_public_content_or_selector_routes() -> None:
    routes_path = Path(__file__).parents[1] / "app" / "routes.py"
    source = routes_path.read_text(encoding="utf-8")
    forbidden = (
        r"/search",
        r"query[_-]vector",
        r"/adapter(s)?/(upload|download)",
        r"model[_-]selector",
        r"deployment[_-]selector",
        r"/maintenance",
    )
    for pattern in forbidden:
        assert re.search(pattern, source, re.IGNORECASE) is None, pattern


def test_body_limit_constant_is_closed_and_not_environment_controlled() -> None:
    assert MAX_NON_UPLOAD_REQUEST_BODY_BYTES == 65_536
    source = (Path(__file__).parents[1] / "app" / "body_limit.py").read_text(encoding="utf-8")
    assert "os.getenv" not in source


def test_fake_runtime_answer_sentinel_preserves_strict_contract(monkeypatch) -> None:
    sentinel = "PHASE13_GENERATED_ANSWER_SENTINEL"
    monkeypatch.setenv("DEPTSLM_RAG_FAKE_ANSWER_SENTINEL", sentinel)
    result = RuntimeModels(Path("/not-used"), "fake").generate(
        "question", [{"source_id": "S1", "text": "authorized evidence"}]
    )
    assert result["status"] == "answered"
    assert result["answer"].startswith(sentinel)
    assert result["citations"] == ["S1"]


def test_compose_services_have_explicit_least_privilege_networks() -> None:
    compose = (Path(__file__).parents[3] / "docker-compose.yml").read_text(encoding="utf-8")
    service_text = compose.split("\nnetworks:\n", 1)[0] + "\nnetworks:\n"
    expected = {
        "web": {"web-internal"},
        "api": {
            "web-internal",
            "postgres-internal",
            "qdrant-internal",
            "rag-base-internal",
            "adapter-prod-internal",
        },
        "postgres": {"postgres-internal"},
        "qdrant": {"qdrant-internal"},
        "rag-worker": {"postgres-internal"},
        "indexing-worker": {"postgres-internal", "qdrant-internal"},
        "evaluator-worker": {"postgres-internal", "qdrant-internal", "rag-base-internal"},
        "model-admin": {"model-egress"},
        "adapter-evaluator": {
            "postgres-internal",
            "qdrant-internal",
            "rag-base-internal",
            "adapter-eval-internal",
        },
        "adapter-eval-runtime": {"adapter-eval-internal"},
        "vector-admin": {"qdrant-internal"},
        "rag-runtime": {"rag-base-internal"},
        "adapter-runtime": {"adapter-prod-internal"},
        "training-worker": {"postgres-internal"},
        "training-job-worker": {"postgres-internal"},
        "adapter-registry-worker": {"postgres-internal"},
        "adapter-governance-worker": {"postgres-internal"},
        "adapter-maintenance": {"postgres-internal"},
        "training-execution-worker": {"postgres-internal"},
        "training-runtime": set(),
    }
    blocks = dict(
        re.findall(
            r"^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:|^networks:)",
            service_text,
            re.MULTILINE | re.DOTALL,
        )
    )
    assert set(blocks) == set(expected)
    for service, networks in expected.items():
        block = blocks[service]
        if service == "training-runtime":
            assert "network_mode: none" in block, service
            assert "\n    networks:\n" not in block, service
        else:
            assert "\n    networks:\n" in block, service
        actual = set(re.findall(r"^      - ([a-z0-9-]+)$", block, re.MULTILINE))
        assert actual == networks, (service, actual)
    assert "  default:" not in compose
    assert "  rag-internal:" not in compose


def test_compose_capability_matrix_has_no_implicit_or_secret_capabilities() -> None:
    compose = (Path(__file__).parents[3] / "docker-compose.yml").read_text(encoding="utf-8")
    service_text = compose.split("\nnetworks:\n", 1)[0] + "\nnetworks:\n"
    blocks = dict(
        re.findall(
            r"^  ([a-z0-9-]+):\n(.*?)(?=^  [a-z0-9-]+:|^networks:)",
            service_text,
            re.MULTILINE | re.DOTALL,
        )
    )
    assert set(blocks) == {
        "web",
        "api",
        "postgres",
        "qdrant",
        "rag-worker",
        "indexing-worker",
        "evaluator-worker",
        "model-admin",
        "adapter-evaluator",
        "adapter-eval-runtime",
        "vector-admin",
        "rag-runtime",
        "adapter-runtime",
        "training-worker",
        "training-job-worker",
        "adapter-registry-worker",
        "adapter-governance-worker",
        "adapter-maintenance",
        "training-execution-worker",
        "training-runtime",
    }
    for service, block in blocks.items():
        if service == "training-runtime":
            assert "network_mode: none" in block, service
            assert "\n    networks:\n" not in block, service
        else:
            assert "network_mode:" not in block, service
            assert "\n    networks:\n" in block, service
        assert "/var/run/docker.sock" not in block, service
        if "ports:" in block:
            assert '"127.0.0.1:' in block, service

    expected_capabilities = {
        "web": ("read_only: true", "cap_drop:\n      - ALL", "no-new-privileges:true"),
        "api": ("read_only: true", "cap_drop:\n      - ALL", "no-new-privileges:true"),
        "rag-runtime": ("rag-base-internal", "model_cache"),
        "adapter-runtime": ("adapter-prod-internal", "adapters/registry", "model_cache"),
        "adapter-eval-runtime": (
            "adapter-eval-internal",
            "adapters/registry",
            "model_cache",
        ),
        "model-admin": ("model-egress", "model_cache"),
        "training-execution-worker": (
            'profiles: ["training"]',
            "training_runs",
            "training-runtime-ipc",
            "DEPTSLM_TRAINING_RUNTIME_TOKEN",
        ),
        "training-runtime": (
            'profiles: ["training"]',
            "network_mode: none",
            "model_cache/qwen3-0.6b-c1899de289a04d12100db370d81485cdf75e47ca",
            "training-runtime-ipc",
            "capabilities: [gpu]",
        ),
    }
    for service, required in expected_capabilities.items():
        block = blocks[service]
        for capability in required:
            assert capability in block, (service, capability)

    forbidden = {
        "web": ("DATABASE_URL", "DEPTSLM_QDRANT", "DEPTSLM_RAG_RUNTIME_TOKEN"),
        "rag-runtime": ("DATABASE_URL", "DEPTSLM_QDRANT", "DEPTSLM_ADAPTER_RUNTIME"),
        "adapter-runtime": (
            "DATABASE_URL",
            "DEPTSLM_QDRANT",
            "DEPTSLM_RAG_RUNTIME_TOKEN",
            "DEPTSLM_ADAPTER_EVAL_RUNTIME_TOKEN",
        ),
        "adapter-eval-runtime": (
            "DATABASE_URL",
            "DEPTSLM_QDRANT",
            "DEPTSLM_ADAPTER_RUNTIME_TOKEN",
        ),
        "model-admin": ("DATABASE_URL", "DEPTSLM_QDRANT", "DEPTSLM_RAG_RUNTIME_TOKEN"),
        "training-runtime": (
            "DATABASE_URL",
            "QDRANT",
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
        ),
    }
    for service, forbidden_values in forbidden.items():
        block = blocks[service]
        for value in forbidden_values:
            assert value not in block, (service, value)

    networks = compose.split("\nnetworks:\n", 1)[1]
    for name in (
        "web-internal",
        "postgres-internal",
        "qdrant-internal",
        "rag-base-internal",
        "adapter-prod-internal",
        "adapter-eval-internal",
    ):
        assert re.search(rf"^  {re.escape(name)}:\n    internal: true$", networks, re.MULTILINE)
    assert re.search(r"^  model-egress:\n    internal: false$", networks, re.MULTILINE)


def test_compose_wrapper_config_does_not_display_secret_sentinels() -> None:
    repository_root = Path(__file__).parents[3]
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        data_dir = root / "runtime"
        required = (
            "uploads",
            "extracted_text",
            "vector_snapshots",
            "model_cache",
            "eval_results",
            "logs",
            "exports",
            "training_datasets",
            "training_datasets/jobs",
            "adapters",
            "adapters/imports",
            "adapters/registry",
            "adapters/.staging/imports",
            "adapters/.staging/registry",
            "adapters/.purge-deleting/source_stage",
            "adapters/.purge-deleting/source_final",
            "adapters/.purge-deleting/registry_stage",
            "adapters/.purge-deleting/registry_final",
            "service_state/postgres",
            "service_state/qdrant",
        )
        for relative in required:
            (data_dir / relative).mkdir(parents=True, exist_ok=True)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *'config --quiet') exit 0 ;;\n"
            "  *'config --no-interpolate') printf 'safe-config\\n'; exit 0 ;;\n"
            "esac\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_docker.chmod(stat.S_IRWXU)
        sentinels = (
            "phase13-auth-sentinel",
            "phase13-postgres-sentinel",
            "phase13-qdrant-sentinel",
            "phase13-rag-sentinel",
            "phase13-adapter-sentinel",
            "phase13-eval-sentinel",
            "phase13-hf-sentinel",
            "phase14-training-runtime-sentinel",
            "phase13-web-dev-bearer-sentinel",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "DEPTSLM_DATA_DIR": str(data_dir),
                "DEPTSLM_AUTH_SECRET": sentinels[0],
                "DEPTSLM_POSTGRES_PASSWORD": sentinels[1],
                "DEPTSLM_QDRANT_API_KEY": sentinels[2],
                "DEPTSLM_RAG_RUNTIME_TOKEN": sentinels[3],
                "DEPTSLM_ADAPTER_RUNTIME_TOKEN": sentinels[4],
                "DEPTSLM_ADAPTER_EVAL_RUNTIME_TOKEN": sentinels[5],
                "HF_TOKEN": sentinels[6],
                "DEPTSLM_TRAINING_RUNTIME_TOKEN": sentinels[7],
                "DEPTSLM_WEB_DEV_BEARER_TOKEN": sentinels[8],
            }
        )
        result = subprocess.run(
            [str(repository_root / "scripts" / "compose.sh"), "config"],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        output = result.stdout + result.stderr
        assert "safe-config" in result.stdout
        assert all(sentinel not in output for sentinel in sentinels)

        refused = subprocess.run(
            [str(repository_root / "scripts" / "compose.sh"), "config", "--environment"],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert refused.returncode == 2
        assert "refused" in refused.stderr
        assert all(sentinel not in refused.stdout + refused.stderr for sentinel in sentinels)


def test_compose_wrapper_has_no_secret_display_mode_and_rejects_environment_dump() -> None:
    wrapper = (Path(__file__).parents[3] / "scripts" / "compose.sh").read_text(encoding="utf-8")
    assert "config --no-interpolate" in wrapper
    assert "config --environment" in wrapper
    assert "show-secrets" not in wrapper
    assert "chmod 600 .env" in wrapper
    assert "DEPTSLM_COMPOSE_WRAPPER=1" in wrapper
