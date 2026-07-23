"""Reviewed local-only administrative bootstrap commands."""

from __future__ import annotations

import argparse
import os
import re
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.database import create_database_engine, create_session_factory
from app.feedback_purge import (
    FeedbackPurgeConfigurationError,
    FeedbackPurgeSettings,
    purge_rag_feedback,
)
from app.models import Department, Membership, PersistentAuditEvent, UserIdentity
from app.services import ServiceError
from app.settings import ALLOWED_HS256_ENVIRONMENTS, ConfigurationError, Settings

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
        FeedbackPurgeConfigurationError,
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
