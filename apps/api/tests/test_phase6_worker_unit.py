"""Unit coverage for Phase 6 artifact, embedding, model, and scope boundaries."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from deptslm_worker.artifact_reader import (
    ArtifactError,
    ArtifactExpectation,
    Phase5ArtifactReader,
)
from deptslm_worker.embedding import EmbeddingError, EmbeddingProcess, validate_vector
from deptslm_worker.index_settings import IndexConfigurationError, IndexSettings
from deptslm_worker.model_store import (
    MANIFEST_NAME,
    ModelStoreError,
    build_manifest,
    model_directory,
    validate_model_store,
)
from deptslm_worker.qdrant_adapter import (
    DepartmentQdrant,
    QdrantBoundaryError,
    VectorPoint,
)

from app.authorization import DepartmentScope
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL_REVISION,
    QDRANT_COLLECTION,
)

pytestmark = pytest.mark.unit


def _index_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "extracted_text").mkdir(parents=True)
    (root / "model_cache").mkdir()
    return root


def _index_environment(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    values = {
        "DEPTSLM_DATA_DIR": str(root),
        "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1/test",
        "DEPTSLM_QDRANT_URL": "http://127.0.0.1:6333",
        "DEPTSLM_QDRANT_API_KEY": "phase6-unit-key-0123456789",
        "DEPTSLM_QDRANT_COLLECTION": QDRANT_COLLECTION,
        "DEPTSLM_EMBEDDING_MODEL_REVISION": EMBEDDING_MODEL_REVISION,
        "DEPTSLM_EMBEDDING_PROVIDER": "fake",
        "ENVIRONMENT": "test",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_index_settings_accept_exact_test_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _index_root(tmp_path)
    _index_environment(monkeypatch, root)
    settings = IndexSettings.from_environment()
    assert settings.embedding_provider == "fake"
    assert settings.qdrant_collection == QDRANT_COLLECTION
    assert settings.embedding_model_revision == EMBEDDING_MODEL_REVISION


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ENVIRONMENT", "developmnt"),
        ("DEPTSLM_QDRANT_URL", "http://user:password@127.0.0.1:6333"),
        ("DEPTSLM_QDRANT_URL", "http://qdrant.example.invalid:6333"),
        ("DEPTSLM_QDRANT_URL", "https://example.invalid/?token=x"),
        ("DEPTSLM_QDRANT_API_KEY", "changeme"),
        ("DEPTSLM_QDRANT_COLLECTION", "client_collection"),
        ("DEPTSLM_EMBEDDING_MODEL_REVISION", "main"),
        ("DEPTSLM_EMBEDDING_BATCH_SIZE", "0"),
        ("DEPTSLM_EMBEDDING_MAX_BATCH_CHARS", "1_000"),
        ("DEPTSLM_VECTOR_INDEX_LEASE_SECONDS", "120"),
    ],
)
def test_index_settings_reject_unsafe_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    root = _index_root(tmp_path)
    _index_environment(monkeypatch, root)
    monkeypatch.setenv(name, value)
    with pytest.raises(IndexConfigurationError):
        IndexSettings.from_environment()


def test_fake_embedding_provider_is_test_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _index_root(tmp_path)
    _index_environment(monkeypatch, root)
    monkeypatch.setenv("ENVIRONMENT", "development")
    with pytest.raises(IndexConfigurationError, match="Fake embeddings"):
        IndexSettings.from_environment()


def test_vector_validation_accepts_only_normalized_finite_dimension() -> None:
    valid = [0.0] * EMBEDDING_DIMENSION
    valid[7] = 1.0
    assert validate_vector(valid)[7] == 1.0
    invalid = (
        [0.0] * EMBEDDING_DIMENSION,
        [1.0] * (EMBEDDING_DIMENSION - 1),
        [math.nan] + valid[1:],
        [math.inf] + valid[1:],
        [11.0] + valid[1:],
        [True] + valid[1:],
    )
    for vector in invalid:
        with pytest.raises(EmbeddingError, match="invalid_embedding"):
            validate_vector(vector)


def test_fake_embedding_subprocess_preserves_order_and_has_no_secret_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "must-not-enter-child")
    monkeypatch.setenv("DEPTSLM_QDRANT_API_KEY", "must-not-enter-child")
    with EmbeddingProcess(
        tmp_path,
        provider="fake",
        environment="test",
        timeout_seconds=5,
        heartbeat=lambda: True,
        should_stop=lambda: False,
    ) as process:
        vectors = process.embed(["first", "second", "first"])
    assert len(vectors) == 3
    assert vectors[0] == vectors[2]
    assert vectors[0] != vectors[1]
    assert all(len(vector) == EMBEDDING_DIMENSION for vector in vectors)


def _artifact(root: Path):
    department_id, document_id, extraction_id = uuid4(), uuid4(), uuid4()
    final = root / "extracted_text" / str(department_id) / str(document_id) / str(extraction_id)
    final.mkdir(parents=True)
    text = "alpha beta"
    normalized = (text + "\n").encode()
    chunk = {
        "ordinal": 0,
        "text": text,
        "char_start": 0,
        "char_end": len(text),
        "byte_size": len(text.encode()),
        "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "provenance_kind": "line",
        "page_start": None,
        "page_end": None,
        "line_start": 1,
        "line_end": 1,
    }
    chunks = (json.dumps(chunk, sort_keys=True, separators=(",", ":")) + "\n").encode()
    manifest = {
        "chunk_count": 1,
        "chunking_version": "phase5-character-chunker-v1",
        "chunks_sha256": hashlib.sha256(chunks).hexdigest(),
        "department_id": str(department_id),
        "document_id": str(document_id),
        "extraction_id": str(extraction_id),
        "normalization_version": "phase5-normalization-v1",
        "normalized_byte_size": len(normalized),
        "normalized_sha256": hashlib.sha256(normalized).hexdigest(),
        "parser_name": "python-utf8",
        "parser_version": "3.12",
        "pipeline_version": "phase5-extraction-v1",
        "source_byte_size": 5,
        "source_sha256": "1" * 64,
    }
    (final / "normalized.txt").write_bytes(normalized)
    (final / "chunks.jsonl").write_bytes(chunks)
    (final / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    expectation = ArtifactExpectation(
        department_id,
        document_id,
        extraction_id,
        1,
        manifest["normalized_sha256"],
        len(normalized),
        sum(path.stat().st_size for path in final.iterdir()),
    )
    return DepartmentScope(department_id), expectation, final


def test_artifact_reader_validates_exact_incremental_phase5_output(tmp_path: Path) -> None:
    root = _index_root(tmp_path)
    scope, expectation, _final = _artifact(root)
    with Phase5ArtifactReader(root, scope, expectation) as reader:
        chunks = tuple(reader.iter_chunks())
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0 and chunks[0].text == "alpha beta"


@pytest.mark.parametrize("mutation", ["unknown", "symlink", "bad-json", "extra-chunk"])
def test_artifact_reader_rejects_unsafe_or_mismatched_output(tmp_path: Path, mutation: str) -> None:
    root = _index_root(tmp_path)
    scope, expectation, final = _artifact(root)
    if mutation == "unknown":
        (final / "unexpected.txt").write_text("x")
    elif mutation == "symlink":
        (final / "normalized.txt").unlink()
        (final / "normalized.txt").symlink_to(final / "manifest.json")
    elif mutation == "bad-json":
        (final / "chunks.jsonl").write_text("{\n")
    else:
        with (final / "chunks.jsonl").open("ab") as stream:
            stream.write((final / "chunks.jsonl").read_bytes())
    with pytest.raises(ArtifactError):
        with Phase5ArtifactReader(root, scope, expectation) as reader:
            tuple(reader.iter_chunks())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ordinal", 1),
        ("content_sha256", "0" * 64),
        ("byte_size", 999),
        ("char_end", 0),
        ("line_start", None),
        ("provenance_kind", "unknown"),
    ],
)
def test_artifact_reader_rejects_semantic_chunk_mismatch(
    tmp_path: Path, field: str, value: object
) -> None:
    root = _index_root(tmp_path)
    scope, expectation, final = _artifact(root)
    chunk = json.loads((final / "chunks.jsonl").read_text())
    chunk[field] = value
    payload = (json.dumps(chunk, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (final / "chunks.jsonl").write_bytes(payload)
    manifest = json.loads((final / "manifest.json").read_text())
    manifest["chunks_sha256"] = hashlib.sha256(payload).hexdigest()
    (final / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    expectation = replace(
        expectation,
        output_byte_size=sum(path.stat().st_size for path in final.iterdir()),
    )
    with pytest.raises(ArtifactError):
        with Phase5ArtifactReader(root, scope, expectation) as reader:
            tuple(reader.iter_chunks())


def test_model_manifest_requires_exact_safetensors_and_rejects_symlinks(tmp_path: Path) -> None:
    root = _index_root(tmp_path)
    location = model_directory(root)
    location.mkdir()
    (location / "model.safetensors").write_bytes(b"synthetic-not-a-model")
    manifest = build_manifest(location)
    (location / MANIFEST_NAME).write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    assert validate_model_store(root).revision == EMBEDDING_MODEL_REVISION
    (location / "unsafe").symlink_to(location / "model.safetensors")
    with pytest.raises(ModelStoreError):
        validate_model_store(root)


def test_model_manifest_rejects_hardlinked_assets(tmp_path: Path) -> None:
    root = _index_root(tmp_path)
    location = model_directory(root)
    location.mkdir()
    model = location / "model.safetensors"
    model.write_bytes(b"synthetic-not-a-model")
    manifest = build_manifest(location)
    (location / MANIFEST_NAME).write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    )
    (tmp_path / "outside-link").hardlink_to(model)
    with pytest.raises(ModelStoreError):
        validate_model_store(root)


def test_qdrant_payload_contains_only_reviewed_content_free_metadata() -> None:
    scope = DepartmentScope(uuid4())
    chunk_id = uuid4()
    point = VectorPoint(
        chunk_id=chunk_id,
        document_id=uuid4(),
        extraction_id=uuid4(),
        indexing_id=uuid4(),
        vector_attempt_id=uuid4(),
        chunk_ordinal=3,
        provenance_kind="page",
        page_start=2,
        page_end=2,
        line_start=None,
        line_end=None,
        vector=tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)),
    )
    payload = DepartmentQdrant._payload(scope, point, published=False)
    assert payload["department_id"] == str(scope.value)
    assert payload["chunk_id"] == str(chunk_id)
    assert payload["published"] is False
    assert {"text", "filename", "path", "content_sha256", "user_id"}.isdisjoint(payload)


def test_qdrant_boundary_rejects_raw_scope_and_oversized_upsert() -> None:
    adapter = object.__new__(DepartmentQdrant)
    with pytest.raises(QdrantBoundaryError, match="qdrant_verification_failed"):
        adapter._base_filter(str(uuid4()), published=True)
    scope = DepartmentScope(uuid4())
    point = VectorPoint(
        chunk_id=uuid4(),
        document_id=uuid4(),
        extraction_id=uuid4(),
        indexing_id=uuid4(),
        vector_attempt_id=uuid4(),
        chunk_ordinal=0,
        provenance_kind="line",
        page_start=None,
        page_end=None,
        line_start=1,
        line_end=1,
        vector=tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)),
    )
    with pytest.raises(QdrantBoundaryError, match="qdrant_write_failed"):
        adapter.upsert_staging(scope, (point,) * 65)


def test_qdrant_payload_validation_rejects_missing_or_foreign_scope() -> None:
    scope = DepartmentScope(uuid4())
    point = VectorPoint(
        chunk_id=uuid4(),
        document_id=uuid4(),
        extraction_id=uuid4(),
        indexing_id=uuid4(),
        vector_attempt_id=uuid4(),
        chunk_ordinal=0,
        provenance_kind="page",
        page_start=1,
        page_end=1,
        line_start=None,
        line_end=None,
        vector=tuple([1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)),
    )
    payload = DepartmentQdrant._payload(scope, point, published=True)
    for mutation in ("missing", "foreign"):
        candidate = dict(payload)
        if mutation == "missing":
            candidate.pop("department_id")
        else:
            candidate["department_id"] = str(uuid4())
        with pytest.raises(QdrantBoundaryError, match="qdrant_verification_failed"):
            DepartmentQdrant._validate_scope_payload(scope, candidate)


def test_no_public_search_route_and_no_direct_qdrant_client_outside_adapter() -> None:
    repository = Path(__file__).resolve().parents[3]
    routes = (repository / "apps/api/app/routes.py").read_text()
    assert "/search" not in routes and "query_vector" not in routes
    worker = repository / "services/rag-worker/deptslm_worker"
    offenders = []
    for path in worker.glob("*.py"):
        if path.name == "qdrant_adapter.py":
            continue
        if "QdrantClient" in path.read_text():
            offenders.append(path.name)
    assert offenders == []
