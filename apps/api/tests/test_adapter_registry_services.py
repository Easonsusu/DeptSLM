"""Service-boundary coverage lives beside the other Phase 12 service tests."""

from app.adapter_registry_services import REGISTRY_CONTRACT_VERSION


def test_registry_contract_is_fixed() -> None:
    assert REGISTRY_CONTRACT_VERSION == "phase12-adapter-registry-v1"
