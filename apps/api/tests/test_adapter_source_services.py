"""Unit coverage for the Phase 12.1B administrator-only source service."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import adapter_source_services as services
from app.adapter_source_artifacts import AdapterArtifactDigest
from app.admin import _parser


class _Transaction:
    def __init__(self) -> None:
        self.session = object()

    def __enter__(self):
        return self.session

    def __exit__(self, *_args: object) -> None:
        return None


class _Factory:
    def begin(self) -> _Transaction:
        return _Transaction()


class _Engine:
    def dispose(self) -> None:
        return None


def _settings(tmp_path: Path) -> services.AdapterSourceImportSettings:
    (tmp_path / "adapters").mkdir(mode=0o700)
    return services.AdapterSourceImportSettings(
        database_url="postgresql+psycopg://example",
        data_dir=tmp_path,
        code_revision="a" * 40,
    )


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "adapter_config.json"
    model = tmp_path / "adapter_model.safetensors"
    config.write_bytes(b"config")
    model.write_bytes(b"model")
    os.chmod(config, 0o600)
    os.chmod(model, 0o600)
    return config, model


def test_settings_require_existing_private_external_adapters_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://example")
    monkeypatch.setenv("DEPTSLM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CODE_REVISION", "b" * 40)
    with pytest.raises(services.AdapterSourceImportConfigurationError) as error:
        services.AdapterSourceImportSettings.from_environment()
    assert error.value.code == "adapter_input_unsafe"

    (tmp_path / "adapters").mkdir(mode=0o700)
    result = services.AdapterSourceImportSettings.from_environment()
    assert result.data_dir == tmp_path
    assert result.code_revision == "b" * 40


def test_dry_run_has_no_database_or_storage_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    config, model = _write_inputs(tmp_path)
    identity = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(services, "create_database_engine", lambda _url: _Engine())
    monkeypatch.setattr(services, "create_session_factory", lambda _engine: _Factory())
    monkeypatch.setattr(
        services,
        "authorize_transaction",
        lambda *_args, **_kwargs: SimpleNamespace(identity=identity),
    )
    monkeypatch.setattr(
        services,
        "_validate_and_hash",
        lambda *_args, **_kwargs: (
            {
                "tensor_dtype": "F16",
                "tensor_count": 392,
                "tensor_payload_byte_size": 20_185_088,
            },
            AdapterArtifactDigest("a" * 64, config.stat().st_size),
            AdapterArtifactDigest("b" * 64, model.stat().st_size),
        ),
    )

    result = services.import_adapter_source(
        settings,
        department_id=uuid4(),
        actor_issuer="issuer",
        actor_subject="subject",
        adapter_config=config,
        adapter_model=model,
        apply=False,
    )

    assert result.applied is False
    assert result.status == "validated"
    assert result.source_bundle_id is None
    assert result.tensor_dtype == "F16"
    assert not (tmp_path / "adapters" / "imports").exists()
    assert not (tmp_path / "adapters" / ".staging").exists()


def test_intake_manifest_is_closed_and_content_free() -> None:
    department_id, source_id, attempt_id, publication_id, actor_id = (uuid4() for _ in range(5))
    manifest = services._intake_manifest(
        department_id=department_id,
        source_bundle_id=source_id,
        import_attempt_id=attempt_id,
        publication_attempt_id=publication_id,
        attempt_number=1,
        imported_by_user_id=actor_id,
        code_revision="c" * 40,
        summary={
            "tensor_dtype": "BF16",
            "tensor_count": 392,
            "tensor_element_count": 10_092_544,
            "tensor_payload_byte_size": 20_185_088,
        },
        config_digest=AdapterArtifactDigest("d" * 64, 10),
        model_digest=AdapterArtifactDigest("e" * 64, 20),
    )
    assert set(manifest) == {
        "source_contract_version",
        "intake_contract_version",
        "config_contract_version",
        "tensor_contract_version",
        "department_id",
        "source_bundle_id",
        "import_attempt_id",
        "publication_attempt_id",
        "attempt_number",
        "imported_by_user_id",
        "code_revision",
        "base_model_id",
        "base_model_revision",
        "base_model_license",
        "peft_version",
        "safetensors_format",
        "tensor_dtype",
        "tensor_count",
        "tensor_element_count",
        "tensor_payload_byte_size",
        "files",
    }
    encoded = str(manifest)
    assert "path" not in encoded and "issuer" not in encoded and "subject" not in encoded


def test_cli_accepts_only_exact_adapter_source_arguments() -> None:
    args = _parser().parse_args(
        [
            "import-adapter-source",
            "--department-id",
            str(uuid4()),
            "--actor-issuer",
            "issuer",
            "--actor-subject",
            "subject",
            "--adapter-config",
            "adapter_config.json",
            "--adapter-model",
            "adapter_model.safetensors",
        ]
    )
    assert args.command == "import-adapter-source"
    assert args.apply is False
    assert not hasattr(args, "source_dir")
