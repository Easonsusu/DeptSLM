"""Service-boundary coverage lives beside the other Phase 12 service tests."""

from uuid import uuid4

import pytest

from app.adapter_registry_services import REGISTRY_CONTRACT_VERSION, enqueue_adapter_registry
from app.services import ServiceError


def test_registry_contract_is_fixed() -> None:
    assert REGISTRY_CONTRACT_VERSION == "phase12-adapter-registry-v1"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confirm_declared_training_association", False),
        ("apply", "true"),
        ("expected_source_version", 0),
        ("expected_training_job_version", 0),
        ("code_revision", "not-a-revision"),
    ],
)
def test_enqueue_rejects_invalid_control_inputs_before_database_access(field, value) -> None:
    values = {
        "source_bundle_id": uuid4(),
        "training_job_id": uuid4(),
        "expected_source_version": 1,
        "expected_training_job_version": 1,
        "confirm_declared_training_association": True,
        "apply": False,
        "code_revision": "a" * 40,
    }
    values[field] = value
    with pytest.raises(ServiceError):
        enqueue_adapter_registry(None, None, None, **values)
