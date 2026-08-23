"""Test-only deterministic Phase 14.1 runtime.

This module is intentionally under ``tests`` and is never packaged into a
worker image or enabled through an environment variable.
"""

from __future__ import annotations

from collections.abc import Callable

from app.training_execution_domain import runtime_fingerprint
from app.training_execution_runtime import TrainingRuntimeRequest, TrainingRuntimeResult


class FakeTrainingRuntime:
    def __init__(self, classification: str = "execution_succeeded", error_code: str | None = None):
        self.classification = classification
        self.error_code = error_code

    def run(
        self,
        request: TrainingRuntimeRequest,
        *,
        should_stop: Callable[[], bool],
        heartbeat: Callable[[], None],
    ) -> TrainingRuntimeResult:
        heartbeat()
        if should_stop():
            return TrainingRuntimeResult(
                request.department_id,
                request.execution_id,
                request.attempt_id,
                request.training_job_id,
                request.authority_fingerprint,
                request.input_snapshot_fingerprint,
                runtime_fingerprint(
                    execution_id=request.execution_id,
                    attempt_id=request.attempt_id,
                    authority=request.authority_fingerprint,
                ),
                "execution_cancelled",
                "cancelled",
            )
        return TrainingRuntimeResult(
            request.department_id,
            request.execution_id,
            request.attempt_id,
            request.training_job_id,
            request.authority_fingerprint,
            request.input_snapshot_fingerprint,
            runtime_fingerprint(
                execution_id=request.execution_id,
                attempt_id=request.attempt_id,
                authority=request.authority_fingerprint,
            ),
            self.classification,
            self.error_code,
        )
