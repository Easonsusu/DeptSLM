"""Focused static and configuration boundaries for Phase 12.1E-C."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app import adapter_lifecycle_release
from app.adapter_lifecycle_release import AdapterLifecycleReleaseSettings
from app.admin import _parser


def _private(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _storage(root: Path) -> Path:
    for relative in (
        "adapters",
        "adapters/imports",
        "adapters/registry",
        "adapters/.staging",
        "adapters/.staging/imports",
        "adapters/.staging/registry",
        "adapters/.purge-deleting",
        "adapters/.purge-deleting/source_stage",
        "adapters/.purge-deleting/source_final",
        "adapters/.purge-deleting/registry_stage",
        "adapters/.purge-deleting/registry_final",
    ):
        _private(root / relative)
    root.chmod(0o700)
    return root


def test_release_command_accepts_only_reviewed_authority_selectors() -> None:
    department_id = uuid4()
    adapter_id = uuid4()
    args = _parser().parse_args(
        [
            "release-adapter-upstream-dependency",
            "--department-id",
            str(department_id),
            "--adapter-id",
            str(adapter_id),
            "--expected-adapter-version",
            "7",
            "--expected-source-version",
            "8",
            "--expected-dependency-version",
            "9",
            "--actor-issuer",
            "https://issuer.invalid",
            "--actor-subject",
            "opaque-subject",
        ]
    )

    assert args.command == "release-adapter-upstream-dependency"
    assert args.department_id == department_id and args.adapter_id == adapter_id
    assert (
        args.expected_adapter_version,
        args.expected_source_version,
        args.expected_dependency_version,
        args.apply,
    ) == (7, 8, 9, False)
    assert not any(
        hasattr(args, name)
        for name in (
            "path",
            "manifest",
            "digest",
            "operation_id",
            "attempt_id",
            "source_bundle_id",
            "training_job_id",
            "dataset_build_id",
            "dependency_id",
        )
    )


@pytest.mark.parametrize("value", ("0", "-1", "01x"))
def test_release_command_rejects_nonpositive_or_malformed_versions(value: str) -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "release-adapter-upstream-dependency",
                "--department-id",
                str(uuid4()),
                "--adapter-id",
                str(uuid4()),
                "--expected-adapter-version",
                value,
                "--expected-source-version",
                "1",
                "--expected-dependency-version",
                "1",
                "--actor-issuer",
                "issuer",
                "--actor-subject",
                "subject",
            ]
        )


def test_release_settings_require_only_the_private_adapter_maintenance_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _storage(tmp_path / "runtime")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test.invalid/deptslm")
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(root))

    settings = AdapterLifecycleReleaseSettings.from_environment()

    assert settings.data_dir == root
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_release_module_has_no_artifact_mutation_or_public_route_dependency() -> None:
    source = inspect.getsource(adapter_lifecycle_release)
    routes = Path(adapter_lifecycle_release.__file__).with_name("routes.py").read_text()

    assert "AdapterPurgeArtifactStore" in source
    assert "assert_tombstone_namespace_empty" in source
    assert "adapter.upstream_dependency.release" in source
    assert all(
        forbidden not in source
        for forbidden in (
            "move_verified_surface_to_tombstone",
            "unlink_committed_tombstone_entry",
            "remove_committed_tombstone_directory",
            "recover_authorized_move",
            "open_committed_tombstone",
        )
    )
    assert "release-adapter-upstream-dependency" not in routes
    assert "adapter.upstream_dependency.release" not in routes


def test_release_settings_do_not_fall_back_to_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test.invalid/deptslm")
    monkeypatch.delenv("DEPTSLM_DATA_DIR", raising=False)

    with pytest.raises(adapter_lifecycle_release.AdapterLifecycleReleaseConfigurationError):
        AdapterLifecycleReleaseSettings.from_environment()

    assert os.getenv("DEPTSLM_DATA_DIR") is None
