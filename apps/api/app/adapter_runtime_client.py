"""Authenticated production adapter-runtime client with target fencing."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.adapter_runtime_contract import ADAPTER_RUNTIME_CONTRACT_VERSION, RuntimeTarget
from app.rag_domain import (
    RagContractError,
    runtime_generation_request,
)

MAX_RUNTIME_RESPONSE_BYTES = 256 * 1024
_SAFE_RUNTIME_ERRORS = frozenset(
    {
        "adapter_runtime_unavailable",
        "adapter_runtime_timeout",
        "adapter_load_failed",
        "adapter_runtime_target_mismatch",
    }
)


class AdapterRuntimeClient:
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

    def generate(self, target: RuntimeTarget, question: str, evidence) -> dict[str, object]:
        if target.target_kind != "adapter":
            raise RagContractError("adapter_runtime_target_mismatch")
        payload = {
            "operation": "generate",
            "target": "adapter",
            **target.adapter_request_fields(),
            **runtime_generation_request(question, evidence),
            "runtime_contract_version": ADAPTER_RUNTIME_CONTRACT_VERSION,
        }
        value = self._post(payload)
        if not isinstance(value, dict):
            raise RagContractError("invalid_generation_response")
        served = value.pop("served_target_fingerprint", None)
        if served != target.fingerprint:
            raise RagContractError("adapter_runtime_target_mismatch")
        if set(value) != {"status", "answer", "citations"}:
            raise RagContractError("invalid_generation_response")
        return value

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
                    if response.status_code != 200:
                        if response.status_code == 503:
                            try:
                                failure = json.loads(body)
                            except (UnicodeDecodeError, json.JSONDecodeError):
                                failure = None
                            if (
                                isinstance(failure, dict)
                                and set(failure) == {"code"}
                                and failure.get("code") in _SAFE_RUNTIME_ERRORS
                            ):
                                raise RagContractError(failure["code"])
                        raise RagContractError("adapter_runtime_unavailable")
        except RagContractError:
            raise
        except httpx.TimeoutException as error:
            raise RagContractError("adapter_runtime_timeout") from error
        except httpx.HTTPError as error:
            raise RagContractError("adapter_runtime_unavailable") from error
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RagContractError("invalid_generation_response") from error
