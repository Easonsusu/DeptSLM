"""FastAPI and OpenAPI boundary tests for Phase 12.1D metadata reads."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import app.routes as routes
from app.adapter_registry_read_services import AdapterMetadataProjection
from app.auth import AuthenticatedPrincipal
from app.authorization import (
    DepartmentRequestScope,
    DepartmentScope,
    require_authenticated_principal,
    require_path_department_selector,
)
from app.database import get_db_session
from app.main import app


def _client(monkeypatch, tmp_path: Path, *, projection: AdapterMetadataProjection | None = None):
    (tmp_path / "uploads").mkdir()
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://deptslm@127.0.0.1:1/deptslm_test")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEPTSLM_AUTH_MODE", "disabled")
    department_id = uuid4()
    principal = AuthenticatedPrincipal("route-subject", "https://route.invalid")
    scope = DepartmentRequestScope(DepartmentScope(department_id))

    def fake_session():
        yield object()

    app.dependency_overrides[get_db_session] = fake_session
    app.dependency_overrides[require_authenticated_principal] = lambda: principal
    app.dependency_overrides[require_path_department_selector] = lambda: scope
    monkeypatch.setattr(routes, "list_adapters", lambda *args, **kwargs: ())
    if projection is not None:
        monkeypatch.setattr(routes, "read_adapter", lambda *args, **kwargs: projection)
    client = TestClient(app)
    return client, department_id


def test_openapi_exposes_only_two_get_adapter_routes() -> None:
    paths = app.openapi()["paths"]
    adapter_paths = {
        "/departments/{department_id}/adapters",
        "/departments/{department_id}/adapters/{adapter_id}",
    }
    assert set(paths["/departments/{department_id}/adapters"]) == {"get"}
    assert set(paths["/departments/{department_id}/adapters/{adapter_id}"]) == {"get"}
    assert all("requestBody" not in paths[path]["get"] for path in adapter_paths)
    # Phase 12.1D still exposes no artifact route. Phase 12.2 evaluation and
    # Phase 12.3 governance mutations are explicit, separate metadata routes
    # and must not be mistaken for adapter artifact lifecycle mutation.
    governance_paths = {
        "/review",
        "/promote",
        "/rollback",
        "/rollback-retention/release",
    }
    assert not any(
        path.startswith("/departments/{department_id}/adapters")
        and "/evaluations" not in path
        and not any(path.endswith(suffix) for suffix in governance_paths)
        and method in {"post", "put", "patch", "delete"}
        for path, methods in paths.items()
        for method in methods
    )
    assert not any("manifest" in path or "config" in path for path in paths if "adapter" in path)


def test_api_has_no_adapter_storage_mount_or_artifact_route() -> None:
    compose = Path(__file__).parents[3].joinpath("docker-compose.yml").read_text()
    api_block = compose.split("  api:\n", 1)[1].split("  postgres:\n", 1)[0]
    assert "/adapters" not in api_block
    source = Path(routes.__file__).read_text()
    assert "FileResponse" not in source
    assert "StreamingResponse" not in source
    assert (
        "upload"
        not in source.split('"/departments/{department_id}/adapters', 1)[1]
        .split("@router.post", 1)[0]
        .lower()
    )


def test_list_route_is_json_and_has_closed_empty_response(monkeypatch, tmp_path: Path) -> None:
    client, department_id = _client(monkeypatch, tmp_path)
    try:
        response = client.get(f"/departments/{department_id}/adapters")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"items": [], "limit": 25, "offset": 0}
    finally:
        app.dependency_overrides.clear()


def test_route_rejects_invalid_uuid_and_pagination(monkeypatch, tmp_path: Path) -> None:
    client, department_id = _client(monkeypatch, tmp_path)
    try:
        assert client.get(f"/departments/{department_id}/adapters/not-a-uuid").status_code == 422
        assert client.get(f"/departments/{department_id}/adapters?limit=0").status_code == 422
        assert client.get(f"/departments/{department_id}/adapters?limit=101").status_code == 422
        assert client.get(f"/departments/{department_id}/adapters?offset=-1").status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_detail_route_serializes_only_the_metadata_projection(monkeypatch, tmp_path: Path) -> None:
    from test_adapter_registry_read_services import _authority_rows, _nested_keys

    from app.adapter_registry_read_services import _project

    adapter, source, dependency = _authority_rows()
    projection = _project(adapter, source, dependency)
    client, _ = _client(monkeypatch, tmp_path, projection=projection)
    try:
        response = client.get(f"/departments/{projection.department_id}/adapters/{projection.id}")
        assert response.status_code == 200
        assert set(response.json()) == set(projection.public_data())
        assert _nested_keys(response.json()).isdisjoint(
            {"path", "storage_path", "sha256", "byte_size", "worker_id", "claim_token"}
        )
    finally:
        app.dependency_overrides.clear()
