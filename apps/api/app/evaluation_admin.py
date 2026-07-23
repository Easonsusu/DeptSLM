"""Explicit Phase 9 suite import and archive commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from app.database import create_database_engine, create_session_factory
from app.evaluation_domain import EvaluationContractError
from app.evaluation_suites import (
    SuiteImportConfigurationError,
    SuiteImportSettings,
    archive_suite,
    import_suite,
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = SuiteImportSettings.from_environment()
        if args.command == "import-suite":
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
        if not args.apply:
            print(f"Would archive evaluation suite {args.suite_id}.")
            return 0
        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        try:
            archive_suite(
                factory,
                department_id=args.department_id,
                suite_id=args.suite_id,
                actor_issuer=args.actor_issuer,
                actor_subject=args.actor_subject,
            )
        finally:
            engine.dispose()
        print(f"Archived evaluation suite {args.suite_id}.")
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
