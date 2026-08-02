"""Queue contract smoke tests for Phase 12.1C."""

from app.adapter_registry_queue import AdapterRegistryQueueError


def test_unknown_queue_error_code_is_safe() -> None:
    assert AdapterRegistryQueueError("not-safe").code == "adapter_registry_publication_failed"
