"""Department-scoped, content-free SFT source and dataset-build control plane."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.models import (
    SftArtifactReconciliationOperationItem,
    SftDatasetBuild,
    SftSourceBundle,
    SftSourceImportAttempt,
)
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.sft_artifacts import SOURCE_FILES, SftArtifactError, SftArtifactStore
from app.sft_authority import SftSourceAuthorityError, validate_source_authority
from app.sft_domain import (
    DATASET_ARTIFACT_CONTRACT_VERSION,
    EXAMPLE_CONTRACT_VERSION,
    NORMALIZATION_VERSION,
    SOURCE_ARTIFACT_CONTRACT_VERSION,
    SPLIT_VERSION,
    VALIDATION_RATIO,
    SftContractError,
    parse_source_bundle,
)

SFT_AUTHOR_ROLES = frozenset(
    {
        DepartmentRole.SYSTEM_ADMIN,
        DepartmentRole.DEPARTMENT_ADMIN,
        DepartmentRole.INSTRUCTOR,
    }
)
SFT_ADMIN_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
_CODE_REVISION = re.compile(r"^[0-9a-f]{40}$")


class SftImportConfigurationError(RuntimeError):
    """Safe setup failure for explicit local SFT source administration."""


@dataclass(frozen=True, slots=True)
class SftImportSettings:
    database_url: str
    data_dir: Path

    @classmethod
    def from_environment(cls) -> SftImportSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise SftImportConfigurationError(
                "DATABASE_URL must use the postgresql+psycopg driver."
            )
        if not raw_data_dir:
            raise SftImportConfigurationError("DEPTSLM_DATA_DIR is required.")
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise SftImportConfigurationError(
                "DEPTSLM_DATA_DIR must be an existing absolute directory."
            )
        try:
            with SftArtifactStore(data_dir):
                pass
        except SftArtifactError as error:
            raise SftImportConfigurationError("SFT dataset storage is unavailable.") from error
        return cls(database_url=database_url, data_dir=data_dir)


@dataclass(frozen=True, slots=True)
class SftSourceImportResult:
    source_bundle_id: UUID
    department_id: UUID
    example_count: int
    group_count: int
    applied: bool


def list_sft_sources(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    offset: int,
) -> tuple[SftSourceBundle, ...]:
    _page(limit, offset)
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            SFT_AUTHOR_ROLES,
            lock=False,
            audit_action="sft.source.list.authorization",
        )
        return tuple(
            session.scalars(
                select(SftSourceBundle)
                .where(SftSourceBundle.department_id == request_scope.department.value)
                .order_by(SftSourceBundle.created_at.desc(), SftSourceBundle.id)
                .offset(offset)
                .limit(limit)
            )
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_sft_source(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    source_bundle_id: UUID,
) -> SftSourceBundle:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            SFT_AUTHOR_ROLES,
            lock=False,
            audit_action="sft.source.read.authorization",
        )
        source = session.execute(
            select(SftSourceBundle).where(
                SftSourceBundle.id == source_bundle_id,
                SftSourceBundle.department_id == request_scope.department.value,
            )
        ).scalar_one_or_none()
        if source is None:
            raise ServiceError(404, "SFT source not found")
        return source
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def enqueue_sft_build(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    source_bundle_id: UUID,
    *,
    code_revision: str | None,
) -> SftDatasetBuild:
    if code_revision is None or _CODE_REVISION.fullmatch(code_revision) is None:
        raise ServiceError(503, "SFT dataset builder unavailable")
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            SFT_AUTHOR_ROLES,
            lock=True,
            audit_action="sft.build.enqueue.authorization",
        )
        source = session.execute(
            select(SftSourceBundle)
            .where(
                SftSourceBundle.id == source_bundle_id,
                SftSourceBundle.department_id == request_scope.department.value,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if source is None:
            raise ServiceError(404, "SFT source not found")
        if source.status != "active":
            raise ServiceError(409, "SFT source is unavailable")
        build = SftDatasetBuild(
            id=uuid4(),
            department_id=request_scope.department.value,
            source_bundle_id=source.id,
            requested_by_user_id=authorization.identity.id,
            status="queued",
            review_status="not_ready",
            attempt_number=1,
            code_revision=code_revision,
            artifact_contract_version=DATASET_ARTIFACT_CONTRACT_VERSION,
            example_contract_version=EXAMPLE_CONTRACT_VERSION,
            normalization_version=NORMALIZATION_VERSION,
            split_version=SPLIT_VERSION,
            validation_ratio=VALIDATION_RATIO,
            source_example_count=source.example_count,
            source_group_count=source.group_count,
            source_reference_count=source.source_reference_count,
        )
        session.add(build)
        session.flush()
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="sft.build.enqueue",
            resource_type="sft_dataset_build",
            resource_id=build.id,
        )
        session.flush()
        return build
    except ServiceError:
        raise
    except IntegrityError as error:
        raise ServiceError(409, "SFT dataset build conflict") from error
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def list_sft_builds(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    *,
    limit: int,
    offset: int,
) -> tuple[SftDatasetBuild, ...]:
    _page(limit, offset)
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            SFT_AUTHOR_ROLES,
            lock=False,
            audit_action="sft.build.list.authorization",
        )
        return tuple(
            session.scalars(
                select(SftDatasetBuild)
                .where(SftDatasetBuild.department_id == request_scope.department.value)
                .order_by(SftDatasetBuild.created_at.desc(), SftDatasetBuild.id)
                .offset(offset)
                .limit(limit)
            )
        )
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def read_sft_build(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    build_id: UUID,
) -> SftDatasetBuild:
    try:
        authorize_transaction(
            session,
            principal,
            request_scope,
            SFT_AUTHOR_ROLES,
            lock=False,
            audit_action="sft.build.read.authorization",
        )
        build = _scoped_build(session, request_scope.department, build_id)
        if build is None:
            raise ServiceError(404, "SFT dataset build not found")
        return build
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def cancel_sft_build(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    build_id: UUID,
    *,
    expected_version: int,
) -> SftDatasetBuild:
    _version(expected_version)
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            SFT_AUTHOR_ROLES,
            lock=True,
            audit_action="sft.build.cancel.authorization",
        )
        build = _scoped_build(session, request_scope.department, build_id, lock=True)
        if build is None:
            raise ServiceError(404, "SFT dataset build not found")
        if build.version != expected_version:
            raise ServiceError(409, "SFT dataset build version conflict")
        if build.status not in {"queued", "running"}:
            raise ServiceError(409, "SFT dataset build is already terminal")
        now = _clock(session)
        build.cancellation_requested_at = now
        if build.status == "queued":
            build.status = "cancelled"
            build.error_code = "cancelled"
            build.finished_at = now
        build.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action="sft.build.cancel",
            resource_type="sft_dataset_build",
            resource_id=build.id,
        )
        session.flush()
        return build
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def review_sft_build(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    build_id: UUID,
    *,
    action: str,
    expected_version: int,
) -> SftDatasetBuild:
    statuses = {"approve": "approved", "reject": "rejected", "archive": "archived"}
    review_status = statuses.get(action)
    if review_status is None:
        raise ServiceError(422, "Invalid SFT dataset review")
    _version(expected_version)
    try:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            SFT_ADMIN_ROLES,
            lock=True,
            audit_action="sft.build.review.authorization",
        )
        pending_purge = (
            select(SftArtifactReconciliationOperationItem.id)
            .where(
                SftArtifactReconciliationOperationItem.department_id
                == request_scope.department.value,
                SftArtifactReconciliationOperationItem.resource_type == "dataset_final",
                SftArtifactReconciliationOperationItem.resource_id == build_id,
                SftArtifactReconciliationOperationItem.status == "registered",
            )
            .exists()
        )
        build = session.execute(
            select(SftDatasetBuild)
            .where(
                SftDatasetBuild.id == build_id,
                SftDatasetBuild.department_id == request_scope.department.value,
                ~pending_purge,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if build is None:
            raise ServiceError(409, "SFT dataset build review conflict")
        if build.version != expected_version:
            raise ServiceError(409, "SFT dataset build version conflict")
        if build.status != "succeeded":
            raise ServiceError(409, "SFT dataset build is not ready for review")
        if action in {"approve", "reject"} and build.review_status != "pending":
            raise ServiceError(409, "SFT dataset build review conflict")
        if action == "archive" and build.review_status not in {"approved", "rejected"}:
            raise ServiceError(409, "SFT dataset build review conflict")
        now = _clock(session)
        build.review_status = review_status
        build.reviewed_by_user_id = authorization.identity.id
        build.reviewed_at = now
        if action == "archive":
            build.archived_at = now
        build.version += 1
        append_mutation_audit(
            session,
            actor=authorization.identity,
            actor_subject=principal.subject,
            request_scope=request_scope,
            action=f"sft.build.review.{action}",
            resource_type="sft_dataset_build",
            resource_id=build.id,
        )
        session.flush()
        return build
    except ServiceError:
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def import_sft_source(
    settings: SftImportSettings,
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    source_directory: Path,
    apply: bool,
) -> SftSourceImportResult:
    """Durably register, stage, publish, and commit a verified source bundle.

    Parsing and every filesystem operation are deliberately outside database
    transactions.  Each crash boundary has a committed import attempt that
    reconciliation can inspect without accepting unknown external state.
    """

    _validate_source_directory(source_directory, settings.data_dir)
    source_directory = Path(os.path.abspath(os.fspath(source_directory)))
    manifest_raw, examples_raw = _read_external_source(source_directory)
    parsed = parse_source_bundle(manifest_raw, examples_raw)
    if parsed.department_id != department_id:
        raise SftImportConfigurationError("SFT source department does not match --department-id.")
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    source_chunk_ids = {
        chunk_id for example in parsed.examples for chunk_id in example.source_chunk_ids
    }
    engine = None
    try:
        from app.database import create_database_engine, create_session_factory

        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        principal = AuthenticatedPrincipal(subject=actor_subject, issuer=actor_issuer)
        scope = DepartmentRequestScope(DepartmentScope(department_id))
        with factory.begin() as session:
            authorization = authorize_transaction(
                session,
                principal,
                scope,
                SFT_AUTHOR_ROLES,
                lock=True,
                audit_action="sft.source.import.authorization",
            )
            existing = _scoped_source(session, scope.department, parsed.source_bundle_id, lock=True)
            if existing is not None:
                if (
                    existing.manifest_sha256 == manifest_sha256
                    and existing.examples_sha256 == parsed.examples_sha256
                ):
                    return SftSourceImportResult(
                        existing.id,
                        department_id,
                        existing.example_count,
                        existing.group_count,
                        False,
                    )
                raise SftImportConfigurationError("SFT source bundle identifier already exists.")
            authority = validate_source_authority(session, department_id, source_chunk_ids)
            if not apply:
                return SftSourceImportResult(
                    parsed.source_bundle_id,
                    department_id,
                    len(parsed.examples),
                    parsed.group_count,
                    False,
                )
            attempt = session.execute(
                select(SftSourceImportAttempt)
                .where(
                    SftSourceImportAttempt.department_id == department_id,
                    SftSourceImportAttempt.import_attempt_id == parsed.import_attempt_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                attempt = SftSourceImportAttempt(
                    id=uuid4(),
                    department_id=department_id,
                    source_bundle_id=parsed.source_bundle_id,
                    import_attempt_id=parsed.import_attempt_id,
                    stage_id=parsed.stage_id,
                    imported_by_user_id=authorization.identity.id,
                    status="registered",
                    manifest_sha256=manifest_sha256,
                    examples_sha256=parsed.examples_sha256,
                    authority_snapshot_sha256=authority.fingerprint,
                    artifact_manifest=parsed.manifest,
                    examples_byte_size=parsed.examples_byte_size,
                )
                session.add(attempt)
                session.flush()
            elif (
                attempt.source_bundle_id != parsed.source_bundle_id
                or attempt.manifest_sha256 != manifest_sha256
                or attempt.examples_sha256 != parsed.examples_sha256
                or attempt.authority_snapshot_sha256 != authority.fingerprint
                or attempt.status not in {"registered", "staged", "published"}
            ):
                raise SftImportConfigurationError("SFT source import is unavailable.")
            attempt_id = attempt.id
            initial_status = attempt.status

        expected_manifest = parsed.manifest
        with SftArtifactStore(settings.data_dir) as store:
            verification = None
            retained = None
            try:
                if initial_status == "registered":
                    try:
                        staged = store.stage_source(
                            scope.department,
                            parsed.source_bundle_id,
                            parsed.import_attempt_id,
                            manifest=manifest_raw,
                            examples=examples_raw,
                        )
                    except SftArtifactError:
                        try:
                            staged = store.open_source_stage(
                                scope.department, parsed.source_bundle_id, parsed.import_attempt_id
                            )
                        except SftArtifactError:
                            verification = store.open_retained_final(
                                scope.department,
                                parsed.source_bundle_id,
                                category="source",
                                attempt_id=parsed.import_attempt_id,
                                allowlist=frozenset(SOURCE_FILES),
                                expected=expected_manifest,
                            )
                            _mark_attempt_published(
                                factory,
                                principal,
                                scope,
                                attempt_id,
                                expected_status="registered",
                                include_staged_transition=True,
                            )
                            initial_status = "published"
                        else:
                            _mark_attempt_staged(factory, principal, scope, attempt_id)
                            published = store.publish(
                                staged,
                                allowlist=frozenset(SOURCE_FILES),
                                expected=expected_manifest,
                                retain=True,
                            )
                            retained = published
                            verification = store.verify_retained_final(
                                published,
                                allowlist=frozenset(SOURCE_FILES),
                                expected=expected_manifest,
                            )
                            retained = None
                            _mark_attempt_published(
                                factory, principal, scope, attempt_id, expected_status="staged"
                            )
                            initial_status = "published"
                    else:
                        _mark_attempt_staged(factory, principal, scope, attempt_id)
                        published = store.publish(
                            staged,
                            allowlist=frozenset(SOURCE_FILES),
                            expected=expected_manifest,
                            retain=True,
                        )
                        retained = published
                        verification = store.verify_retained_final(
                            published,
                            allowlist=frozenset(SOURCE_FILES),
                            expected=expected_manifest,
                        )
                        retained = None
                        _mark_attempt_published(
                            factory, principal, scope, attempt_id, expected_status="staged"
                        )
                        initial_status = "published"
                elif initial_status == "staged":
                    try:
                        staged = store.open_source_stage(
                            scope.department, parsed.source_bundle_id, parsed.import_attempt_id
                        )
                    except SftArtifactError:
                        verification = store.open_retained_final(
                            scope.department,
                            parsed.source_bundle_id,
                            category="source",
                            attempt_id=parsed.import_attempt_id,
                            allowlist=frozenset(SOURCE_FILES),
                            expected=expected_manifest,
                        )
                    else:
                        published = store.publish(
                            staged,
                            allowlist=frozenset(SOURCE_FILES),
                            expected=expected_manifest,
                            retain=True,
                        )
                        retained = published
                        verification = store.verify_retained_final(
                            published,
                            allowlist=frozenset(SOURCE_FILES),
                            expected=expected_manifest,
                        )
                        retained = None
                    _mark_attempt_published(
                        factory, principal, scope, attempt_id, expected_status="staged"
                    )
                    initial_status = "published"
                elif initial_status == "published":
                    verification = store.open_retained_final(
                        scope.department,
                        parsed.source_bundle_id,
                        category="source",
                        attempt_id=parsed.import_attempt_id,
                        allowlist=frozenset(SOURCE_FILES),
                        expected=expected_manifest,
                    )
                if verification is None or initial_status != "published":
                    raise SftImportConfigurationError("SFT source import is unavailable.")
                with factory.begin() as session:
                    authorization = _reauthorize_attempt(
                        session, principal, scope, attempt_id, "published"
                    )
                    attempt = session.get(SftSourceImportAttempt, attempt_id, with_for_update=True)
                    if attempt is None or attempt.status != "published":
                        raise SftImportConfigurationError("SFT source import is unavailable.")
                    validate_source_authority(
                        session,
                        department_id,
                        source_chunk_ids,
                        expected_fingerprint=attempt.authority_snapshot_sha256,
                        lock=True,
                    )
                    verification.recheck_identity()
                    source = SftSourceBundle(
                        id=parsed.source_bundle_id,
                        department_id=department_id,
                        imported_by_user_id=authorization.identity.id,
                        status="active",
                        artifact_contract_version=SOURCE_ARTIFACT_CONTRACT_VERSION,
                        normalization_version=NORMALIZATION_VERSION,
                        example_contract_version=EXAMPLE_CONTRACT_VERSION,
                        example_count=len(parsed.examples),
                        group_count=parsed.group_count,
                        source_reference_count=parsed.source_reference_count,
                        manifest_sha256=manifest_sha256,
                        examples_sha256=parsed.examples_sha256,
                        authority_snapshot_sha256=attempt.authority_snapshot_sha256,
                        examples_byte_size=parsed.examples_byte_size,
                    )
                    session.add(source)
                    attempt.status = "committed"
                    attempt.committed_at = _clock(session)
                    attempt.version += 1
                    append_mutation_audit(
                        session,
                        actor=authorization.identity,
                        actor_subject=principal.subject,
                        request_scope=scope,
                        action="sft.source.import",
                        resource_type="sft_source_bundle",
                        resource_id=source.id,
                    )
                    session.flush()
                    return SftSourceImportResult(
                        source.id, department_id, source.example_count, source.group_count, True
                    )
            finally:
                if verification is not None:
                    verification.close()
                elif retained is not None:
                    retained.close()
    except (SftContractError, SftArtifactError, SftSourceAuthorityError) as error:
        raise SftImportConfigurationError("SFT source import failed.") from error
    except ServiceError as error:
        raise SftImportConfigurationError(error.detail) from error
    except SQLAlchemyError as error:
        raise SftImportConfigurationError("SFT source import failed.") from error
    finally:
        if engine is not None:
            engine.dispose()


def _read_external_source(source_directory: Path) -> tuple[bytes, bytes]:
    """Read only the two exact regular source files without symlink traversal."""

    try:
        directory_fd = _open_directory_chain(source_directory)
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_nlink != 2
            or directory_stat.st_uid != os.geteuid()
            or directory_stat.st_mode & 0o022
        ):
            raise SftImportConfigurationError("SFT source directory is unsafe.")
        names = set(os.listdir(directory_fd))
        if names != {"manifest.json", "examples.jsonl"}:
            raise SftImportConfigurationError("SFT source directory has unexpected entries.")
        values: list[bytes] = []
        entries: dict[str, os.stat_result] = {}
        for name, maximum in (("manifest.json", 128 * 1024), ("examples.jsonl", 512 * 1024 * 1024)):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode & 0o022
                    or metadata.st_size > maximum
                ):
                    raise SftImportConfigurationError("SFT source file is unsafe.")
                chunks = bytearray()
                while True:
                    part = os.read(fd, min(1024 * 1024, maximum - len(chunks) + 1))
                    if not part:
                        break
                    chunks.extend(part)
                    if len(chunks) > maximum:
                        raise SftImportConfigurationError("SFT source is too large.")
                if not _same_stable_file(metadata, os.fstat(fd)):
                    raise SftImportConfigurationError("SFT source changed while reading.")
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if not _same_stable_file(metadata, current):
                    raise SftImportConfigurationError("SFT source changed while reading.")
                entries[name] = metadata
                values.append(bytes(chunks))
            finally:
                os.close(fd)
        if set(os.listdir(directory_fd)) != {"manifest.json", "examples.jsonl"}:
            raise SftImportConfigurationError("SFT source changed while reading.")
        if not _same_stable_directory(directory_stat, os.fstat(directory_fd)) or set(entries) != {
            "manifest.json",
            "examples.jsonl",
        }:
            raise SftImportConfigurationError("SFT source changed while reading.")
        return values[0], values[1]
    except OSError as error:
        raise SftImportConfigurationError("SFT source directory is unavailable.") from error
    finally:
        try:
            os.close(directory_fd)
        except (OSError, UnboundLocalError):
            pass


def _scoped_source(
    session: Session, scope: DepartmentScope, source_id: UUID, *, lock: bool = False
) -> SftSourceBundle | None:
    statement = select(SftSourceBundle).where(
        SftSourceBundle.id == source_id, SftSourceBundle.department_id == scope.value
    )
    return session.execute(statement.with_for_update() if lock else statement).scalar_one_or_none()


def _reauthorize_attempt(
    session: Session,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    attempt_id: UUID,
    status: str,
):
    authorization = authorize_transaction(
        session,
        principal,
        request_scope,
        SFT_AUTHOR_ROLES,
        lock=True,
        audit_action="sft.source.import.authorization",
    )
    attempt = session.execute(
        select(SftSourceImportAttempt)
        .where(
            SftSourceImportAttempt.id == attempt_id,
            SftSourceImportAttempt.department_id == request_scope.department.value,
            SftSourceImportAttempt.status == status,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if attempt is None:
        raise SftImportConfigurationError("SFT source import is unavailable.")
    return authorization


def _mark_attempt_staged(
    factory,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    attempt_id: UUID,
) -> None:
    with factory.begin() as session:
        _reauthorize_attempt(session, principal, request_scope, attempt_id, "registered")
        attempt = session.get(SftSourceImportAttempt, attempt_id, with_for_update=True)
        if attempt is None:
            raise SftImportConfigurationError("SFT source import is unavailable.")
        attempt.status = "staged"
        attempt.staged_at = _clock(session)
        attempt.version += 1


def _mark_attempt_published(
    factory,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    attempt_id: UUID,
    *,
    expected_status: str,
    include_staged_transition: bool = False,
) -> None:
    with factory.begin() as session:
        _reauthorize_attempt(session, principal, request_scope, attempt_id, expected_status)
        attempt = session.get(SftSourceImportAttempt, attempt_id, with_for_update=True)
        if attempt is None:
            raise SftImportConfigurationError("SFT source import is unavailable.")
        now = _clock(session)
        if include_staged_transition:
            attempt.staged_at = now
        attempt.status = "published"
        attempt.published_at = now
        attempt.version += 1


def _validate_source_directory(source_directory: Path, data_dir: Path) -> None:
    if not isinstance(source_directory, Path) or not source_directory.is_absolute():
        raise SftImportConfigurationError("--source-dir must be an absolute directory.")
    raw_source = os.path.abspath(os.fspath(source_directory))
    repository_root = os.path.abspath(os.fspath(Path(__file__).parents[3]))
    runtime_root = os.path.abspath(os.fspath(data_dir))
    try:
        if (
            os.path.commonpath((raw_source, repository_root)) == repository_root
            or os.path.commonpath((raw_source, runtime_root)) == runtime_root
        ):
            raise SftImportConfigurationError(
                "--source-dir must be outside repository and runtime storage."
            )
    except OSError as error:
        raise SftImportConfigurationError("SFT source directory is unavailable.") from error


def _open_directory_chain(path: Path) -> int:
    """Open every source path component without following a symlink."""

    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise


def _same_stable_file(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare read integrity fields while deliberately ignoring atime."""

    return (
        stat.S_ISREG(first.st_mode)
        and stat.S_ISREG(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_uid == second.st_uid
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_nlink == second.st_nlink
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _same_stable_directory(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(first.st_mode)
        and stat.S_ISDIR(second.st_mode)
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_uid == second.st_uid
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_nlink == second.st_nlink
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _scoped_build(
    session: Session, scope: DepartmentScope, build_id: UUID, *, lock: bool = False
) -> SftDatasetBuild | None:
    statement = select(SftDatasetBuild).where(
        SftDatasetBuild.id == build_id, SftDatasetBuild.department_id == scope.value
    )
    return session.execute(statement.with_for_update() if lock else statement).scalar_one_or_none()


def _clock(session: Session) -> datetime:
    from sqlalchemy import func

    return session.scalar(select(func.clock_timestamp()))


def _page(limit: object, offset: object) -> None:
    if (
        isinstance(limit, bool)
        or isinstance(offset, bool)
        or not isinstance(limit, int)
        or not isinstance(offset, int)
        or not 1 <= limit <= 100
        or offset < 0
    ):
        raise ServiceError(422, "Invalid pagination")


def _version(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ServiceError(409, "SFT dataset build version conflict")
