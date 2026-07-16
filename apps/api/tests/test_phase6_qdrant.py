"""Qdrant 1.13.4 integration coverage for Phase 6 tenant isolation."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from deptslm_worker.qdrant_adapter import (
    DepartmentQdrant,
    QdrantBoundaryError,
    VectorPoint,
)
from qdrant_client import QdrantClient
from qdrant_client.http import models

from app.authorization import DepartmentScope
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    QDRANT_VECTOR_NAME,
)

pytestmark = pytest.mark.qdrant


def _configuration() -> tuple[str, str]:
    url = os.getenv("DEPTSLM_TEST_QDRANT_URL")
    key = os.getenv("DEPTSLM_TEST_QDRANT_API_KEY")
    isolated = os.getenv("DEPTSLM_TEST_QDRANT_ISOLATED") == "1"
    if url and key and isolated:
        return url, key
    if os.getenv("DEPTSLM_REQUIRE_QDRANT_TESTS") == "1":
        pytest.fail("isolated Qdrant URL, key, and explicit isolation marker are required")
    pytest.skip("isolated Qdrant integration service is unavailable")


@pytest.fixture
def qdrant():
    url, key = _configuration()
    raw = QdrantClient(url=url, api_key=key, check_compatibility=False)
    if raw.collection_exists(QDRANT_COLLECTION):
        raw.delete_collection(QDRANT_COLLECTION)
    adapter = DepartmentQdrant(url, key, 10)
    adapter.bootstrap_collection()
    yield raw, adapter
    adapter.close()
    if raw.collection_exists(QDRANT_COLLECTION):
        raw.delete_collection(QDRANT_COLLECTION)
    raw.close()


def _vector(index: int = 0) -> tuple[float, ...]:
    value = [0.0] * EMBEDDING_DIMENSION
    value[index] = 1.0
    return tuple(value)


def _point(scope: DepartmentScope, indexing_id, attempt_id, ordinal=0) -> VectorPoint:
    return VectorPoint(
        chunk_id=uuid4(),
        document_id=uuid4(),
        extraction_id=uuid4(),
        indexing_id=indexing_id,
        vector_attempt_id=attempt_id,
        chunk_ordinal=ordinal,
        provenance_kind="line",
        page_start=None,
        page_end=None,
        line_start=ordinal + 1,
        line_end=ordinal + 1,
        vector=_vector(ordinal),
    )


def test_bootstrap_is_idempotent_and_creates_exact_tenant_schema(qdrant) -> None:
    raw, adapter = qdrant
    adapter.bootstrap_collection()
    information = raw.get_collection(QDRANT_COLLECTION)
    vector = information.config.params.vectors[QDRANT_VECTOR_NAME]
    assert vector.size == EMBEDDING_DIMENSION
    assert vector.distance == models.Distance.COSINE
    assert information.payload_schema["department_id"].params.is_tenant is True
    assert {
        "department_id",
        "document_id",
        "extraction_id",
        "indexing_id",
        "vector_attempt_id",
        "published",
        "embedding_pipeline_version",
    } <= set(information.payload_schema)


def test_scoped_count_inspect_activation_search_and_delete_are_exact(qdrant) -> None:
    _raw, adapter = qdrant
    first, second = DepartmentScope(uuid4()), DepartmentScope(uuid4())
    first_index, first_attempt = uuid4(), uuid4()
    second_index, second_attempt = uuid4(), uuid4()
    sibling_attempt = uuid4()
    sibling_index = uuid4()
    first_point = _point(first, first_index, first_attempt)
    second_point = _point(second, second_index, second_attempt)
    sibling_attempt_point = _point(first, first_index, sibling_attempt)
    sibling_index_point = _point(first, sibling_index, uuid4())
    adapter.upsert_staging(first, (first_point,))
    adapter.upsert_staging(second, (second_point,))
    adapter.upsert_staging(first, (sibling_attempt_point,))
    adapter.upsert_staging(first, (sibling_index_point,))
    assert adapter.count_attempt(first, first_index, first_attempt, published=False) == 1
    assert adapter.inspect_attempt(
        first, first_index, first_attempt, published=False, maximum=1
    ) == (first_point.chunk_id,)
    adapter.activate_attempt(first, first_index, first_attempt)
    hits = adapter.search_published(first, _vector(), limit=5)
    assert [hit.point_id for hit in hits] == [first_point.chunk_id]
    assert adapter.search_published(second, _vector(), limit=5) == ()
    adapter.delete_attempt(first, first_index, first_attempt)
    assert adapter.count_attempt(first, first_index, first_attempt, published=True) == 0
    assert adapter.count_attempt(second, second_index, second_attempt, published=False) == 1
    assert adapter.count_attempt(first, first_index, sibling_attempt, published=False) == 1
    assert (
        adapter.count_attempt(
            first,
            sibling_index,
            sibling_index_point.vector_attempt_id,
            published=False,
        )
        == 1
    )
    adapter.delete_attempt(first, first_index, first_attempt)


def test_malformed_matching_payload_fails_closed(qdrant) -> None:
    raw, adapter = qdrant
    scope = DepartmentScope(uuid4())
    raw.upsert(
        QDRANT_COLLECTION,
        points=[
            models.PointStruct(
                id=str(uuid4()),
                vector={QDRANT_VECTOR_NAME: list(_vector())},
                payload={
                    "department_id": str(scope.value),
                    "document_id": "not-a-uuid",
                    "extraction_id": str(uuid4()),
                    "chunk_id": str(uuid4()),
                    "indexing_id": str(uuid4()),
                    "vector_attempt_id": str(uuid4()),
                    "ordinal": 0,
                    "provenance_kind": "line",
                    "page_start": None,
                    "page_end": None,
                    "line_start": 1,
                    "line_end": 1,
                    "embedding_pipeline_version": EMBEDDING_PIPELINE_VERSION,
                    "published": True,
                },
            )
        ],
        wait=True,
    )
    with pytest.raises(QdrantBoundaryError, match="qdrant_verification_failed"):
        adapter.search_published(scope, _vector(), limit=5)


def test_existing_mismatched_collection_is_never_recreated(qdrant) -> None:
    raw, adapter = qdrant
    raw.delete_collection(QDRANT_COLLECTION)
    raw.create_collection(
        QDRANT_COLLECTION,
        vectors_config={"wrong": models.VectorParams(size=8, distance=models.Distance.DOT)},
    )
    url, key = _configuration()
    mismatch = DepartmentQdrant(url, key, 10)
    with pytest.raises(QdrantBoundaryError, match="qdrant_schema_mismatch"):
        mismatch.bootstrap_collection()
    information = raw.get_collection(QDRANT_COLLECTION)
    assert set(information.config.params.vectors) == {"wrong"}
    mismatch.close()


def _mismatched_vectors(kind: str):
    dense = models.VectorParams(size=EMBEDDING_DIMENSION, distance=models.Distance.COSINE)
    if kind == "extra":
        return {
            QDRANT_VECTOR_NAME: dense,
            "extra": models.VectorParams(size=4, distance=models.Distance.DOT),
        }
    if kind == "unnamed":
        return dense
    if kind == "wrong-name":
        return {"other": dense}
    if kind == "wrong-dimension":
        return {
            QDRANT_VECTOR_NAME: models.VectorParams(
                size=EMBEDDING_DIMENSION - 1, distance=models.Distance.COSINE
            )
        }
    if kind == "wrong-distance":
        return {
            QDRANT_VECTOR_NAME: models.VectorParams(
                size=EMBEDDING_DIMENSION, distance=models.Distance.DOT
            )
        }
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind", ["extra", "unnamed", "wrong-name", "wrong-dimension", "wrong-distance"]
)
def test_exact_dense_only_schema_rejects_every_mismatch_without_recreate(qdrant, kind: str) -> None:
    raw, adapter = qdrant
    raw.delete_collection(QDRANT_COLLECTION)
    raw.create_collection(QDRANT_COLLECTION, vectors_config=_mismatched_vectors(kind))
    before = raw.get_collection(QDRANT_COLLECTION).config.params.vectors
    with pytest.raises(QdrantBoundaryError, match="qdrant_schema_mismatch"):
        adapter.bootstrap_collection()
    after = raw.get_collection(QDRANT_COLLECTION).config.params.vectors
    assert type(after) is type(before)
    if isinstance(before, dict):
        assert set(after) == set(before)
        for name in before:
            assert after[name].size == before[name].size
            assert after[name].distance == before[name].distance
    else:
        assert after.size == before.size
        assert after.distance == before.distance


def test_mismatched_fixed_collection_points_are_never_mutated(qdrant) -> None:
    raw, adapter = qdrant
    raw.delete_collection(QDRANT_COLLECTION)
    raw.create_collection(QDRANT_COLLECTION, vectors_config=_mismatched_vectors("extra"))
    scope = DepartmentScope(uuid4())
    indexing_id, attempt_id, point_id = uuid4(), uuid4(), uuid4()
    payload = {
        "department_id": str(scope.value),
        "document_id": str(uuid4()),
        "extraction_id": str(uuid4()),
        "chunk_id": str(point_id),
        "indexing_id": str(indexing_id),
        "vector_attempt_id": str(attempt_id),
        "ordinal": 0,
        "provenance_kind": "line",
        "page_start": None,
        "page_end": None,
        "line_start": 1,
        "line_end": 1,
        "embedding_pipeline_version": EMBEDDING_PIPELINE_VERSION,
        "published": False,
    }
    raw.upsert(
        QDRANT_COLLECTION,
        points=[
            models.PointStruct(
                id=str(point_id),
                vector={QDRANT_VECTOR_NAME: list(_vector()), "extra": [1.0, 0.0, 0.0, 0.0]},
                payload=payload,
            )
        ],
        wait=True,
    )
    with pytest.raises(QdrantBoundaryError, match="qdrant_schema_mismatch"):
        adapter.verify_collection()
    with pytest.raises(QdrantBoundaryError, match="qdrant_schema_mismatch"):
        adapter.delete_attempt(scope, indexing_id, attempt_id)
    assert [str(point.id) for point in raw.retrieve(QDRANT_COLLECTION, ids=[str(point_id)])] == [
        str(point_id)
    ]


def test_missing_payload_index_fails_and_unknown_collection_is_untouched(qdrant) -> None:
    raw, adapter = qdrant
    unknown = f"phase6_unknown_{uuid4().hex}"
    raw.create_collection(
        unknown,
        vectors_config=models.VectorParams(size=4, distance=models.Distance.DOT),
    )
    try:
        raw.delete_payload_index(QDRANT_COLLECTION, "published", wait=True)
        with pytest.raises(QdrantBoundaryError, match="qdrant_schema_mismatch"):
            adapter.verify_collection()
        assert raw.collection_exists(unknown)
    finally:
        if raw.collection_exists(unknown):
            raw.delete_collection(unknown)
