"""Explicit settings for the isolated adapter-evaluation worker."""

from __future__ import annotations

import os
from dataclasses import dataclass

from deptslm_worker.evaluation_settings import (
    EvaluationConfigurationError,
    EvaluationSettings,
)


@dataclass(frozen=True, slots=True)
class AdapterEvaluationSettings:
    evaluation: EvaluationSettings
    candidate_runtime_url: str
    candidate_runtime_token: str

    @classmethod
    def from_environment(cls) -> AdapterEvaluationSettings:
        evaluation = EvaluationSettings.from_environment()
        url = os.getenv("DEPTSLM_ADAPTER_EVAL_RUNTIME_URL", "").strip()
        token = os.getenv("DEPTSLM_ADAPTER_EVAL_RUNTIME_TOKEN", "")
        if not url.startswith("http://") or not url.rstrip("/"):
            raise EvaluationConfigurationError(
                "Adapter evaluation runtime URL is required."
            )
        if (
            len(token) < 32
            or token != token.strip()
            or any(character.isspace() for character in token)
        ):
            raise EvaluationConfigurationError(
                "Adapter evaluation runtime token is unsafe."
            )
        return cls(evaluation, url.rstrip("/"), token)
