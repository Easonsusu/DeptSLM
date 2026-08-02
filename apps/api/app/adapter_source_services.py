"""Administrator-only, immutable Phase 12.1B adapter source intake."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.adapter_contract import (
    ADAPTER_CONFIG_CONTRACT_VERSION,
    ADAPTER_INTAKE_CONTRACT_VERSION,
    ADAPTER_SOURCE_CONTRACT_VERSION,
    ADAPTER_TENSOR_CONTRACT_VERSION,
    BASE_MODEL_ID,
    BASE_MODEL_LICENSE,
    BASE_MODEL_REVISION,
    PEFT_FORMAT_REFERENCE_VERSION,
    SAFETENSORS_FORMAT_REFERENCE_VERSION,
)
from app.adapter_source_artifacts import (
    AdapterArtifactDigest,
    AdapterSourceArtifactError,
    AdapterSourceArtifactStore,
    ExternalAdapterInput,
    StagedAdapterSource,
)
from app.adapter_source_supervision import run_adapter_source_validation
from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.models import AdapterImportAttempt, AdapterImportSource
from app.services import ServiceError, append_mutation_audit, authorize_transaction

_CODE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ADMIN_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
_STAGING_STATUS = "staging"
_VALIDATION_FAILURES = frozenset(
    {
        "adapter_config_invalid",
        "adapter_config_unsupported",
        "adapter_header_invalid",
        "adapter_header_too_large",
        "adapter_file_too_large",
        "adapter_tensor_set_invalid",
        "adapter_tensor_shape_invalid",
        "adapter_tensor_dtype_invalid",
        "adapter_tensor_offsets_invalid",
        "adapter_tensor_size_invalid",
        "adapter_input_invalid",
        "adapter_input_unsafe",
    }
)


class AdapterSourceImportConfigurationError(RuntimeError):
    """Safe setup or import failure; never contains operator paths or details."""

    def __init__(self, message: str = "Adapter source import failed.", code: str | None = None):
        self.code = code or "adapter_source_publication_failed"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class AdapterSourceImportSettings:
    database_url: str
    data_dir: Path
    code_revision: str

    @classmethod
    def from_environment(cls) -> AdapterSourceImportSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        code_revision = os.getenv("CODE_REVISION", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise AdapterSourceImportConfigurationError(
                "DATABASE_URL must use the postgresql+psycopg driver.", "database_unavailable"
            )
        if not raw_data_dir:
            raise AdapterSourceImportConfigurationError(
                "DEPTSLM_DATA_DIR is required.", "adapter_input_unsafe"
            )
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise AdapterSourceImportConfigurationError(
                "DEPTSLM_DATA_DIR must be an existing absolute directory.", "adapter_input_unsafe"
            )
        if _CODE_REVISION.fullmatch(code_revision) is None:
            raise AdapterSourceImportConfigurationError(
                "CODE_REVISION must be 40 lowercase hexadecimal characters.",
                "adapter_input_invalid",
            )
        try:
            _require_private_directory(data_dir / "adapters")
        except OSError as error:
            raise AdapterSourceImportConfigurationError(
                "Adapter storage is unavailable.", "adapter_input_unsafe"
            ) from error
        return cls(database_url=database_url, data_dir=data_dir, code_revision=code_revision)


@dataclass(frozen=True, slots=True)
class AdapterSourceImportResult:
    department_id: UUID
    source_bundle_id: UUID | None
    import_attempt_id: UUID | None
    status: str
    applied: bool
    base_model_display_id: str
    tensor_dtype: str
    tensor_count: int
    tensor_payload_byte_size: int

    @property
    def source_id(self) -> UUID | None:
        return self.source_bundle_id

    @property
    def base_model_id(self) -> str:
        return self.base_model_display_id

    @property
    def dtype(self) -> str:
        return self.tensor_dtype

    @property
    def aggregate_tensor_bytes(self) -> int:
        return self.tensor_payload_byte_size


@dataclass(frozen=True, slots=True)
class AdapterAuthoritySnapshot:
    """Frozen optimistic authority for one source and its exact attempt."""

    department_id: UUID
    source_bundle_id: UUID
    import_attempt_id: UUID
    publication_attempt_id: UUID
    attempt_number: int
    imported_by_user_id: UUID
    source_version: int
    attempt_version: int
    source_status: str
    attempt_status: str
    source_code_revision: str
    attempt_code_revision: str
    source_contract_version: str
    intake_contract_version: str
    config_contract_version: str
    tensor_contract_version: str
    base_model_id: str
    base_model_revision: str
    base_model_license: str
    peft_version: str
    safetensors_format: str
    adapter_config_sha256: str | None
    adapter_config_byte_size: int | None
    adapter_model_sha256: str | None
    adapter_model_byte_size: int | None
    intake_manifest_sha256: str | None
    intake_manifest_byte_size: int | None
    tensor_dtype: str | None
    tensor_count: int | None
    tensor_element_count: int | None
    tensor_payload_byte_size: int | None
    ownership_manifest: str | None


def import_adapter_source(
    settings: AdapterSourceImportSettings,
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    adapter_config: Path,
    adapter_model: Path,
    apply: bool,
) -> AdapterSourceImportResult:
    """Validate and optionally publish one immutable external source bundle."""

    if not isinstance(department_id, UUID) or department_id.int == 0:
        raise AdapterSourceImportConfigurationError(
            "Department is invalid.", "adapter_input_invalid"
        )
    if not actor_issuer or not actor_subject:
        raise AdapterSourceImportConfigurationError(
            "Requester is unauthorized.", "requester_unauthorized"
        )
    if not isinstance(apply, bool):
        raise AdapterSourceImportConfigurationError(
            "Import mode is invalid.", "adapter_input_invalid"
        )
    engine = None
    config_input: ExternalAdapterInput | None = None
    model_input: ExternalAdapterInput | None = None
    staged: StagedAdapterSource | None = None
    source_id: UUID | None = None
    attempt_id: UUID | None = None
    authority: AdapterAuthoritySnapshot | None = None
    principal = AuthenticatedPrincipal(subject=actor_subject, issuer=actor_issuer)
    scope = DepartmentRequestScope(DepartmentScope(department_id))
    try:
        # Opening the exact source descriptors precedes authorization, but no
        # database lock is retained while the child or hash pass runs.
        with AdapterSourceArtifactStore(settings.data_dir) as store:
            config_input, model_input = store.open_external_inputs(adapter_config, adapter_model)
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            with factory.begin() as session:
                authorize_transaction(
                    session,
                    principal,
                    scope,
                    _ADMIN_ROLES,
                    lock=False,
                    audit_action="adapter.source.import.authorization",
                )

            if not apply:
                summary, _config_digest, _model_digest = _validate_and_hash(
                    config_input, model_input
                )
                return AdapterSourceImportResult(
                    department_id=department_id,
                    source_bundle_id=None,
                    import_attempt_id=None,
                    status="validated",
                    applied=False,
                    base_model_display_id=BASE_MODEL_ID,
                    tensor_dtype=str(summary["tensor_dtype"]),
                    tensor_count=int(summary["tensor_count"]),
                    tensor_payload_byte_size=int(summary["tensor_payload_byte_size"]),
                )

            (
                source_id,
                attempt_id,
                publication_attempt_id,
                attempt_number,
                actor_id,
                authority,
            ) = _register(
                factory,
                principal,
                scope,
                settings.code_revision,
            )
            try:
                summary, config_digest, model_digest = _validate_and_hash(config_input, model_input)
            except AdapterSourceArtifactError as error:
                if error.code in _VALIDATION_FAILURES:
                    _reject_registered(
                        factory,
                        principal,
                        scope,
                        source_id,
                        attempt_id,
                        error.code,
                        expected=authority,
                    )
                else:
                    _abandon_registered(
                        factory,
                        principal,
                        scope,
                        source_id,
                        attempt_id,
                        error.code,
                        expected=authority,
                    )
                raise AdapterSourceImportConfigurationError(
                    "Adapter source validation failed.", error.code
                ) from error

            authority = _mark_validated(
                factory,
                principal,
                scope,
                source_id,
                attempt_id,
                summary,
                config_digest,
                model_digest,
                expected=authority,
            )
            manifest = _intake_manifest(
                department_id=department_id,
                source_bundle_id=source_id,
                import_attempt_id=attempt_id,
                publication_attempt_id=publication_attempt_id,
                attempt_number=attempt_number,
                imported_by_user_id=actor_id,
                code_revision=settings.code_revision,
                summary=summary,
                config_digest=config_digest,
                model_digest=model_digest,
            )
            staged = store.stage(
                scope.department,
                source_id,
                attempt_id,
                publication_attempt_id,
                attempt_number,
                config_input,
                model_input,
                manifest,
                expected_config=config_digest,
                expected_model=model_digest,
            )
            authority = _mark_staged(
                factory, principal, scope, source_id, attempt_id, manifest, expected=authority
            )
            store.publish(staged)
            authority = _mark_published(
                factory, principal, scope, source_id, attempt_id, manifest, expected=authority
            )
            authority = _commit_published(
                factory,
                principal,
                scope,
                source_id,
                attempt_id,
                manifest,
                staged,
                expected=authority,
            )
            staged = None
            return AdapterSourceImportResult(
                department_id=department_id,
                source_bundle_id=source_id,
                import_attempt_id=attempt_id,
                status="committed",
                applied=True,
                base_model_display_id=BASE_MODEL_ID,
                tensor_dtype=str(summary["tensor_dtype"]),
                tensor_count=int(summary["tensor_count"]),
                tensor_payload_byte_size=int(summary["tensor_payload_byte_size"]),
            )
    except AdapterSourceImportConfigurationError:
        raise
    except AdapterSourceArtifactError as error:
        if source_id is not None and attempt_id is not None:
            _abandon_registered(
                factory,
                principal,
                scope,
                source_id,
                attempt_id,
                error.code,
                expected=authority,
            )
        raise AdapterSourceImportConfigurationError(
            "Adapter source import failed.", error.code
        ) from error
    except ServiceError as error:
        code = (
            "requester_unauthorized"
            if error.status_code == 403
            else "database_unavailable"
            if error.status_code >= 500
            else "adapter_input_invalid"
        )
        raise AdapterSourceImportConfigurationError(
            "Adapter source import failed.", code
        ) from error
    except (IntegrityError, SQLAlchemyError, OSError, ValueError, TypeError) as error:
        if source_id is not None and attempt_id is not None:
            _abandon_registered(
                factory,
                principal,
                scope,
                source_id,
                attempt_id,
                "database_unavailable"
                if isinstance(error, SQLAlchemyError)
                else "adapter_input_invalid",
                expected=authority,
            )
        raise AdapterSourceImportConfigurationError("Adapter source import failed.") from error
    finally:
        if staged is not None:
            staged.close()
        if config_input is not None:
            config_input.close()
        if model_input is not None:
            model_input.close()
        if engine is not None:
            engine.dispose()


def _validate_and_hash(
    config: ExternalAdapterInput,
    model: ExternalAdapterInput,
) -> tuple[dict[str, object], AdapterArtifactDigest, AdapterArtifactDigest]:
    # The first complete digest pass binds the retained descriptors before the
    # isolated child is allowed to inspect them.
    config_before = _digest_external(config)
    model_before = _digest_external(model)
    summary = run_adapter_source_validation(
        config_fd=config.descriptor,
        model_fd=model.descriptor,
        config_size=config.size,
        model_size=model.size,
    )
    # A second complete pass immediately after child validation binds the
    # validated bytes.  Same-inode, same-size in-place changes are detected by
    # digest comparison even though descriptor identity is unchanged.
    config_after = _digest_external(config)
    model_after = _digest_external(model)
    if config_before != config_after or model_before != model_after:
        raise AdapterSourceArtifactError("adapter_source_changed")
    return summary, config_after, model_after


def _digest_external(source: ExternalAdapterInput) -> AdapterArtifactDigest:
    digest = hashlib.sha256()
    offset = 0
    total = 0
    try:
        before = os.fstat(source.descriptor)
        if not _same_identity(source, before):
            raise AdapterSourceArtifactError("adapter_source_changed")
        while offset < source.size:
            block = os.pread(source.descriptor, min(1024 * 1024, source.size - offset), offset)
            if not block:
                raise AdapterSourceArtifactError("adapter_source_changed")
            digest.update(block)
            offset += len(block)
            total += len(block)
        after = os.fstat(source.descriptor)
    except OSError as error:
        raise AdapterSourceArtifactError("adapter_source_changed") from error
    if total != source.size or not _same_identity(source, after):
        raise AdapterSourceArtifactError("adapter_source_changed")
    return AdapterArtifactDigest(digest.hexdigest(), total)


def _same_identity(source: ExternalAdapterInput, metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == source.device
        and metadata.st_ino == source.inode
        and metadata.st_size == source.size
        and stat.S_IMODE(metadata.st_mode) == source.mode
        and metadata.st_uid == source.uid
        and metadata.st_nlink == source.nlink == 1
    )


def _manifest_fingerprint(value: dict[str, object] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot(
    source: AdapterImportSource, attempt: AdapterImportAttempt
) -> AdapterAuthoritySnapshot:
    if source.id != attempt.source_bundle_id or source.department_id != attempt.department_id:
        raise AdapterSourceImportConfigurationError(
            "Adapter source authority changed.", "adapter_source_authority_changed"
        )
    return AdapterAuthoritySnapshot(
        department_id=source.department_id,
        source_bundle_id=source.id,
        import_attempt_id=attempt.id,
        publication_attempt_id=attempt.publication_attempt_id,
        attempt_number=attempt.attempt_number,
        imported_by_user_id=source.imported_by_user_id,
        source_version=source.version,
        attempt_version=attempt.version,
        source_status=source.status,
        attempt_status=attempt.status,
        source_code_revision=source.code_revision,
        attempt_code_revision=attempt.code_revision,
        source_contract_version=source.source_contract_version,
        intake_contract_version=source.intake_contract_version,
        config_contract_version=source.config_contract_version,
        tensor_contract_version=source.tensor_contract_version,
        base_model_id=source.base_model_id,
        base_model_revision=source.base_model_revision,
        base_model_license=source.base_model_license,
        peft_version=source.peft_version,
        safetensors_format=source.safetensors_format,
        adapter_config_sha256=source.adapter_config_sha256,
        adapter_config_byte_size=source.adapter_config_byte_size,
        adapter_model_sha256=source.adapter_model_sha256,
        adapter_model_byte_size=source.adapter_model_byte_size,
        intake_manifest_sha256=source.intake_manifest_sha256,
        intake_manifest_byte_size=source.intake_manifest_byte_size,
        tensor_dtype=source.tensor_dtype,
        tensor_count=source.tensor_count,
        tensor_element_count=source.tensor_element_count,
        tensor_payload_byte_size=source.tensor_payload_byte_size,
        ownership_manifest=_manifest_fingerprint(attempt.ownership_manifest),
    )


def _require_snapshot(
    source: AdapterImportSource,
    attempt: AdapterImportAttempt,
    expected: AdapterAuthoritySnapshot | None,
) -> AdapterAuthoritySnapshot:
    actual = _snapshot(source, attempt)
    if expected is not None and actual != expected:
        raise AdapterSourceImportConfigurationError(
            "Adapter source authority changed.", "adapter_source_authority_changed"
        )
    return actual


def _register(factory, principal, scope, code_revision):
    source_id = uuid4()
    attempt_id = uuid4()
    publication_attempt_id = uuid4()
    with factory.begin() as session:
        authorization = authorize_transaction(
            session,
            principal,
            scope,
            _ADMIN_ROLES,
            lock=True,
            audit_action="adapter.source.import.authorization",
        )
        source = AdapterImportSource(
            id=source_id,
            department_id=scope.department.value,
            imported_by_user_id=authorization.identity.id,
            status="staging",
            source_contract_version=ADAPTER_SOURCE_CONTRACT_VERSION,
            intake_contract_version=ADAPTER_INTAKE_CONTRACT_VERSION,
            config_contract_version=ADAPTER_CONFIG_CONTRACT_VERSION,
            tensor_contract_version=ADAPTER_TENSOR_CONTRACT_VERSION,
            base_model_id=BASE_MODEL_ID,
            base_model_revision=BASE_MODEL_REVISION,
            base_model_license=BASE_MODEL_LICENSE,
            peft_version=PEFT_FORMAT_REFERENCE_VERSION,
            safetensors_format=SAFETENSORS_FORMAT_REFERENCE_VERSION,
            code_revision=code_revision,
            version=1,
        )
        attempt = AdapterImportAttempt(
            id=attempt_id,
            department_id=scope.department.value,
            source_bundle_id=source_id,
            attempt_number=1,
            publication_attempt_id=publication_attempt_id,
            status="registered",
            code_revision=code_revision,
            version=1,
        )
        # Flush the parent before the composite child insert.  SQLAlchemy's
        # unit-of-work ordering does not reliably infer this dependency from
        # the content-free composite foreign key when no ORM relationship is
        # declared, and PostgreSQL must see the exact department/source pair.
        session.add(source)
        session.flush()
        session.add(attempt)
        session.flush()
        return (
            source_id,
            attempt_id,
            publication_attempt_id,
            1,
            authorization.identity.id,
            _snapshot(source, attempt),
        )


def _validate_source_attempt(
    session,
    principal,
    scope,
    source_id,
    attempt_id,
    status,
    *,
    expected: AdapterAuthoritySnapshot | None = None,
):
    authorize_transaction(
        session,
        principal,
        scope,
        _ADMIN_ROLES,
        lock=True,
        audit_action="adapter.source.import.authorization",
    )
    source = session.execute(
        select(AdapterImportSource)
        .where(
            AdapterImportSource.id == source_id,
            AdapterImportSource.department_id == scope.department.value,
            AdapterImportSource.status == "staging",
        )
        .with_for_update()
    ).scalar_one_or_none()
    attempt = session.execute(
        select(AdapterImportAttempt)
        .where(
            AdapterImportAttempt.id == attempt_id,
            AdapterImportAttempt.department_id == scope.department.value,
            AdapterImportAttempt.source_bundle_id == source_id,
            AdapterImportAttempt.status == status,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if source is None or attempt is None:
        raise AdapterSourceImportConfigurationError(
            "Adapter source authority changed.", "adapter_source_authority_changed"
        )
    _require_snapshot(source, attempt, expected)
    return source, attempt


def _mark_validated(
    factory,
    principal,
    scope,
    source_id,
    attempt_id,
    summary,
    config_digest,
    model_digest,
    *,
    expected: AdapterAuthoritySnapshot | None = None,
):
    with factory.begin() as session:
        source, attempt = _validate_source_attempt(
            session,
            principal,
            scope,
            source_id,
            attempt_id,
            "registered",
            expected=expected,
        )
        source.adapter_config_sha256 = config_digest.sha256
        source.adapter_config_byte_size = config_digest.byte_size
        source.adapter_model_sha256 = model_digest.sha256
        source.adapter_model_byte_size = model_digest.byte_size
        source.tensor_dtype = str(summary["tensor_dtype"])
        source.tensor_count = int(summary["tensor_count"])
        source.tensor_element_count = int(summary["tensor_element_count"])
        source.tensor_payload_byte_size = int(summary["tensor_payload_byte_size"])
        attempt.status = "validated"
        attempt.validated_at = _server_now(session)
        source.version += 1
        attempt.version += 1
        session.flush()
        return _snapshot(source, attempt)


def _intake_manifest(
    *,
    department_id,
    source_bundle_id,
    import_attempt_id,
    publication_attempt_id,
    attempt_number,
    imported_by_user_id,
    code_revision,
    summary,
    config_digest,
    model_digest,
) -> dict[str, object]:
    return {
        "source_contract_version": ADAPTER_SOURCE_CONTRACT_VERSION,
        "intake_contract_version": ADAPTER_INTAKE_CONTRACT_VERSION,
        "config_contract_version": ADAPTER_CONFIG_CONTRACT_VERSION,
        "tensor_contract_version": ADAPTER_TENSOR_CONTRACT_VERSION,
        "department_id": str(department_id),
        "source_bundle_id": str(source_bundle_id),
        "import_attempt_id": str(import_attempt_id),
        "publication_attempt_id": str(publication_attempt_id),
        "attempt_number": attempt_number,
        "imported_by_user_id": str(imported_by_user_id),
        "code_revision": code_revision,
        "base_model_id": BASE_MODEL_ID,
        "base_model_revision": BASE_MODEL_REVISION,
        "base_model_license": BASE_MODEL_LICENSE,
        "peft_version": PEFT_FORMAT_REFERENCE_VERSION,
        "safetensors_format": SAFETENSORS_FORMAT_REFERENCE_VERSION,
        "tensor_dtype": summary["tensor_dtype"],
        "tensor_count": summary["tensor_count"],
        "tensor_element_count": summary["tensor_element_count"],
        "tensor_payload_byte_size": summary["tensor_payload_byte_size"],
        "files": {
            "adapter_config.json": {
                "sha256": config_digest.sha256,
                "byte_size": config_digest.byte_size,
            },
            "adapter_model.safetensors": {
                "sha256": model_digest.sha256,
                "byte_size": model_digest.byte_size,
            },
        },
    }


def _mark_staged(factory, principal, scope, source_id, attempt_id, manifest, *, expected=None):
    with factory.begin() as session:
        source, attempt = _validate_source_attempt(
            session,
            principal,
            scope,
            source_id,
            attempt_id,
            "validated",
            expected=expected,
        )
        attempt.ownership_manifest = manifest
        attempt.status = "staged"
        attempt.staged_at = _server_now(session)
        source.version += 1
        attempt.version += 1
        session.flush()
        return _snapshot(source, attempt)


def _mark_published(factory, principal, scope, source_id, attempt_id, manifest, *, expected=None):
    with factory.begin() as session:
        source, attempt = _validate_source_attempt(
            session,
            principal,
            scope,
            source_id,
            attempt_id,
            "staged",
            expected=expected,
        )
        if attempt.ownership_manifest != manifest:
            raise AdapterSourceImportConfigurationError(
                "Adapter source authority changed.", "adapter_source_authority_changed"
            )
        attempt.status = "published"
        attempt.published_at = _server_now(session)
        source.version += 1
        attempt.version += 1
        session.flush()
        return _snapshot(source, attempt)


def _manifest_uuid(value: object) -> UUID | None:
    try:
        if not isinstance(value, str):
            return None
        parsed = UUID(value)
        return parsed if str(parsed) == value and parsed.int else None
    except (TypeError, ValueError):
        return None


def _manifest_file_digest(value: object) -> AdapterArtifactDigest | None:
    if not isinstance(value, dict):
        return None
    digest = value.get("sha256")
    size = value.get("byte_size")
    if type(digest) is not str or len(digest) != 64 or type(size) is not int or size <= 0:
        return None
    return AdapterArtifactDigest(digest, size)


def _commit_published(
    factory,
    principal,
    scope,
    source_id,
    attempt_id,
    manifest,
    staged,
    *,
    expected: AdapterAuthoritySnapshot | None = None,
):
    with factory.begin() as session:
        authorization = authorize_transaction(
            session,
            principal,
            scope,
            _ADMIN_ROLES,
            lock=True,
            audit_action="adapter.source.import.authorization",
        )
        source = session.execute(
            select(AdapterImportSource)
            .where(
                AdapterImportSource.id == source_id,
                AdapterImportSource.department_id == scope.department.value,
                AdapterImportSource.status == "staging",
            )
            .with_for_update()
        ).scalar_one_or_none()
        attempt = session.execute(
            select(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.id == attempt_id,
                AdapterImportAttempt.department_id == scope.department.value,
                AdapterImportAttempt.source_bundle_id == source_id,
                AdapterImportAttempt.status == "published",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if source is None or attempt is None:
            raise AdapterSourceImportConfigurationError(
                "Adapter source authority changed.", "adapter_source_authority_changed"
            )
        _require_snapshot(source, attempt, expected)
        if attempt.ownership_manifest != manifest:
            raise AdapterSourceImportConfigurationError(
                "Adapter source authority changed.", "adapter_source_authority_changed"
            )
        staged.recheck_retained_authority()
        manifest_digest = hashlib.sha256(_manifest_bytes(manifest)).hexdigest()
        digest_map = staged.digest_map
        files = manifest.get("files")
        config_file = files.get("adapter_config.json") if isinstance(files, dict) else None
        model_file = files.get("adapter_model.safetensors") if isinstance(files, dict) else None
        manifest_contract_matches = all(
            getattr(source, field) == manifest.get(key)
            for field, key in (
                ("source_contract_version", "source_contract_version"),
                ("intake_contract_version", "intake_contract_version"),
                ("config_contract_version", "config_contract_version"),
                ("tensor_contract_version", "tensor_contract_version"),
                ("base_model_id", "base_model_id"),
                ("base_model_revision", "base_model_revision"),
                ("base_model_license", "base_model_license"),
                ("peft_version", "peft_version"),
                ("safetensors_format", "safetensors_format"),
            )
        )
        if (
            not isinstance(config_file, dict)
            or not isinstance(model_file, dict)
            or not manifest_contract_matches
            or _manifest_uuid(manifest.get("department_id")) != source.department_id
            or _manifest_uuid(manifest.get("source_bundle_id")) != source.id
            or _manifest_uuid(manifest.get("import_attempt_id")) != attempt.id
            or _manifest_uuid(manifest.get("publication_attempt_id"))
            != attempt.publication_attempt_id
            or type(manifest.get("attempt_number")) is not int
            or manifest.get("attempt_number") != attempt.attempt_number
            or _manifest_uuid(manifest.get("imported_by_user_id")) != source.imported_by_user_id
            or source.adapter_config_sha256 != config_file.get("sha256")
            or source.adapter_config_byte_size != config_file.get("byte_size")
            or source.adapter_model_sha256 != model_file.get("sha256")
            or source.adapter_model_byte_size != model_file.get("byte_size")
            or digest_map.get("adapter_config.json") != _manifest_file_digest(config_file)
            or digest_map.get("adapter_model.safetensors") != _manifest_file_digest(model_file)
            or digest_map.get("intake_manifest.json")
            != AdapterArtifactDigest(manifest_digest, len(_manifest_bytes(manifest)))
            or source.tensor_dtype != manifest.get("tensor_dtype")
            or source.tensor_count != manifest.get("tensor_count")
            or source.tensor_element_count != manifest.get("tensor_element_count")
            or source.tensor_payload_byte_size != manifest.get("tensor_payload_byte_size")
            or source.code_revision != manifest.get("code_revision")
            or attempt.code_revision != manifest.get("code_revision")
            or source.intake_manifest_sha256 not in (None, manifest_digest)
        ):
            raise AdapterSourceImportConfigurationError(
                "Adapter source authority changed.", "adapter_source_authority_changed"
            )
        source.status = "committed"
        source.authoritative_attempt_id = attempt.id
        source.intake_manifest_sha256 = manifest_digest
        source.intake_manifest_byte_size = len(_manifest_bytes(manifest))
        source.committed_at = _server_now(session)
        source.error_code = None
        attempt.status = "committed"
        now = _server_now(session)
        attempt.committed_at = now
        attempt.finished_at = now
        source.version += 1
        attempt.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action="adapter.source.import",
            resource_type="adapter_import_source",
            resource_id=source.id,
        )
        session.flush()
        return _snapshot(source, attempt)


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    import json

    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )


def _reject_registered(factory, principal, scope, source_id, attempt_id, code, *, expected=None):
    code = code if code in _VALIDATION_FAILURES else "adapter_input_invalid"
    with factory.begin() as session:
        authorization = authorize_transaction(
            session,
            principal,
            scope,
            _ADMIN_ROLES,
            lock=True,
            audit_action="adapter.source.import.authorization",
        )
        source = session.execute(
            select(AdapterImportSource)
            .where(
                AdapterImportSource.id == source_id,
                AdapterImportSource.department_id == scope.department.value,
                AdapterImportSource.status == "staging",
            )
            .with_for_update()
        ).scalar_one_or_none()
        attempt = session.execute(
            select(AdapterImportAttempt)
            .where(
                AdapterImportAttempt.id == attempt_id,
                AdapterImportAttempt.department_id == scope.department.value,
                AdapterImportAttempt.source_bundle_id == source_id,
                AdapterImportAttempt.status == "registered",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if source is None or attempt is None:
            raise AdapterSourceImportConfigurationError(
                "Adapter source authority changed.", "adapter_source_authority_changed"
            )
        _require_snapshot(source, attempt, expected)
        now = _server_now(session)
        source.status = "rejected"
        source.error_code = code
        source.rejected_at = now
        attempt.status = "failed"
        attempt.error_code = code
        attempt.finished_at = now
        source.version += 1
        attempt.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=scope,
            action="adapter.source.reject",
            resource_type="adapter_import_source",
            resource_id=source.id,
        )
        session.flush()


def _abandon_registered(factory, principal, scope, source_id, attempt_id, code, *, expected=None):
    if factory is None:
        return
    code = code if code not in _VALIDATION_FAILURES else "adapter_source_changed"
    try:
        with factory.begin() as session:
            authorize_transaction(
                session,
                principal,
                scope,
                _ADMIN_ROLES,
                lock=True,
                audit_action="adapter.source.import.authorization",
            )
            source = session.execute(
                select(AdapterImportSource)
                .where(
                    AdapterImportSource.id == source_id,
                    AdapterImportSource.department_id == scope.department.value,
                    AdapterImportSource.status == "staging",
                )
                .with_for_update()
            ).scalar_one_or_none()
            attempt = session.execute(
                select(AdapterImportAttempt)
                .where(
                    AdapterImportAttempt.id == attempt_id,
                    AdapterImportAttempt.department_id == scope.department.value,
                    AdapterImportAttempt.source_bundle_id == source_id,
                    AdapterImportAttempt.status.in_(("registered", "validated")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if source is None or attempt is None:
                return
            _require_snapshot(source, attempt, expected)
            now = _server_now(session)
            source.status = "abandoned"
            source.error_code = code
            source.abandoned_at = now
            attempt.status = "abandoned"
            attempt.error_code = code
            attempt.finished_at = now
            source.version += 1
            attempt.version += 1
            session.flush()
    except (SQLAlchemyError, ServiceError):
        # Never replace the original safe failure with a database exception.
        return


def _server_now(session):
    value = session.scalar(select(func.clock_timestamp()))
    if value is None:
        raise ServiceError(503, "Database unavailable")
    return value


def _require_private_directory(path: Path) -> None:
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
        or not metadata.st_mode & stat.S_IWUSR
    ):
        raise OSError("private adapter storage is required")


__all__ = [
    "AdapterSourceImportConfigurationError",
    "AdapterSourceImportSettings",
    "AdapterSourceImportResult",
    "AdapterAuthoritySnapshot",
    "import_adapter_source",
]
