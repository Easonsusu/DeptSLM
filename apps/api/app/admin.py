"""Reviewed local-only administrative bootstrap commands."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.adapter_registry_services import enqueue_adapter_registry
from app.adapter_source_services import (
    AdapterSourceImportConfigurationError,
    AdapterSourceImportSettings,
    import_adapter_source,
)
from app.auth import AuthenticatedPrincipal
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.feedback_purge import (
    FeedbackPurgeConfigurationError,
    FeedbackPurgeSettings,
    purge_rag_feedback,
)
from app.models import Department, Membership, PersistentAuditEvent, UserIdentity
from app.services import ServiceError
from app.settings import ALLOWED_HS256_ENVIRONMENTS, ConfigurationError, Settings
from app.sft_maintenance import (
    SftMaintenanceConfigurationError,
    SftMaintenanceSettings,
    archive_sft_source,
    purge_sft_artifacts,
    reconcile_sft_artifacts,
)
from app.sft_services import (
    SftImportConfigurationError,
    SftImportSettings,
    import_sft_source,
)
from app.training_job_maintenance import (
    TrainingJobMaintenanceConfigurationError,
    TrainingJobMaintenanceSettings,
    archive_training_job,
    purge_training_job_artifacts,
    reconcile_training_job_artifacts,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BootstrapError(RuntimeError):
    pass


def bootstrap_department(
    settings: Settings, *, slug: str, display_name: str, admin_issuer: str, admin_subject: str
) -> tuple[Department, Membership]:
    """Create initial department authority atomically without global privileges."""

    if not 2 <= len(slug) <= 63 or SLUG_PATTERN.fullmatch(slug) is None:
        raise BootstrapError("Department slug is invalid.")
    display_name = display_name.strip()
    if not display_name or len(display_name) > 200:
        raise BootstrapError("Department display name is invalid.")
    if not admin_issuer or not admin_subject:
        raise BootstrapError("Admin issuer and subject must be non-empty.")

    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            existing_department = session.execute(
                select(Department).where(Department.slug == slug)
            ).scalar_one_or_none()
            if existing_department:
                raise BootstrapError("Department slug already exists.")
            identity = session.execute(
                select(UserIdentity)
                .where(UserIdentity.issuer == admin_issuer, UserIdentity.subject == admin_subject)
                .with_for_update()
            ).scalar_one_or_none()
            if identity is None:
                identity = UserIdentity(issuer=admin_issuer, subject=admin_subject, status="active")
                session.add(identity)
                session.flush()
            elif identity.status != "active":
                raise BootstrapError("Existing identity is not active.")
            department = Department(slug=slug, display_name=display_name, status="active")
            session.add(department)
            session.flush()
            membership = Membership(
                user_id=identity.id,
                department_id=department.id,
                role="department_admin",
                status="active",
                created_by_user_id=identity.id,
            )
            session.add(membership)
            session.flush()
            session.add(
                PersistentAuditEvent(
                    actor_subject=admin_subject,
                    actor_user_id=identity.id,
                    department_id=department.id,
                    action="department.bootstrap",
                    resource_type="department",
                    resource_id=str(department.id),
                    result="allowed",
                    reason_code="local_bootstrap",
                )
            )
        return department, membership
    except IntegrityError as error:
        raise BootstrapError("Bootstrap conflicts with existing state.") from error
    except SQLAlchemyError as error:
        raise BootstrapError("Database operation failed.") from error
    finally:
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-department")
    bootstrap.add_argument("--slug", required=True)
    bootstrap.add_argument("--display-name", required=True)
    bootstrap.add_argument("--admin-issuer", required=True)
    bootstrap.add_argument("--admin-subject", required=True)
    purge = commands.add_parser("purge-rag-feedback")
    purge.add_argument("--department-id", required=True, type=_nonzero_uuid)
    purge.add_argument("--actor-issuer", required=True)
    purge.add_argument("--actor-subject", required=True)
    purge.add_argument("--limit", type=_purge_limit, default=500)
    purge.add_argument("--apply", action="store_true")
    sft_import = commands.add_parser("import-sft-source")
    sft_import.add_argument("--department-id", required=True, type=_nonzero_uuid)
    sft_import.add_argument("--actor-issuer", required=True)
    sft_import.add_argument("--actor-subject", required=True)
    sft_import.add_argument("--source-dir", required=True)
    sft_import.add_argument("--apply", action="store_true")
    adapter_import = commands.add_parser("import-adapter-source")
    adapter_import.add_argument("--department-id", required=True, type=_nonzero_uuid)
    adapter_import.add_argument("--actor-issuer", required=True)
    adapter_import.add_argument("--actor-subject", required=True)
    adapter_import.add_argument("--adapter-config", required=True, type=Path)
    adapter_import.add_argument("--adapter-model", required=True, type=Path)
    adapter_import.add_argument("--apply", action="store_true")
    adapter_registry = commands.add_parser("enqueue-adapter-registry")
    adapter_registry.add_argument("--department-id", required=True, type=_nonzero_uuid)
    adapter_registry.add_argument("--actor-issuer", required=True)
    adapter_registry.add_argument("--actor-subject", required=True)
    adapter_registry.add_argument("--source-bundle-id", required=True, type=_nonzero_uuid)
    adapter_registry.add_argument("--training-job-id", required=True, type=_nonzero_uuid)
    adapter_registry.add_argument("--expected-source-version", required=True, type=int)
    adapter_registry.add_argument("--expected-training-job-version", required=True, type=int)
    adapter_registry.add_argument("--confirm-declared-training-association", action="store_true")
    adapter_registry.add_argument("--apply", action="store_true")
    sft_archive = commands.add_parser("archive-sft-source")
    sft_archive.add_argument("--department-id", required=True, type=_nonzero_uuid)
    sft_archive.add_argument("--source-bundle-id", required=True, type=_nonzero_uuid)
    sft_archive.add_argument("--actor-issuer", required=True)
    sft_archive.add_argument("--actor-subject", required=True)
    sft_archive.add_argument("--apply", action="store_true")
    sft_reconcile = commands.add_parser("reconcile-sft-artifacts")
    sft_reconcile.add_argument("--department-id", required=True, type=_nonzero_uuid)
    sft_reconcile.add_argument("--actor-issuer", required=True)
    sft_reconcile.add_argument("--actor-subject", required=True)
    sft_reconcile.add_argument("--limit", type=_purge_limit, default=500)
    sft_reconcile.add_argument("--apply", action="store_true")
    sft_purge = commands.add_parser("purge-sft-artifacts")
    sft_purge.add_argument("--department-id", required=True, type=_nonzero_uuid)
    sft_purge.add_argument("--actor-issuer", required=True)
    sft_purge.add_argument("--actor-subject", required=True)
    sft_purge.add_argument("--limit", type=_purge_limit, default=500)
    sft_purge.add_argument("--apply", action="store_true")
    training_archive = commands.add_parser("archive-training-job")
    training_archive.add_argument("--department-id", required=True, type=_nonzero_uuid)
    training_archive.add_argument("--training-job-id", required=True, type=_nonzero_uuid)
    training_archive.add_argument("--actor-issuer", required=True)
    training_archive.add_argument("--actor-subject", required=True)
    training_archive.add_argument("--apply", action="store_true")
    training_reconcile = commands.add_parser("reconcile-training-job-artifacts")
    training_reconcile.add_argument("--department-id", required=True, type=_nonzero_uuid)
    training_reconcile.add_argument("--actor-issuer", required=True)
    training_reconcile.add_argument("--actor-subject", required=True)
    training_reconcile.add_argument("--limit", type=_purge_limit, default=500)
    training_reconcile.add_argument("--apply", action="store_true")
    training_purge = commands.add_parser("purge-training-job-artifacts")
    training_purge.add_argument("--department-id", required=True, type=_nonzero_uuid)
    training_purge.add_argument("--actor-issuer", required=True)
    training_purge.add_argument("--actor-subject", required=True)
    training_purge.add_argument("--limit", type=_purge_limit, default=500)
    training_purge.add_argument("--apply", action="store_true")
    return parser


def _nonzero_uuid(raw: str) -> UUID:
    try:
        value = UUID(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("department ID must be a UUID") from error
    if value.int == 0:
        raise argparse.ArgumentTypeError("department ID must be non-zero")
    return value


def _purge_limit(raw: str) -> int:
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise argparse.ArgumentTypeError("limit must be an ASCII integer from 1 through 1000")
    value = int(raw)
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError("limit must be an ASCII integer from 1 through 1000")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bootstrap-department":
            environment = os.getenv("ENVIRONMENT", "").strip()
            if environment not in ALLOWED_HS256_ENVIRONMENTS:
                print(
                    "Bootstrap is allowed only in an explicitly reviewed local environment.",
                    file=sys.stderr,
                )
                return 2
            settings = Settings.from_environment()
            department, _membership = bootstrap_department(
                settings,
                slug=args.slug,
                display_name=args.display_name,
                admin_issuer=args.admin_issuer,
                admin_subject=args.admin_subject,
            )
            print(f"Bootstrapped department {department.slug} ({department.id}).")
            return 0
        if args.command == "import-sft-source":
            result = import_sft_source(
                SftImportSettings.from_environment(),
                department_id=args.department_id,
                actor_issuer=args.actor_issuer,
                actor_subject=args.actor_subject,
                source_directory=Path(args.source_dir),
                apply=args.apply,
            )
            verb = "Imported" if result.applied else "Validated"
            print(
                f"{verb} SFT source {result.source_bundle_id}: "
                f"{result.example_count} examples in {result.group_count} groups."
            )
            return 0
        if args.command == "import-adapter-source":
            result = import_adapter_source(
                AdapterSourceImportSettings.from_environment(),
                department_id=args.department_id,
                actor_issuer=args.actor_issuer,
                actor_subject=args.actor_subject,
                adapter_config=args.adapter_config,
                adapter_model=args.adapter_model,
                apply=args.apply,
            )
            if result.applied:
                print(
                    f"Imported adapter source {result.source_bundle_id} "
                    f"for department {result.department_id}."
                )
                print("Status: committed")
            else:
                print("Adapter source validation succeeded.")
                print(f"Base model: {result.base_model_display_id}")
                print(f"Dtype: {result.tensor_dtype}")
                print(f"Tensor count: {result.tensor_count}")
                print(f"Aggregate tensor bytes: {result.tensor_payload_byte_size}")
            return 0
        if args.command == "enqueue-adapter-registry":
            database_url = os.getenv("DATABASE_URL", "").strip()
            code_revision = os.getenv("DEPTSLM_ADAPTER_REGISTRY_CODE_REVISION", "").strip()
            if not database_url:
                print("Database unavailable.", file=sys.stderr)
                return 1
            engine = create_database_engine(database_url)
            factory = create_session_factory(engine)
            try:
                principal = AuthenticatedPrincipal(
                    issuer=args.actor_issuer, subject=args.actor_subject
                )
                request_scope = DepartmentRequestScope(DepartmentScope(args.department_id))
                with factory.begin() as session:
                    result = enqueue_adapter_registry(
                        session,
                        principal,
                        request_scope,
                        source_bundle_id=args.source_bundle_id,
                        training_job_id=args.training_job_id,
                        expected_source_version=args.expected_source_version,
                        expected_training_job_version=args.expected_training_job_version,
                        confirm_declared_training_association=(
                            args.confirm_declared_training_association
                        ),
                        apply=args.apply,
                        code_revision=code_revision,
                    )
            finally:
                engine.dispose()
            if result.applied:
                print(f"Queued adapter registry publication {result.adapter_id}.")
            else:
                print(f"Eligible department: {result.department_id}")
                print(f"Profile: {result.profile_id}")
                print(f"Base model: {result.base_model_id}")
                print(f"Dtype: {result.tensor_dtype}")
                print(f"Tensor count: {result.tensor_count}")
                print(f"Aggregate tensor bytes: {result.tensor_payload_byte_size}")
            return 0
        if args.command in {"archive-sft-source", "reconcile-sft-artifacts", "purge-sft-artifacts"}:
            settings = SftMaintenanceSettings.from_environment()
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            try:
                if args.command == "archive-sft-source":
                    applied = archive_sft_source(
                        factory,
                        department_id=args.department_id,
                        source_bundle_id=args.source_bundle_id,
                        actor_issuer=args.actor_issuer,
                        actor_subject=args.actor_subject,
                        apply=args.apply,
                    )
                    print("Archived SFT source." if applied else "Validated SFT source archive.")
                    return 0
                if args.command == "reconcile-sft-artifacts":
                    result = reconcile_sft_artifacts(
                        factory,
                        data_dir=settings.data_dir,
                        department_id=args.department_id,
                        actor_issuer=args.actor_issuer,
                        actor_subject=args.actor_subject,
                        limit=args.limit,
                        apply=args.apply,
                    )
                else:
                    result = purge_sft_artifacts(
                        factory,
                        data_dir=settings.data_dir,
                        department_id=args.department_id,
                        actor_issuer=args.actor_issuer,
                        actor_subject=args.actor_subject,
                        retention_days=settings.retention_days,
                        limit=args.limit,
                        apply=args.apply,
                    )
            finally:
                engine.dispose()
            print(
                "Eligible: "
                f"{result.eligible_count}; applied: {result.applied_count}; "
                f"blocked: {result.blocked_count}."
            )
            return 0
        if args.command in {
            "archive-training-job",
            "reconcile-training-job-artifacts",
            "purge-training-job-artifacts",
        }:
            settings = TrainingJobMaintenanceSettings.from_environment()
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            try:
                if args.command == "archive-training-job":
                    applied = archive_training_job(
                        factory,
                        department_id=args.department_id,
                        training_job_id=args.training_job_id,
                        actor_issuer=args.actor_issuer,
                        actor_subject=args.actor_subject,
                        apply=args.apply,
                    )
                    print(
                        "Archived training job." if applied else "Validated training-job archive."
                    )
                    return 0
                if args.command == "reconcile-training-job-artifacts":
                    result = reconcile_training_job_artifacts(
                        factory,
                        data_dir=settings.data_dir,
                        department_id=args.department_id,
                        actor_issuer=args.actor_issuer,
                        actor_subject=args.actor_subject,
                        limit=args.limit,
                        apply=args.apply,
                    )
                else:
                    result = purge_training_job_artifacts(
                        factory,
                        data_dir=settings.data_dir,
                        department_id=args.department_id,
                        actor_issuer=args.actor_issuer,
                        actor_subject=args.actor_subject,
                        retention_days=settings.retention_days,
                        limit=args.limit,
                        apply=args.apply,
                    )
            finally:
                engine.dispose()
            print(
                "Eligible: "
                f"{result.eligible_count}; applied: {result.applied_count}; "
                f"blocked: {result.blocked_count}."
            )
            return 0
        settings = FeedbackPurgeSettings.from_environment()
        result = purge_rag_feedback(
            settings,
            department_id=args.department_id,
            actor_issuer=args.actor_issuer,
            actor_subject=args.actor_subject,
            limit=args.limit,
            apply=args.apply,
        )
    except (
        BootstrapError,
        ConfigurationError,
        AdapterSourceImportConfigurationError,
        FeedbackPurgeConfigurationError,
        SftImportConfigurationError,
        SftMaintenanceConfigurationError,
        TrainingJobMaintenanceConfigurationError,
        ServiceError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 1
    if result.applied:
        print(f"Purged feedback count: {result.purged_count}.")
    else:
        print(f"Department: {result.department_id}")
        print(f"Eligible count: {result.eligible_count}")
        print(f"Oldest expiry: {result.oldest_expires_at or 'none'}")
        print(f"Newest expiry: {result.newest_expires_at or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
