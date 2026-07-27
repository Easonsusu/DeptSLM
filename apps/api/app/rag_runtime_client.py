"""Narrow authenticated HTTP client for the internal model-only RAG runtime."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.rag_domain import RagContractError, runtime_generation_request

MAX_RUNTIME_RESPONSE_BYTES = 256 * 1024


class RagRuntimeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: int,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    def query_embedding(self, question: str) -> Any:
        value = self._post(
            "/internal/v1/query-embedding",
            {"question": question},
            timeout_code="query_embedding_failed",
        )
        if not isinstance(value, dict) or set(value) != {"vector"}:
            raise RagContractError("invalid_query_embedding")
        return value["vector"]

    def generate(self, question: str, evidence, *, seed: int | None = None) -> Any:
        payload = runtime_generation_request(question, evidence)
        if seed is not None:
            if (
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or not 0 <= seed <= (1 << 63) - 1
            ):
                raise RagContractError("invalid_generation_response")
            payload["seed"] = seed
        return self._post(
            "/internal/v1/generate",
            payload,
            timeout_code="generation_timeout",
        )

    def _post(self, path: str, payload: dict[str, Any], *, timeout_code: str) -> Any:
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                with client.stream(
                    "POST",
                    path,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                ) as response:
                    if response.status_code != 200:
                        raise RagContractError("runtime_unavailable")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > MAX_RUNTIME_RESPONSE_BYTES:
                            raise RagContractError("invalid_generation_response")
                        body.extend(chunk)
        except RagContractError:
            raise
        except httpx.TimeoutException as error:
            raise RagContractError(timeout_code) from error
        except httpx.HTTPError as error:
            raise RagContractError("runtime_unavailable") from error
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RagContractError("invalid_generation_response") from error
