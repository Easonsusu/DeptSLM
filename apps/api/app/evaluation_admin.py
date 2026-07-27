"""Explicit Phase 9 suite import and archive commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from app.database import create_database_engine, create_session_factory
from app.evaluation_domain import EvaluationContractError
from app.evaluation_suites import (
    ArtifactReconcileSettings,
    SuiteArchiveSettings,
    SuiteImportConfigurationError,
    SuiteImportSettings,
    archive_suite,
    import_suite,
    reconcile_artifacts,
)
from app.services import ServiceError


def _nonzero_uuid(raw: str) -> UUID:
    try:
        value = UUID(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a UUID") from error
    if value.int == 0:
        raise argparse.ArgumentTypeError("value must be non-zero")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation_admin")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import-suite")
    importer.add_argument("--department-id", required=True, type=_nonzero_uuid)
    importer.add_argument("--actor-issuer", required=True)
    importer.add_argument("--actor-subject", required=True)
    importer.add_argument("--source-directory", required=True, type=Path)
    importer.add_argument("--apply", action="store_true")
    archive = commands.add_parser("archive-suite")
    archive.add_argument("--department-id", required=True, type=_nonzero_uuid)
    archive.add_argument("--suite-id", required=True, type=_nonzero_uuid)
    archive.add_argument("--actor-issuer", required=True)
    archive.add_argument("--actor-subject", required=True)
    archive.add_argument("--apply", action="store_true")
    reconcile = commands.add_parser("reconcile-artifacts")
    reconcile.add_argument("--department-id", required=True, type=_nonzero_uuid)
    reconcile.add_argument("--actor-issuer", required=True)
    reconcile.add_argument("--actor-subject", required=True)
    reconcile.add_argument("--limit", required=True, type=int)
    reconcile.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "import-suite":
            settings = SuiteImportSettings.from_environment()
            result = import_suite(
                settings,
                department_id=args.department_id,
                actor_issuer=args.actor_issuer,
                actor_subject=args.actor_subject,
                source_directory=args.source_directory,
                apply=args.apply,
            )
            verb = "Imported" if result.applied else "Validated"
            print(
                f"{verb} evaluation suite {result.suite_id}: "
                f"{result.case_count} cases "
                f"({result.answered_case_count} answered, "
                f"{result.insufficient_case_count} insufficient)."
            )
            return 0
        if args.command == "reconcile-artifacts":
            settings = ArtifactReconcileSettings.from_environment()
            engine = create_database_engine(settings.database_url)
            factory = create_session_factory(engine)
            try:
                items = reconcile_artifacts(
                    factory,
                    data_dir=settings.data_dir,
                    department_id=args.department_id,
                    actor_issuer=args.actor_issuer,
                    actor_subject=args.actor_subject,
                    limit=args.limit,
                    apply=args.apply,
                )
            finally:
                engine.dispose()
            print(
                json.dumps(
                    {
                        "apply": args.apply,
                        "count": len(items),
                        "items": [
                            {
                                "resource_type": item.resource_type,
                                "resource_id": str(item.resource_id),
                                "status": item.status,
                                "created_at": item.created_at.isoformat(),
                                "staging_present": item.staging_present,
                                "staging_owned": item.staging_owned,
                                "final_present": item.final_present,
                                "final_owned": item.final_owned,
                                "applied": item.applied,
                                "reconciliation_status": item.reconciliation_status,
                                "blocked_reason_code": item.blocked_reason_code,
                            }
                            for item in items
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        settings = SuiteArchiveSettings.from_environment()
        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        try:
            applied = archive_suite(
                factory,
                department_id=args.department_id,
                suite_id=args.suite_id,
                actor_issuer=args.actor_issuer,
                actor_subject=args.actor_subject,
                apply=args.apply,
            )
        finally:
            engine.dispose()
        verb = "Archived" if applied else "Validated archive for"
        print(f"{verb} evaluation suite {args.suite_id}.")
        return 0
    except (
        EvaluationContractError,
        ServiceError,
        SuiteImportConfigurationError,
    ) as error:
        print(f"Evaluation administration failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
