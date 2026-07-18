"""The only direct Qdrant boundary for department-scoped vector operations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.authorization import DepartmentScope
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    QDRANT_VECTOR_NAME,
)
from deptslm_worker.embedding import validate_vector

MAX_UPSERT_POINTS = 64


class QdrantBoundaryError(RuntimeError):
    def __init__(self, code: str = "qdrant_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VectorPoint:
    chunk_id: UUID
    document_id: UUID
    extraction_id: UUID
    indexing_id: UUID
    vector_attempt_id: UUID
    chunk_ordinal: int
    provenance_kind: str
    page_start: int | None
    page_end: int | None
    line_start: int | None
    line_end: int | None
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class VectorHit:
    point_id: UUID
    document_id: UUID
    extraction_id: UUID
    indexing_id: UUID
    vector_attempt_id: UUID
    chunk_ordinal: int
    score: float


class DepartmentQdrant:
    """Construct all filters internally from a mandatory typed department scope."""

    def __init__(self, url: str, api_key: str, timeout_seconds: int) -> None:
        self._verified = False
        try:
            from qdrant_client import QdrantClient

            self._client = QdrantClient(
                url=url,
                api_key=api_key,
                timeout=timeout_seconds,
                check_compatibility=False,
            )
        except Exception as error:
            raise QdrantBoundaryError() from error

    def close(self) -> None:
        self._client.close()

    def verify_collection(self) -> None:
        self._verified = False
        try:
            self._verify_vector_contract()
            self._verify_payload_indexes()
            self._verified = True
        except QdrantBoundaryError:
            raise
        except Exception as error:
            raise QdrantBoundaryError() from error

    def bootstrap_collection(self) -> None:
        """Create the fixed schema only when absent; never recreate existing data."""
        self._verified = False
        try:
            from qdrant_client.http import models

            if not self._client.collection_exists(QDRANT_COLLECTION):
                self._client.create_collection(
                    QDRANT_COLLECTION,
                    vectors_config={
                        QDRANT_VECTOR_NAME: models.VectorParams(
                            size=EMBEDDING_DIMENSION,
                            distance=models.Distance.COSINE,
                        )
                    },
                )
            self._verify_vector_contract()
            indexes: tuple[tuple[str, Any], ...] = (
                (
                    "department_id",
                    models.KeywordIndexParams(type="keyword", is_tenant=True),
                ),
                ("document_id", models.PayloadSchemaType.KEYWORD),
                ("extraction_id", models.PayloadSchemaType.KEYWORD),
                ("indexing_id", models.PayloadSchemaType.KEYWORD),
                ("vector_attempt_id", models.PayloadSchemaType.KEYWORD),
                ("embedding_pipeline_version", models.PayloadSchemaType.KEYWORD),
                ("published", models.PayloadSchemaType.BOOL),
            )
            for field_name, schema in indexes:
                self._client.create_payload_index(
                    QDRANT_COLLECTION,
                    field_name=field_name,
                    field_schema=schema,
                    wait=True,
                )
            self.verify_collection()
        except QdrantBoundaryError:
            raise
        except Exception as error:
            raise QdrantBoundaryError("qdrant_schema_mismatch") from error

    def _verify_vector_contract(self) -> None:
        if not self._client.collection_exists(QDRANT_COLLECTION):
            raise QdrantBoundaryError("qdrant_schema_mismatch")
        information = self._client.get_collection(QDRANT_COLLECTION)
        vectors = information.config.params.vectors
        if not isinstance(vectors, dict) or set(vectors) != {QDRANT_VECTOR_NAME}:
            raise QdrantBoundaryError("qdrant_schema_mismatch")
        parameters = vectors[QDRANT_VECTOR_NAME]
        distance = getattr(getattr(parameters, "distance", None), "value", None)
        if (
            parameters is None
            or parameters.size != EMBEDDING_DIMENSION
            or str(distance).lower() != EMBEDDING_DISTANCE
        ):
            raise QdrantBoundaryError("qdrant_schema_mismatch")

    def _verify_payload_indexes(self) -> None:
        information = self._client.get_collection(QDRANT_COLLECTION)
        schema = information.payload_schema
        expected = {
            "department_id": "keyword",
            "document_id": "keyword",
            "extraction_id": "keyword",
            "indexing_id": "keyword",
            "vector_attempt_id": "keyword",
            "embedding_pipeline_version": "keyword",
            "published": "bool",
        }
        for field_name, data_type in expected.items():
            details = schema.get(field_name)
            actual = getattr(getattr(details, "data_type", None), "value", None)
            if str(actual).lower() != data_type:
                raise QdrantBoundaryError("qdrant_schema_mismatch")
        tenant = getattr(schema["department_id"], "params", None)
        if getattr(tenant, "is_tenant", None) is not True:
            raise QdrantBoundaryError("qdrant_schema_mismatch")

    def upsert_staging(self, scope: DepartmentScope, points: Sequence[VectorPoint]) -> None:
        _require_scope(scope)
        self._require_verified()
        if not points:
            return
        if len(points) > MAX_UPSERT_POINTS:
            raise QdrantBoundaryError("qdrant_write_failed")
        try:
            from qdrant_client.http import models

            records = []
            expected_indexing = points[0].indexing_id
            expected_attempt = points[0].vector_attempt_id
            for point in points:
                if (
                    not isinstance(point, VectorPoint)
                    or point.indexing_id != expected_indexing
                    or point.vector_attempt_id != expected_attempt
                ):
                    raise QdrantBoundaryError("qdrant_write_failed")
                payload = self._payload(scope, point, published=False)
                self._validate_attempt_payload(
                    scope,
                    payload,
                    indexing_id=expected_indexing,
                    vector_attempt_id=expected_attempt,
                    published=False,
                )
                vector = validate_vector(point.vector)
                records.append(
                    models.PointStruct(
                        id=str(point.chunk_id),
                        vector={QDRANT_VECTOR_NAME: list(vector)},
                        payload=payload,
                    )
                )
            self._client.upsert(QDRANT_COLLECTION, points=records, wait=True)
        except Exception as error:
            raise QdrantBoundaryError("qdrant_write_failed") from error

    def count_attempt(
        self,
        scope: DepartmentScope,
        indexing_id: UUID,
        vector_attempt_id: UUID,
        *,
        published: bool,
    ) -> int:
        _require_scope(scope)
        self._require_verified()
        try:
            result = self._client.count(
                QDRANT_COLLECTION,
                count_filter=self._attempt_filter(
                    scope, indexing_id, vector_attempt_id, published=published
                ),
                exact=True,
            )
            return int(result.count)
        except Exception as error:
            raise QdrantBoundaryError() from error

    def activate_attempt(
        self, scope: DepartmentScope, indexing_id: UUID, vector_attempt_id: UUID
    ) -> None:
        _require_scope(scope)
        self._require_verified()
        try:
            self._client.set_payload(
                QDRANT_COLLECTION,
                payload={"published": True},
                points=self._selector(
                    self._attempt_filter(scope, indexing_id, vector_attempt_id, published=False)
                ),
                wait=True,
            )
        except Exception as error:
            raise QdrantBoundaryError("qdrant_write_failed") from error

    def inspect_attempt(
        self,
        scope: DepartmentScope,
        indexing_id: UUID,
        vector_attempt_id: UUID,
        *,
        published: bool,
        maximum: int,
    ) -> tuple[UUID, ...]:
        _require_scope(scope)
        self._require_verified()
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise QdrantBoundaryError("qdrant_verification_failed")
        try:
            result = []
            offset = None
            while True:
                records, offset = self._client.scroll(
                    QDRANT_COLLECTION,
                    scroll_filter=self._attempt_filter(
                        scope, indexing_id, vector_attempt_id, published=published
                    ),
                    limit=min(256, maximum + 1 - len(result)),
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload or {}
                    self._validate_attempt_payload(
                        scope,
                        payload,
                        indexing_id=indexing_id,
                        vector_attempt_id=vector_attempt_id,
                        published=published,
                    )
                    if UUID(payload["chunk_id"]) != UUID(str(record.id)):
                        raise QdrantBoundaryError("qdrant_verification_failed")
                    result.append(UUID(str(record.id)))
                if len(result) > maximum:
                    raise QdrantBoundaryError("qdrant_verification_failed")
                if offset is None:
                    break
            return tuple(result)
        except QdrantBoundaryError:
            raise
        except Exception as error:
            raise QdrantBoundaryError("qdrant_verification_failed") from error

    def delete_attempt(
        self, scope: DepartmentScope, indexing_id: UUID, vector_attempt_id: UUID
    ) -> None:
        _require_scope(scope)
        self._require_verified()
        try:
            self._client.delete(
                QDRANT_COLLECTION,
                points_selector=self._selector(
                    self._attempt_filter(scope, indexing_id, vector_attempt_id)
                ),
                wait=True,
            )
            unpublished = self.count_attempt(scope, indexing_id, vector_attempt_id, published=False)
            published = self.count_attempt(scope, indexing_id, vector_attempt_id, published=True)
            if unpublished != 0 or published != 0:
                raise QdrantBoundaryError("qdrant_cleanup_failed")
        except QdrantBoundaryError as error:
            raise QdrantBoundaryError("qdrant_cleanup_failed") from error
        except Exception as error:
            raise QdrantBoundaryError("qdrant_cleanup_failed") from error

    def search_published(
        self,
        scope: DepartmentScope,
        query: Sequence[float],
        *,
        limit: int,
        document_id: UUID | None = None,
    ) -> tuple[VectorHit, ...]:
        """Internal-only primitive. Callers must cross-check PostgreSQL authority."""
        _require_scope(scope)
        self._require_verified()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise QdrantBoundaryError("qdrant_verification_failed")
        if document_id is not None and not isinstance(document_id, UUID):
            raise QdrantBoundaryError("qdrant_verification_failed")
        try:
            validated_query = validate_vector(query)
            query_filter = self._base_filter(scope, published=True)
            if document_id is not None:
                query_filter.must.append(self._condition("document_id", document_id))
            response = self._client.query_points(
                QDRANT_COLLECTION,
                query=list(validated_query),
                using=QDRANT_VECTOR_NAME,
                query_filter=query_filter,
                with_payload=True,
                limit=limit,
            )
            hits = []
            for scored in response.points:
                payload = scored.payload or {}
                self._validate_scope_payload(scope, payload)
                point_id = UUID(str(scored.id))
                if point_id != UUID(payload["chunk_id"]):
                    raise QdrantBoundaryError("qdrant_verification_failed")
                score = float(scored.score)
                if not math.isfinite(score) or not -1.0 <= score <= 1.0:
                    raise QdrantBoundaryError("qdrant_verification_failed")
                hits.append(
                    VectorHit(
                        point_id=point_id,
                        document_id=UUID(payload["document_id"]),
                        extraction_id=UUID(payload["extraction_id"]),
                        indexing_id=UUID(payload["indexing_id"]),
                        vector_attempt_id=UUID(payload["vector_attempt_id"]),
                        chunk_ordinal=_nonnegative_integer(payload["ordinal"]),
                        score=score,
                    )
                )
            return tuple(hits)
        except QdrantBoundaryError:
            raise
        except Exception as error:
            raise QdrantBoundaryError("qdrant_verification_failed") from error

    def _require_verified(self) -> None:
        if self._verified is not True:
            raise QdrantBoundaryError("qdrant_schema_mismatch")

    @staticmethod
    def _payload(
        scope: DepartmentScope, point: VectorPoint, *, published: bool
    ) -> dict[str, object]:
        return {
            "department_id": str(scope.value),
            "document_id": str(point.document_id),
            "extraction_id": str(point.extraction_id),
            "indexing_id": str(point.indexing_id),
            "vector_attempt_id": str(point.vector_attempt_id),
            "chunk_id": str(point.chunk_id),
            "ordinal": point.chunk_ordinal,
            "provenance_kind": point.provenance_kind,
            "page_start": point.page_start,
            "page_end": point.page_end,
            "line_start": point.line_start,
            "line_end": point.line_end,
            "embedding_pipeline_version": EMBEDDING_PIPELINE_VERSION,
            "published": published,
        }

    @staticmethod
    def _condition(key: str, value: object):
        from qdrant_client.http import models

        return models.FieldCondition(key=key, match=models.MatchValue(value=str(value)))

    @classmethod
    def _base_filter(cls, scope: DepartmentScope, *, published: bool | None = None):
        from qdrant_client.http import models

        _require_scope(scope)
        if published is not None and not isinstance(published, bool):
            raise QdrantBoundaryError("qdrant_verification_failed")
        must = [cls._condition("department_id", scope.value)]
        if published is not None:
            must.append(
                models.FieldCondition(key="published", match=models.MatchValue(value=published))
            )
        if published is True:
            must.append(cls._condition("embedding_pipeline_version", EMBEDDING_PIPELINE_VERSION))
        return models.Filter(must=must)

    @classmethod
    def _attempt_filter(
        cls,
        scope: DepartmentScope,
        indexing_id: UUID,
        vector_attempt_id: UUID,
        *,
        published: bool | None = None,
    ):
        if (
            not isinstance(indexing_id, UUID)
            or indexing_id.int == 0
            or not isinstance(vector_attempt_id, UUID)
            or vector_attempt_id.int == 0
        ):
            raise QdrantBoundaryError("qdrant_verification_failed")
        result = cls._base_filter(scope, published=published)
        result.must.extend(
            [
                cls._condition("indexing_id", indexing_id),
                cls._condition("vector_attempt_id", vector_attempt_id),
            ]
        )
        return result

    @staticmethod
    def _selector(query_filter):
        from qdrant_client.http import models

        return models.FilterSelector(filter=query_filter)

    @staticmethod
    def _validate_scope_payload(scope: DepartmentScope, payload: dict[str, Any]) -> None:
        DepartmentQdrant._validate_common_payload(scope, payload, published=True)

    @classmethod
    def _validate_attempt_payload(
        cls,
        scope: DepartmentScope,
        payload: dict[str, Any],
        *,
        indexing_id: UUID,
        vector_attempt_id: UUID,
        published: bool,
    ) -> None:
        cls._validate_common_payload(scope, payload, published=published)
        if payload.get("indexing_id") != str(indexing_id) or payload.get(
            "vector_attempt_id"
        ) != str(vector_attempt_id):
            raise QdrantBoundaryError("qdrant_verification_failed")

    @staticmethod
    def _validate_common_payload(
        scope: DepartmentScope, payload: dict[str, Any], *, published: bool
    ) -> None:
        allowed = {
            "department_id",
            "document_id",
            "extraction_id",
            "chunk_id",
            "indexing_id",
            "vector_attempt_id",
            "ordinal",
            "provenance_kind",
            "page_start",
            "page_end",
            "line_start",
            "line_end",
            "embedding_pipeline_version",
            "published",
        }
        if (
            set(payload) != allowed
            or payload.get("department_id") != str(scope.value)
            or payload.get("published") is not published
            or payload.get("embedding_pipeline_version") != EMBEDDING_PIPELINE_VERSION
        ):
            raise QdrantBoundaryError("qdrant_verification_failed")
        try:
            for key in (
                "document_id",
                "extraction_id",
                "chunk_id",
                "indexing_id",
                "vector_attempt_id",
            ):
                UUID(payload[key])
            _nonnegative_integer(payload["ordinal"])
        except (KeyError, TypeError, ValueError) as error:
            raise QdrantBoundaryError("qdrant_verification_failed") from error
        provenance = payload.get("provenance_kind")
        if provenance == "page":
            valid = (
                _positive_integer(payload.get("page_start"))
                and _positive_integer(payload.get("page_end"))
                and payload["page_end"] >= payload["page_start"]
                and payload.get("line_start") is None
                and payload.get("line_end") is None
            )
        elif provenance == "line":
            valid = (
                _positive_integer(payload.get("line_start"))
                and _positive_integer(payload.get("line_end"))
                and payload["line_end"] >= payload["line_start"]
                and payload.get("page_start") is None
                and payload.get("page_end") is None
            )
        else:
            valid = False
        if not valid:
            raise QdrantBoundaryError("qdrant_verification_failed")


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QdrantBoundaryError("qdrant_verification_failed")
    return value


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_scope(scope: object) -> DepartmentScope:
    if not isinstance(scope, DepartmentScope):
        raise QdrantBoundaryError("qdrant_verification_failed")
    return scope
