"""Server-owned routing between the Phase 7 base and Phase 12.4 runtimes."""

from __future__ import annotations

from typing import Any

from app.adapter_runtime_contract import RuntimeTarget
from app.rag_domain import RagContractError


class RoutedRagRuntime:
    """Keep retrieval/query embedding on base while routing generation once."""

    def __init__(self, base_runtime: Any, adapter_runtime: Any | None, target: RuntimeTarget):
        self._base_runtime = base_runtime
        self._adapter_runtime = adapter_runtime
        self._target = target

    def query_embedding(self, question: str) -> Any:
        return self._base_runtime.query_embedding(question)

    def generate(self, question: str, evidence, *, seed: int | None = None) -> Any:
        if self._target.target_kind == "base":
            if seed is None:
                return self._base_runtime.generate(question, evidence)
            return self._base_runtime.generate(question, evidence, seed=seed)
        if seed is not None or self._adapter_runtime is None:
            raise RagContractError("adapter_runtime_unavailable")
        try:
            return self._adapter_runtime.generate(self._target, question, evidence)
        except RagContractError:
            raise
        except Exception as error:
            raise RagContractError("adapter_runtime_unavailable") from error
