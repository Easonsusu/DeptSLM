"""Authenticated client for the private adapter-evaluation runtime."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import httpx

from app.adapter_contract import BASE_MODEL_ID, BASE_MODEL_REVISION
from app.rag_domain import RagContractError, runtime_generation_request

MAX_RUNTIME_RESPONSE_BYTES = 256 * 1024


class AdapterEvaluationRuntimeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: int,
        *,
        department_id: UUID,
        adapter_id: UUID,
        adapter_version: int,
        registry_publication_attempt_id: UUID,
        registry_attempt_number: int,
        registry_manifest_sha256: str,
        adapter_config_sha256: str,
        adapter_config_byte_size: int,
        adapter_model_sha256: str,
        adapter_model_byte_size: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._timeout = timeout_seconds
        self._target = {
            "target": "candidate",
            "base_model_id": BASE_MODEL_ID,
            "base_model_revision": BASE_MODEL_REVISION,
            "department_id": str(department_id),
            "adapter_id": str(adapter_id),
            "adapter_version": adapter_version,
            "registry_publication_attempt_id": str(registry_publication_attempt_id),
            "registry_attempt_number": registry_attempt_number,
            "registry_manifest_sha256": registry_manifest_sha256,
            "adapter_config_sha256": adapter_config_sha256,
            "adapter_config_byte_size": adapter_config_byte_size,
            "adapter_model_sha256": adapter_model_sha256,
            "adapter_model_byte_size": adapter_model_byte_size,
        }
        self._transport = transport

    def query_embedding(self, question: str) -> Any:
        raise RagContractError("runtime_unavailable")

    def generate(self, question: str, evidence, *, seed: int | None = None) -> Any:
        payload = runtime_generation_request(question, evidence)
        payload["operation"] = "generate"
        payload.update(self._target)
        if seed is not None:
            if (
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or not 0 <= seed <= (1 << 63) - 1
            ):
                raise RagContractError("invalid_generation_response")
            payload["seed"] = seed
        return self._post(payload)

    def _post(self, payload: dict[str, Any]) -> Any:
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
                    "/internal/v1/generate",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                ) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        if len(body) + len(chunk) > MAX_RUNTIME_RESPONSE_BYTES:
                            raise RagContractError("invalid_generation_response")
                        body.extend(chunk)
        except RagContractError:
            raise
        except httpx.TimeoutException as error:
            raise RagContractError("candidate_runtime_timeout") from error
        except httpx.HTTPError as error:
            raise RagContractError("candidate_runtime_unavailable") from error
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RagContractError("invalid_generation_response") from error
        if response.status_code != 200:
            code = value.get("code") if isinstance(value, dict) else None
            if code == "candidate_adapter_load_failed":
                raise RagContractError(code)
            if code == "candidate_runtime_timeout":
                raise RagContractError(code)
            raise RagContractError("candidate_runtime_unavailable")
        return value

    def verify_target(self) -> None:
        self._post({"operation": "verify", **self._target})
