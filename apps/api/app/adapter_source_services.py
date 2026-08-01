"""Administrator-only, immutable Phase 12.1B adapter source intake."""

from __future__ import annotations

import hashlib
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

            source_id, attempt_id, publication_attempt_id, attempt_number, actor_id = _register(
                factory,
                principal,
                scope,
                settings.code_revision,
            )
            try:
                summary, config_digest, model_digest = _validate_and_hash(
                    config_input, model_input, include_digests=True
                )
            except AdapterSourceArtifactError as error:
                if error.code in _VALIDATION_FAILURES:
                    _reject_registered(
                        factory,
                        principal,
                        scope,
                        source_id,
                        attempt_id,
                        error.code,
                    )
                else:
                    _abandon_registered(
                        factory,
                        principal,
                        scope,
                        source_id,
                        attempt_id,
                        error.code,
                    )
                raise AdapterSourceImportConfigurationError(
                    "Adapter source validation failed.", error.code
                ) from error

            _mark_validated(
                factory,
                principal,
                scope,
                source_id,
                attempt_id,
                summary,
                config_digest,
                model_digest,
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
            )
            _mark_staged(factory, principal, scope, source_id, attempt_id, manifest)
            store.publish(staged)
            _mark_published(factory, principal, scope, source_id, attempt_id, manifest)
            _commit_published(
                factory,
                principal,
                scope,
                source_id,
                attempt_id,
                manifest,
                staged,
                summary,
                config_digest,
                model_digest,
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
            _abandon_registered(factory, principal, scope, source_id, attempt_id, error.code)
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
    *,
    include_digests: bool = False,
) -> tuple[dict[str, object], AdapterArtifactDigest | None, AdapterArtifactDigest | None]:
    summary = run_adapter_source_validation(
        config_fd=config.descriptor,
        model_fd=model.descriptor,
        config_size=config.size,
        model_size=model.size,
    )
    # Dry-run and apply both hash the retained descriptors; apply persists the
    # resulting digests only after the validation-authority transaction.
    config_digest = _digest_external(config)
    model_digest = _digest_external(model)
    return summary, config_digest, model_digest


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
        and metadata.st_nlink == 1
    )


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
        session.add_all((source, attempt))
        session.flush()
        return source_id, attempt_id, publication_attempt_id, 1, authorization.identity.id


def _validate_source_attempt(session, principal, scope, source_id, attempt_id, status):
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
        raise AdapterSourceImportConfigurationError("Adapter source authority changed.")
    return source, attempt


def _mark_validated(
    factory, principal, scope, source_id, attempt_id, summary, config_digest, model_digest
):
    with factory.begin() as session:
        source, attempt = _validate_source_attempt(
            session, principal, scope, source_id, attempt_id, "registered"
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


def _mark_staged(factory, principal, scope, source_id, attempt_id, manifest):
    with factory.begin() as session:
        source, attempt = _validate_source_attempt(
            session, principal, scope, source_id, attempt_id, "validated"
        )
        attempt.ownership_manifest = manifest
        attempt.status = "staged"
        attempt.staged_at = _server_now(session)
        source.version += 1
        attempt.version += 1
        session.flush()


def _mark_published(factory, principal, scope, source_id, attempt_id, manifest):
    with factory.begin() as session:
        source, attempt = _validate_source_attempt(
            session, principal, scope, source_id, attempt_id, "staged"
        )
        if attempt.ownership_manifest != manifest:
            raise AdapterSourceImportConfigurationError("Adapter source authority changed.")
        attempt.status = "published"
        attempt.published_at = _server_now(session)
        source.version += 1
        attempt.version += 1
        session.flush()


def _commit_published(
    factory,
    principal,
    scope,
    source_id,
    attempt_id,
    manifest,
    staged,
    summary,
    config_digest,
    model_digest,
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
        if source is None or attempt is None or attempt.ownership_manifest != manifest:
            raise AdapterSourceImportConfigurationError("Adapter source authority changed.")
        staged.recheck_identity()
        manifest_digest = hashlib.sha256(_manifest_bytes(manifest)).hexdigest()
        source.status = "committed"
        source.authoritative_attempt_id = attempt.id
        source.intake_manifest_sha256 = manifest_digest
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


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    import json

    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        + b"\n"
    )


def _reject_registered(factory, principal, scope, source_id, attempt_id, code):
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
            raise AdapterSourceImportConfigurationError("Adapter source authority changed.")
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


def _abandon_registered(factory, principal, scope, source_id, attempt_id, code):
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
    "import_adapter_source",
]
