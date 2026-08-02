"""Pure Phase 12.1C manifest contract tests."""

import pytest

from app.adapter_registry_domain import (
    AdapterRegistryDomainError,
    canonical_json_bytes,
    parse_registry_manifest,
)


def test_registry_manifest_is_canonical_and_rejects_duplicates() -> None:
    assert canonical_json_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}\n'
    with pytest.raises(AdapterRegistryDomainError):
        parse_registry_manifest(b'{"a":1,"a":2}\n')
    with pytest.raises(AdapterRegistryDomainError):
        parse_registry_manifest(b"{}\n\n")
