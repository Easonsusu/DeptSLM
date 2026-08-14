from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import AdapterEvaluationCancelRequest, AdapterEvaluationCreateRequest


def test_adapter_evaluation_create_body_is_closed():
    body = AdapterEvaluationCreateRequest(suite_id=uuid4(), expected_adapter_version=1)
    assert body.expected_adapter_version == 1
    with pytest.raises(ValidationError):
        AdapterEvaluationCreateRequest(
            suite_id=uuid4(), expected_adapter_version=1, model_id="Qwen/Qwen3-0.6B"
        )


@pytest.mark.parametrize(
    "field",
    ["runtime_url", "adapter_path", "seed", "temperature", "top_p", "prompt", "gate_values"],
)
def test_adapter_evaluation_create_rejects_runtime_and_generation_controls(field):
    payload = {"suite_id": uuid4(), "expected_adapter_version": 1, field: "blocked"}
    with pytest.raises(ValidationError):
        AdapterEvaluationCreateRequest(**payload)


def test_cancel_body_requires_exact_positive_version():
    with pytest.raises(ValidationError):
        AdapterEvaluationCancelRequest(expected_version=0)
