"""Phase 13 executable transport, tenant, persistence, and log proof."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
import test_phase7_postgres as phase7_tests
from alembic.config import Config
from deptslm_worker.qdrant_adapter import DepartmentQdrant, VectorPoint
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from app.authorization import DepartmentScope
from app.database import create_database_engine
from app.main import app
from app.models import (
    Document,
    PersistentAuditEvent,
    RagAnswerRun,
    RagAnswerRuntimeSnapshot,
)
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
)

pytestmark = pytest.mark.postgres

MAX_NON_UPLOAD = 65_536
SECRET = phase7_tests.SECRET
ISSUER = phase7_tests.ISSUER
AUDIENCE = phase7_tests.AUDIENCE


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL")
    if value:
        return value
    if os.getenv("DEPTSLM_REQUIRE_POSTGRES_TESTS") == "1":
        pytest.fail("DATABASE_TEST_URL is required; PostgreSQL tests may not be skipped")
    pytest.skip("PostgreSQL integration database is unavailable")


def _qdrant_configuration() -> tuple[str, str]:
    url = os.getenv("DEPTSLM_TEST_QDRANT_URL")
    key = os.getenv("DEPTSLM_TEST_QDRANT_API_KEY")
    if url and key and os.getenv("DEPTSLM_TEST_QDRANT_ISOLATED") == "1":
        return url, key
    if os.getenv("DEPTSLM_REQUIRE_QDRANT_TESTS") == "1":
        pytest.fail("isolated Qdrant URL, key, and explicit isolation marker are required")
    pytest.skip("isolated Qdrant integration service is unavailable")


@pytest.fixture(scope="module")
def engine():
    value = create_database_engine(_database_url())
    command.upgrade(Config("alembic.ini"), "head")
    yield value
    value.dispose()


def _seed_unique(db: Session, tmp_path: Path):
    # Keep the returned seed objects usable after the helper's commit.  The
    # integration tests intentionally close this session before exercising the
    # real HTTP application, so expired ORM attributes would otherwise try to
    # refresh through a detached PostgreSQL session.
    db.expire_on_commit = False
    original = phase7_tests._identity

    def identity(session, department, role, subject):
        return original(session, department, role, f"{subject}-{uuid4().hex}")

    phase7_tests._identity = identity
    try:
        return phase7_tests._seed(db, tmp_path)
    finally:
        phase7_tests._identity = original


def _token(subject: str) -> str:
    return jwt.encode(
        {
            "sub": subject,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=10),
        },
        SECRET,
        algorithm="HS256",
    )


def _headers(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(subject)}"}


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, maximum: int | None = None):
    (tmp_path / "uploads").mkdir(exist_ok=True)
    (tmp_path / "extracted_text").mkdir(exist_ok=True)
    (tmp_path / "model_cache").mkdir(exist_ok=True)
    values = {
        "DATABASE_URL": _database_url(),
        "DEPTSLM_DATA_DIR": str(tmp_path),
        "ENVIRONMENT": "test",
        "DEPTSLM_AUTH_MODE": "hs256",
        "DEPTSLM_AUTH_ISSUER": ISSUER,
        "DEPTSLM_AUTH_AUDIENCE": AUDIENCE,
        "DEPTSLM_AUTH_SECRET": SECRET,
        "DEPTSLM_QDRANT_URL": os.getenv("DEPTSLM_TEST_QDRANT_URL", "http://localhost:6333"),
        "DEPTSLM_QDRANT_API_KEY": os.getenv(
            "DEPTSLM_TEST_QDRANT_API_KEY", "phase13-test-qdrant-key-0123456789"
        ),
        "DEPTSLM_QDRANT_COLLECTION": "deptslm_chunks_qwen3_0_6b_1024_v1",
        "DEPTSLM_RAG_RUNTIME_URL": "http://localhost:8010",
        "DEPTSLM_RAG_RUNTIME_TOKEN": "phase13-test-runtime-token-0123456789-abcdef",
        "DEPTSLM_DEPARTMENT_DOCUMENT_QUOTA_BYTES": "300000",
        # Keep the test configuration valid while exercising the independent
        # non-upload middleware limit.  Upload tests override this explicitly.
        "DEPTSLM_DOCUMENT_MAX_BYTES": str(MAX_NON_UPLOAD),
    }
    if maximum is not None:
        values["DEPTSLM_DOCUMENT_MAX_BYTES"] = str(maximum)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return TestClient(app)


def _counts(engine, department_id: UUID) -> dict[str, int]:
    with Session(engine) as session:
        return {
            "runs": session.scalar(
                select(func.count())
                .select_from(RagAnswerRun)
                .where(RagAnswerRun.department_id == department_id)
            ),
            "snapshots": session.scalar(
                select(func.count())
                .select_from(RagAnswerRuntimeSnapshot)
                .where(RagAnswerRuntimeSnapshot.department_id == department_id)
            ),
            "start_audits": session.scalar(
                select(func.count())
                .select_from(PersistentAuditEvent)
                .where(
                    PersistentAuditEvent.department_id == department_id,
                    PersistentAuditEvent.action == "rag.answer.start",
                    PersistentAuditEvent.result == "allowed",
                )
            ),
            "all_audits": session.scalar(
                select(func.count())
                .select_from(PersistentAuditEvent)
                .where(PersistentAuditEvent.department_id == department_id)
            ),
            "documents": session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.department_id == department_id)
            ),
        }


class _CallRecorder:
    def __init__(self) -> None:
        self.query_embedding_calls = 0
        self.generate_calls = 0
        self.qdrant_calls = 0
        self.adapter_calls = 0

    def query_embedding(self, _question):
        self.query_embedding_calls += 1
        vector = [0.0] * EMBEDDING_DIMENSION
        vector[0] = 1.0
        return vector

    def generate(self, _question, _evidence):
        self.generate_calls += 1
        return {"status": "answered", "answer": "safe [S1]", "citations": ["S1"]}


class _ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, chunk_size: int = 4096) -> None:
        self.body = body
        self.chunk_size = chunk_size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.body), self.chunk_size):
            yield self.body[offset : offset + self.chunk_size]
            await asyncio.sleep(0)


def _oversized_json() -> bytes:
    return b'{"question":"' + (b"x" * (MAX_NON_UPLOAD + 10_000)) + b'"}'


def _exact_limit_json() -> bytes:
    prefix = b'{"question":"'
    suffix = b'"}'
    return prefix + (b"x" * (MAX_NON_UPLOAD - len(prefix) - len(suffix))) + suffix


def test_real_declared_oversized_rag_has_no_application_side_effects(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with Session(engine) as db:
        identities, department, *_ = _seed_unique(db, tmp_path)
    recorder = _CallRecorder()
    before = _counts(engine, department.id)
    with _client(monkeypatch, tmp_path) as client:
        app.state.rag_runtime_client = recorder
        app.state.rag_qdrant = recorder
        response = client.post(
            f"/departments/{department.id}/rag/answers",
            headers={
                **_headers(identities["system_admin"].subject),
                "Content-Length": str(MAX_NON_UPLOAD + 1),
            },
            content=_oversized_json(),
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert _counts(engine, department.id) == before
    assert recorder.query_embedding_calls == recorder.generate_calls == recorder.qdrant_calls == 0
    assert recorder.adapter_calls == 0


def test_real_streamed_oversized_rag_has_no_application_side_effects(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with Session(engine) as db:
        identities, department, *_ = _seed_unique(db, tmp_path)
    recorder = _CallRecorder()
    before = _counts(engine, department.id)
    with _client(monkeypatch, tmp_path):
        app.state.rag_runtime_client = recorder
        app.state.rag_qdrant = recorder

        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.post(
                    f"/departments/{department.id}/rag/answers",
                    headers=_headers(identities["system_admin"].subject),
                    content=_ChunkedBody(_oversized_json()),
                )

        response = asyncio.run(send())
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert _counts(engine, department.id) == before
    assert recorder.query_embedding_calls == recorder.generate_calls == recorder.qdrant_calls == 0
    assert recorder.adapter_calls == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/departments/{department_id}"),
        ("POST", "/departments/{department_id}/memberships"),
        (
            "POST",
            "/departments/{department_id}/adapter-deployment/operations/{operation_id}/cancel",
        ),
    ],
)
def test_real_oversized_control_plane_body_is_rejected_before_mutation(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str, path: str
) -> None:
    with Session(engine) as db:
        identities, department, *_ = _seed_unique(db, tmp_path)
    rendered = path.format(department_id=department.id, operation_id=uuid4())
    before = _counts(engine, department.id)
    with _client(monkeypatch, tmp_path) as client:
        response = client.request(
            method,
            rendered,
            headers={
                **_headers(identities["system_admin"].subject),
                "Content-Length": str(MAX_NON_UPLOAD + 1),
                "Content-Type": "application/json",
            },
            content=_oversized_json(),
        )
    assert response.status_code == 413
    assert _counts(engine, department.id) == before


def test_exact_limit_and_malformed_under_limit_reach_application_contract(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with Session(engine) as db:
        identities, department, *_ = _seed_unique(db, tmp_path)
    with _client(monkeypatch, tmp_path) as client:
        headers = _headers(identities["system_admin"].subject)
        exact = client.post(
            f"/departments/{department.id}/rag/answers",
            headers=headers,
            content=_exact_limit_json(),
        )
        malformed = client.post(
            f"/departments/{department.id}/rag/answers", headers=headers, content=b'{"question":'
        )
    assert exact.status_code == 422
    assert exact.json() != {"detail": "Request body too large"}
    assert malformed.status_code == 422
    assert malformed.json() != {"detail": "Request body too large"}


def test_real_streaming_upload_exempts_only_the_global_limit_and_commits(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = (b"phase13-upload-evidence\n" * 4000)[:70_000]
    assert len(body) > MAX_NON_UPLOAD
    with Session(engine) as db:
        identities, department, *_ = _seed_unique(db, tmp_path)
    with _client(monkeypatch, tmp_path, maximum=100_000) as client:
        response = client.post(
            f"/departments/{department.id}/documents",
            headers={
                **_headers(identities["department_admin"].subject),
                "Content-Disposition": 'attachment; filename="phase13.txt"',
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body)),
            },
            content=body,
        )
    assert response.status_code == 201, response.text
    document_id = UUID(response.json()["id"])
    with Session(engine) as db:
        document = db.get(Document, document_id)
        upload_audit = db.scalar(
            select(func.count())
            .select_from(PersistentAuditEvent)
            .where(
                PersistentAuditEvent.department_id == department.id,
                PersistentAuditEvent.action == "document.upload",
                PersistentAuditEvent.resource_id == str(document_id),
                PersistentAuditEvent.result == "allowed",
            )
        )
    assert document is not None and document.byte_size == len(body)
    assert upload_audit == 1
    final_source = tmp_path / "uploads" / str(department.id) / str(document_id) / "source"
    assert final_source.read_bytes() == body
    assert not any((tmp_path / "uploads" / str(department.id) / ".staging").iterdir())


def test_raw_upload_above_its_own_limit_keeps_the_phase4_413_contract(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = b"x" * (MAX_NON_UPLOAD + 1)
    with Session(engine) as db:
        identities, department, *_ = _seed_unique(db, tmp_path)
    before = _counts(engine, department.id)
    with _client(monkeypatch, tmp_path, maximum=MAX_NON_UPLOAD) as client:
        response = client.post(
            f"/departments/{department.id}/documents",
            headers={
                **_headers(identities["department_admin"].subject),
                "Content-Disposition": 'attachment; filename="too-large.txt"',
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": str(len(body)),
            },
            content=body,
        )
    assert response.status_code == 413
    assert response.json() != {"detail": "Request body too large"}
    assert _counts(engine, department.id) == before


def test_current_surface_matrix_denies_department_a_system_admin_on_department_b(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with Session(engine) as db:
        identities_a, department_a, *_ = _seed_unique(db, tmp_path)
        _identities_b, department_b, document_b, extraction_b, *_ = _seed_unique(db, tmp_path)
    routes = {
        "department": f"/departments/{department_b.id}",
        "membership": f"/departments/{department_b.id}/memberships",
        "documents": f"/departments/{department_b.id}/documents",
        "extraction": f"/departments/{department_b.id}/documents/{document_b.id}/extractions",
        "indexing": (
            f"/departments/{department_b.id}/documents/{document_b.id}/"
            f"extractions/{extraction_b.id}/indexings"
        ),
        "rag": f"/departments/{department_b.id}/rag/answers",
        "feedback": f"/departments/{department_b.id}/rag/feedback",
        "evaluation": f"/departments/{department_b.id}/evaluation-suites",
        "sft": f"/departments/{department_b.id}/sft/sources",
        "training": f"/departments/{department_b.id}/training/jobs",
        "adapter registry": f"/departments/{department_b.id}/adapters",
        "adapter evaluation": f"/departments/{department_b.id}/adapters/{uuid4()}/evaluations",
        "adapter governance": f"/departments/{department_b.id}/adapters/{uuid4()}/reviews",
        "deployment": f"/departments/{department_b.id}/adapter-deployment",
        "deployment operation": f"/departments/{department_b.id}/adapter-deployment/operations",
        "deployment event": f"/departments/{department_b.id}/adapter-deployment/events",
    }
    before = _counts(engine, department_b.id)
    with _client(monkeypatch, tmp_path) as client:
        for family, path in routes.items():
            if family == "rag":
                response = client.post(
                    path,
                    headers={
                        **_headers(identities_a["system_admin"].subject),
                        "Content-Type": "application/json",
                    },
                    json={"question": "cross department authorization"},
                )
            else:
                response = client.get(path, headers=_headers(identities_a["system_admin"].subject))
            assert response.status_code == 403, (family, response.status_code, response.text)
            assert response.headers.get("www-authenticate") is None
    assert _counts(engine, department_b.id) == before
    assert department_a.id != department_b.id


def test_route_inventory_maps_every_department_scoped_path_to_a_reviewed_family() -> None:
    routes_path = Path(__file__).parents[1] / "app" / "routes.py"
    source = routes_path.read_text(encoding="utf-8")
    paths = sorted(set(re.findall(r'"(/departments/[^"\n]+)"', source)))
    assert paths
    families = {
        "membership": ("/memberships",),
        "documents": ("/documents",),
        "extraction": ("/extractions",),
        "indexing": ("/indexings",),
        "rag": ("/rag/answers",),
        "feedback": ("/feedback",),
        "evaluation": ("/evaluation-suites", "evaluation-runs"),
        "sft": ("/sft/",),
        "training": ("/training/",),
        "adapter": ("/adapters", "adapter-deployment"),
    }
    for path in paths:
        if path == "/departments/{department_id}":
            continue
        matches = [
            (len(fragment), family)
            for family, fragments in families.items()
            for fragment in fragments
            if fragment in path
        ]
        assert matches, path
        longest = max(length for length, _family in matches)
        selected = {family for length, family in matches if length == longest}
        assert len(selected) == 1, (path, matches)
    assert not any("/search" in path or "query_vector" in path for path in paths)


@pytest.mark.qdrant
def test_rag_transient_sentinels_stay_out_of_postgres_external_forbidden_storage_and_logs(
    engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    question = f"PHASE13_QUESTION_SENTINEL_{uuid4().hex}"
    answer = f"PHASE13_GENERATED_ANSWER_SENTINEL_{uuid4().hex}"
    evidence = f"PHASE13_EVIDENCE_SENTINEL_{uuid4().hex}"
    source_text = f"Authorized evidence: {evidence}"
    with Session(engine) as db:
        identities, department, document, extraction, chunk, indexing = _seed_unique(db, tmp_path)
        source_bytes = source_text.encode()
        document.original_filename = "sentinel-source.txt"
        document.sha256 = hashlib.sha256(source_bytes).hexdigest()
        document.byte_size = len(source_bytes)
        extraction.source_sha256 = document.sha256
        extraction.normalized_sha256 = document.sha256
        extraction.source_byte_size = document.byte_size
        extraction.normalized_byte_size = document.byte_size
        chunk.content_sha256 = document.sha256
        chunk.byte_size = document.byte_size
        chunk.char_start = 0
        chunk.char_end = len(source_text)
        artifact_directory = (
            tmp_path / "extracted_text" / str(department.id) / str(document.id) / str(extraction.id)
        )
        shutil.rmtree(artifact_directory)
        extraction.output_byte_size = phase7_tests._write_artifact(
            tmp_path, department, document, extraction, chunk, source_bytes
        )
        db.commit()

    url, key = _qdrant_configuration()
    qdrant = DepartmentQdrant(url, key, 10)
    scope = DepartmentScope(department.id)
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[0] = 1.0
    point = VectorPoint(
        chunk_id=chunk.id,
        document_id=document.id,
        extraction_id=extraction.id,
        indexing_id=indexing.id,
        vector_attempt_id=indexing.vector_attempt_id,
        chunk_ordinal=chunk.ordinal,
        provenance_kind=chunk.provenance_kind,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
        vector=tuple(vector),
    )
    try:
        qdrant.bootstrap_collection()
        qdrant.upsert_staging(scope, (point,))
        qdrant.activate_attempt(scope, indexing.id, indexing.vector_attempt_id)
        caller_token = _token(identities["student"].subject)
        caller_headers = {"Authorization": f"Bearer {caller_token}"}

        class SentinelRuntime:
            def query_embedding(self, _question):
                return vector

            def generate(self, _question, _evidence):
                return {"status": "answered", "answer": f"{answer} [S1]", "citations": ["S1"]}

        caplog.set_level(logging.INFO)
        with _client(monkeypatch, tmp_path) as client:
            app.state.rag_runtime_client = SentinelRuntime()
            app.state.rag_qdrant = qdrant
            response = client.post(
                f"/departments/{department.id}/rag/answers",
                headers=caller_headers,
                json={"question": question},
            )
        assert response.status_code == 200, response.text
        assert response.json()["answer"].startswith(answer)

        with engine.connect() as connection:
            for table in inspect(engine).get_table_names():
                for column in inspect(engine).get_columns(table):
                    try:
                        python_type = column["type"].python_type
                    except (AttributeError, NotImplementedError):
                        continue
                    if python_type not in {str, dict, list}:
                        continue
                    query = (
                        f'SELECT count(*) FROM "{table}" '
                        f'WHERE CAST("{column["name"]}" AS text) LIKE :needle'
                    )
                    assert (
                        connection.execute(text(query), {"needle": f"%{question}%"}).scalar_one()
                        == 0
                    )
                    assert (
                        connection.execute(text(query), {"needle": f"%{answer}%"}).scalar_one() == 0
                    )
                    assert (
                        connection.execute(text(query), {"needle": f"%{evidence}%"}).scalar_one()
                        == 0
                    )
                    assert (
                        connection.execute(
                            text(query), {"needle": f"%{caller_token}%"}
                        ).scalar_one()
                        == 0
                    )
        forbidden_roots = (
            "vector_snapshots",
            "training_datasets",
            "adapters",
            "model_cache",
            "eval_results",
            "logs",
            "exports",
        )
        for area in forbidden_roots:
            root = tmp_path / area
            for candidate in root.rglob("*") if root.exists() else ():
                if candidate.is_file():
                    value = candidate.read_text(errors="ignore")
                    assert all(
                        marker not in value for marker in (question, answer, evidence, caller_token)
                    )
        assert all(
            marker not in caplog.text for marker in (question, answer, evidence, caller_token)
        )
    finally:
        try:
            qdrant.delete_attempt(scope, indexing.id, indexing.vector_attempt_id)
        finally:
            qdrant.close()
