"""Fail-closed verification of the installed training-runtime dependency set."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from pathlib import Path

MANIFEST_PATH = Path("/opt/llamafactory/installed-distributions.json")


class DependencyAuthorityError(RuntimeError):
    """The image does not contain the reviewed dependency closure."""


def normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def canonical_manifest_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def read_expected_manifest(path: Path = MANIFEST_PATH) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DependencyAuthorityError() from error
    if not isinstance(value, dict) or set(value) != {"manifest_version", "distributions"}:
        raise DependencyAuthorityError()
    if value.get("manifest_version") != 1 or not isinstance(value.get("distributions"), dict):
        raise DependencyAuthorityError()
    distributions = value["distributions"]
    if not distributions or any(
        not isinstance(name, str)
        or normalize_distribution_name(name) != name
        or not isinstance(version, str)
        or not version
        for name, version in distributions.items()
    ):
        raise DependencyAuthorityError()
    if len(distributions) != len(set(distributions)):
        raise DependencyAuthorityError()
    return value


def installed_manifest() -> dict[str, object]:
    distributions: dict[str, str] = {}
    try:
        for distribution in importlib.metadata.distributions():
            raw_name = distribution.metadata.get("Name")
            version = distribution.version
            if not isinstance(raw_name, str) or not raw_name or not isinstance(version, str):
                raise DependencyAuthorityError()
            name = normalize_distribution_name(raw_name)
            if name in distributions and distributions[name] != version:
                raise DependencyAuthorityError()
            distributions[name] = version
    except (importlib.metadata.PackageNotFoundError, KeyError, TypeError) as error:
        raise DependencyAuthorityError() from error
    return {"manifest_version": 1, "distributions": dict(sorted(distributions.items()))}


def verify_installed_distributions(path: Path = MANIFEST_PATH) -> tuple[str, int]:
    expected = read_expected_manifest(path)
    actual = installed_manifest()
    if actual != expected:
        raise DependencyAuthorityError()
    return hashlib.sha256(canonical_manifest_bytes(actual)).hexdigest(), len(
        actual["distributions"]
    )


__all__ = [
    "DependencyAuthorityError",
    "canonical_manifest_bytes",
    "installed_manifest",
    "normalize_distribution_name",
    "read_expected_manifest",
    "verify_installed_distributions",
]
