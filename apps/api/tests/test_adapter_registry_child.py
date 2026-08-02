"""Child request and static-boundary tests for Phase 12.1C."""

from uuid import uuid4

import pytest

from app.adapter_registry_child import AdapterRegistryChildError, _exact_request


def test_child_request_rejects_extra_fields_and_boolean_attempt_number() -> None:
    with pytest.raises(AdapterRegistryChildError):
        _exact_request({"operation": "build_registry"})
    request = {
        key: 1
        for key in (
            "source_config_fd",
            "source_model_fd",
            "source_manifest_fd",
            "training_manifest_fd",
            "stage_fd",
        )
    }
    request.update(
        {
            "source_config_size": 1,
            "source_model_size": 1,
            "source_manifest_size": 1,
            "training_manifest_size": 1,
            "department_id": str(uuid4()),
            "adapter_id": str(uuid4()),
            "publication_attempt_id": str(uuid4()),
            "attempt_number": True,
            "code_revision": "a" * 40,
            "source": {},
            "governance_lineage": {},
        }
    )
    with pytest.raises(AdapterRegistryChildError):
        _exact_request(request)
