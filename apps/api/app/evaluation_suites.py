"""Strict external suite import, authority validation, and archival."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from deptslm_worker.artifact_reader import (
    ArtifactAuthorityIdentity,
    ArtifactError,
    ArtifactExpectation,
    Phase5ArtifactReader,
    verify_artifact_authority_identity,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.auth import AuthenticatedPrincipal, DepartmentRole
from app.authorization import DepartmentRequestScope, DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.evaluation_artifacts import (
    EvaluationArtifactStore,
    SuiteSourceReader,
    canonical_json_bytes,
    iter_source_cases,
)
from app.evaluation_domain import (
    ANSWER_NORMALIZATION_VERSION,
    ARTIFACT_CONTRACT_VERSION,
    GATE_POLICY_VERSION,
    MAX_ACCEPTED_ANSWER_CHARS,
    MAX_SUITE_CASES,
    METRIC_CONTRACT_VERSION,
    SUITE_CONTRACT_VERSION,
    EvaluationContractError,
    QualityGates,
    normalize_answer,
    parse_quality_gates,
)
from app.extraction_domain import CHUNKING_VERSION, NORMALIZATION_VERSION, PIPELINE_VERSION
from app.models import (
    Document,
    DocumentChunk,
    DocumentExtraction,
    DocumentVectorIndexing,
    EvaluationArtifactReconciliationOperation,
    EvaluationArtifactReconciliationOperationItem,
    EvaluationRun,
    EvaluationSuite,
    EvaluationSuiteImportAttempt,
)
from app.rag_domain import MAX_QUESTION_CHARS, normalize_question, validate_safe_text
from app.services import ServiceError, append_mutation_audit, authorize_transaction
from app.vector_index_domain import (
    EMBEDDING_DIMENSION,
    EMBEDDING_DISTANCE,
    EMBEDDING_MODEL_ID,
    EMBEDDING_MODEL_REVISION,
    EMBEDDING_PIPELINE_VERSION,
    QDRANT_COLLECTION,
    VECTOR_SCHEMA_VERSION,
)

EVALUATOR_ROLES = frozenset(
    {
        DepartmentRole.SYSTEM_ADMIN,
        DepartmentRole.DEPARTMENT_ADMIN,
        DepartmentRole.INSTRUCTOR,
    }
)
RECONCILER_ROLES = frozenset({DepartmentRole.SYSTEM_ADMIN, DepartmentRole.DEPARTMENT_ADMIN})
RECONCILIATION_MINIMUM_AGE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ParsedEvaluationCase:
    case_id: UUID
    expected_status: str
    question: str
    relevant_chunk_ids: tuple[UUID, ...]
    accepted_answers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SuiteImportResult:
    suite_id: UUID
    department_id: UUID
    case_count: int
    answered_case_count: int
    insufficient_case_count: int
    applied: bool


class SuiteImportConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GroundTruthArtifactValidation:
    expectation: ArtifactExpectation
    targets: dict[int, dict[str, object]]


@dataclass(frozen=True, slots=True)
class VerifiedGroundTruthArtifact:
    expectation: ArtifactExpectation
    identity: ArtifactAuthorityIdentity


@dataclass(frozen=True, slots=True)
class GroundTruthAuthoritySnapshot:
    sources: dict[UUID, dict[str, object]]
    artifacts: tuple[VerifiedGroundTruthArtifact, ...]


@dataclass(frozen=True, slots=True)
class SuiteImportSettings:
    database_url: str
    data_dir: Path
    repository_root: Path

    @classmethod
    def from_environment(cls) -> SuiteImportSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise SuiteImportConfigurationError(
                "DATABASE_URL must use the postgresql+psycopg driver."
            )
        if not raw_data_dir:
            raise SuiteImportConfigurationError("DEPTSLM_DATA_DIR is required.")
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise SuiteImportConfigurationError(
                "DEPTSLM_DATA_DIR must be an existing absolute directory."
            )
        repository_root = _repository_root(Path(__file__))
        if repository_root is None:
            raise SuiteImportConfigurationError("DeptSLM repository root is unavailable.")
        resolved = data_dir.resolve()
        if resolved == repository_root or resolved.is_relative_to(repository_root):
            raise SuiteImportConfigurationError("DEPTSLM_DATA_DIR must be outside the repository.")
        try:
            EvaluationArtifactStore(resolved)
        except EvaluationContractError as error:
            raise SuiteImportConfigurationError("Evaluation storage is unavailable.") from error
        return cls(database_url, resolved, repository_root)


@dataclass(frozen=True, slots=True)
class SuiteArchiveSettings:
    database_url: str

    @classmethod
    def from_environment(cls) -> SuiteArchiveSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise SuiteImportConfigurationError(
                "DATABASE_URL must use the postgresql+psycopg driver."
            )
        return cls(database_url)


@dataclass(frozen=True, slots=True)
class ArtifactReconcileSettings:
    database_url: str
    data_dir: Path

    @classmethod
    def from_environment(cls) -> ArtifactReconcileSettings:
        database_url = os.getenv("DATABASE_URL", "").strip()
        raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
        if not database_url.startswith("postgresql+psycopg://"):
            raise SuiteImportConfigurationError(
                "DATABASE_URL must use the postgresql+psycopg driver."
            )
        if not raw_data_dir:
            raise SuiteImportConfigurationError("DEPTSLM_DATA_DIR is required.")
        data_dir = Path(raw_data_dir).expanduser()
        if not data_dir.is_absolute() or not data_dir.is_dir():
            raise SuiteImportConfigurationError(
                "DEPTSLM_DATA_DIR must be an existing absolute directory."
            )
        return cls(database_url, data_dir.resolve())


@dataclass(frozen=True, slots=True)
class ArtifactReconcileItem:
    resource_type: str
    resource_id: UUID
    status: str
    created_at: object
    staging_present: bool
    staging_owned: bool
    final_present: bool
    final_owned: bool
    applied: bool
    reconciliation_status: str
    blocked_reason_code: str | None

    @property
    def owned(self) -> bool:
        """Compatibility view: at least one exact owned artifact is recoverable."""

        return self.staging_owned or self.final_owned


@dataclass(frozen=True, slots=True)
class _ArtifactReconcileCandidate:
    resource_type: str
    resource_id: UUID
    suite_id: UUID
    ownership_attempt_id: UUID
    stage_id: UUID
    attempt_number: int | None
    code_revision: str | None
    status: str
    created_at: object


def import_suite(
    settings: SuiteImportSettings,
    *,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    source_directory: Path,
    apply: bool,
) -> SuiteImportResult:
    if (
        not isinstance(department_id, UUID)
        or department_id.int == 0
        or not actor_issuer.strip()
        or not actor_subject.strip()
    ):
        raise EvaluationContractError()
    with SuiteSourceReader(source_directory, settings.repository_root) as source:
        gates = _suite_definition(source.read_definition())
        cases = tuple(_parse_case_values(source.iter_cases()))
    answered = sum(case.expected_status == "answered" for case in cases)
    insufficient = len(cases) - answered
    if answered == 0:
        raise EvaluationContractError()

    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    scope = DepartmentScope(department_id)
    request_scope = DepartmentRequestScope(scope)
    principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
    store = EvaluationArtifactStore(settings.data_dir)
    suite_id = uuid4()
    stage_id = uuid4()
    import_attempt_id = uuid4()
    staged = None
    try:
        with factory.begin() as session:
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                EVALUATOR_ROLES,
                lock=True,
                audit_action="evaluation.suite.import.authorization",
            )
            if apply:
                session.add(
                    EvaluationSuiteImportAttempt(
                        id=import_attempt_id,
                        department_id=department_id,
                        imported_by_user_id=authorization.identity.id,
                        suite_id=suite_id,
                        stage_id=stage_id,
                        status="registered",
                    )
                )
            snapshots, validations = _ground_truth_snapshot_metadata(session, scope, cases)
        verified_artifacts = _verify_ground_truth_artifacts(settings.data_dir, scope, validations)
        verified = GroundTruthAuthoritySnapshot(snapshots, verified_artifacts)
        canonical_lines = tuple(
            canonical_json_bytes(_canonical_case(case, snapshots)) + b"\n" for case in cases
        )
        manifest = _suite_manifest(
            department_id=department_id,
            suite_id=suite_id,
            import_attempt_id=import_attempt_id,
            stage_id=stage_id,
            gates=gates,
            case_count=len(cases),
            answered=answered,
            insufficient=insufficient,
        )
        staged = store.stage_suite(scope, suite_id, stage_id, manifest, canonical_lines)
        if not apply:
            store.remove_owned_suite_stage(scope, suite_id, stage_id, import_attempt_id)
            return SuiteImportResult(
                suite_id, department_id, len(cases), answered, insufficient, False
            )

        with factory.begin() as session:
            attempt = session.execute(
                select(EvaluationSuiteImportAttempt)
                .where(
                    EvaluationSuiteImportAttempt.id == import_attempt_id,
                    EvaluationSuiteImportAttempt.department_id == department_id,
                    EvaluationSuiteImportAttempt.status == "registered",
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                raise EvaluationContractError("artifact_reconciliation_failed")
            now = session.execute(select(_clock_timestamp())).scalar_one()
            attempt.status = "staged"
            attempt.artifact_manifest_sha256 = staged.manifest.sha256
            attempt.canonical_cases_sha256 = staged.payload.sha256
            attempt.canonical_cases_byte_size = staged.payload.byte_size
            attempt.staged_at = now
            attempt.version += 1

        try:
            current = capture_ground_truth_authority(
                factory,
                settings.data_dir,
                scope,
                cases,
            )
            if current != verified:
                raise EvaluationContractError("suite_source_stale")
            published_artifact = store.publish(staged, frozenset({"manifest.json", "cases.jsonl"}))
            published_artifact = store.verify_published_suite(
                scope,
                suite_id,
                expected_manifest=manifest,
                expected=staged,
            )
            with factory.begin() as session:
                attempt = session.execute(
                    select(EvaluationSuiteImportAttempt)
                    .where(
                        EvaluationSuiteImportAttempt.id == import_attempt_id,
                        EvaluationSuiteImportAttempt.department_id == department_id,
                        EvaluationSuiteImportAttempt.status == "staged",
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if attempt is None:
                    raise EvaluationContractError("artifact_reconciliation_failed")
                attempt.status = "published"
                attempt.published_at = session.execute(select(_clock_timestamp())).scalar_one()
                attempt.version += 1
            with factory.begin() as session:
                authorization = authorize_transaction(
                    session,
                    principal,
                    request_scope,
                    EVALUATOR_ROLES,
                    lock=True,
                    audit_action="evaluation.suite.import.authorization",
                )
                attempt = session.execute(
                    select(EvaluationSuiteImportAttempt)
                    .where(
                        EvaluationSuiteImportAttempt.id == import_attempt_id,
                        EvaluationSuiteImportAttempt.department_id == department_id,
                    )
                    .with_for_update()
                ).scalar_one_or_none()
                if attempt is None or attempt.status != "published":
                    raise EvaluationContractError("artifact_reconciliation_failed")
                revalidate_ground_truth_authority_in_transaction(
                    session,
                    settings.data_dir,
                    scope,
                    cases,
                    current,
                )
                suite = EvaluationSuite(
                    id=suite_id,
                    department_id=department_id,
                    imported_by_user_id=authorization.identity.id,
                    status="active",
                    suite_contract_version=SUITE_CONTRACT_VERSION,
                    artifact_contract_version=ARTIFACT_CONTRACT_VERSION,
                    metric_contract_version=METRIC_CONTRACT_VERSION,
                    answer_normalization_version=ANSWER_NORMALIZATION_VERSION,
                    gate_policy_version=GATE_POLICY_VERSION,
                    case_count=len(cases),
                    answered_case_count=answered,
                    insufficient_case_count=insufficient,
                    artifact_manifest_sha256=staged.manifest.sha256,
                    canonical_cases_sha256=staged.payload.sha256,
                    canonical_cases_byte_size=staged.payload.byte_size,
                    **gates.as_dict(),
                )
                session.add(suite)
                session.flush()
                append_mutation_audit(
                    session,
                    actor=authorization.identity,
                    actor_subject=principal.subject,
                    request_scope=request_scope,
                    action="evaluation.suite.import",
                    resource_type="evaluation_suite",
                    resource_id=suite.id,
                )
                suite.artifact_manifest_sha256 = published_artifact.manifest.sha256
                suite.canonical_cases_sha256 = published_artifact.payload.sha256
                suite.canonical_cases_byte_size = published_artifact.payload.byte_size
                now = session.execute(select(_clock_timestamp())).scalar_one()
                attempt.status = "committed"
                attempt.published_at = attempt.published_at or now
                attempt.committed_at = now
                attempt.version += 1
                session.flush()
        except Exception:
            _finalize_failed_suite_import(
                factory,
                store,
                scope,
                department_id,
                suite_id,
                stage_id,
                import_attempt_id,
            )
            raise
        return SuiteImportResult(suite_id, department_id, len(cases), answered, insufficient, True)
    except (ServiceError, EvaluationContractError):
        if apply:
            _finalize_failed_suite_import(
                factory,
                store,
                scope,
                department_id,
                suite_id,
                stage_id,
                import_attempt_id,
            )
        raise
    except SQLAlchemyError as error:
        if apply:
            _finalize_failed_suite_import(
                factory,
                store,
                scope,
                department_id,
                suite_id,
                stage_id,
                import_attempt_id,
            )
        raise EvaluationContractError("database_unavailable") from error
    finally:
        engine.dispose()


def _finalize_failed_suite_import(
    factory: sessionmaker[Session],
    store: EvaluationArtifactStore,
    scope: DepartmentScope,
    department_id: UUID,
    suite_id: UUID,
    stage_id: UUID,
    import_attempt_id: UUID,
) -> None:
    """Terminalize only after exact descriptor-verified external cleanup.

    A database failure after removal leaves a recoverable non-terminal ownership row;
    reconciliation can safely resume it. A malformed or foreign artifact is never removed.
    """

    try:
        with factory.begin() as session:
            attempt = session.execute(
                select(EvaluationSuiteImportAttempt)
                .where(
                    EvaluationSuiteImportAttempt.id == import_attempt_id,
                    EvaluationSuiteImportAttempt.department_id == department_id,
                    EvaluationSuiteImportAttempt.status.in_(("registered", "staged", "published")),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                return

            if store.suite_final_present(scope, suite_id):
                store.remove_owned_suite_final(scope, suite_id, import_attempt_id)
            if store.suite_stage_present(scope, suite_id, stage_id):
                store.remove_owned_suite_stage(scope, suite_id, stage_id, import_attempt_id)
            if store.suite_final_present(scope, suite_id) or store.suite_stage_present(
                scope, suite_id, stage_id
            ):
                raise EvaluationContractError("artifact_reconciliation_failed")

            now = session.scalar(select(_clock_timestamp()))
            attempt.status = "failed"
            attempt.failed_at = now
            attempt.cleanup_confirmed_at = now
            attempt.version += 1
    except (SQLAlchemyError, EvaluationContractError):
        return


def archive_suite(
    factory: sessionmaker[Session],
    *,
    department_id: UUID,
    suite_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    apply: bool = True,
) -> bool:
    scope = DepartmentScope(department_id)
    request_scope = DepartmentRequestScope(scope)
    principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
    try:
        with factory.begin() as session:
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                EVALUATOR_ROLES,
                lock=True,
                audit_action="evaluation.suite.archive.authorization",
            )
            suite = session.execute(
                select(EvaluationSuite)
                .where(
                    EvaluationSuite.id == suite_id,
                    EvaluationSuite.department_id == department_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if suite is None:
                raise ServiceError(404, "Evaluation suite not found")
            if suite.status != "active":
                raise ServiceError(409, "Evaluation suite is already archived")
            if not apply:
                return False
            suite.status = "archived"
            suite.archived_at = session.execute(select(_clock_timestamp())).scalar_one()
            suite.version += 1
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=principal.subject,
                request_scope=request_scope,
                action="evaluation.suite.archive",
                resource_type="evaluation_suite",
                resource_id=suite.id,
            )
            return True
    except (ServiceError, EvaluationContractError):
        raise
    except SQLAlchemyError as error:
        raise ServiceError(503, "Database unavailable") from error


def reconcile_artifacts(
    factory: sessionmaker[Session],
    *,
    data_dir: Path,
    department_id: UUID,
    actor_issuer: str,
    actor_subject: str,
    limit: int,
    apply: bool = False,
) -> tuple[ArtifactReconcileItem, ...]:
    """Recover only exact metadata-owned artifacts; dry-runs have no side effects."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise EvaluationContractError()
    scope = DepartmentScope(department_id)
    request_scope = DepartmentRequestScope(scope)
    principal = AuthenticatedPrincipal(actor_subject, actor_issuer)
    store = EvaluationArtifactStore(data_dir)
    try:
        if not apply:
            with factory.begin() as session:
                authorize_transaction(
                    session,
                    principal,
                    request_scope,
                    RECONCILER_ROLES,
                    lock=True,
                    audit_action="evaluation.artifact.reconcile.authorization",
                )
                candidates = _reconciliation_candidates(session, department_id, limit)
            return tuple(
                _reconciliation_item(
                    store,
                    scope,
                    candidate,
                    applied=False,
                    reconciliation_status="dry_run",
                    blocked_reason_code=None,
                )
                for candidate in candidates
            )

        with factory.begin() as session:
            authorization = authorize_transaction(
                session,
                principal,
                request_scope,
                RECONCILER_ROLES,
                lock=True,
                audit_action="evaluation.artifact.reconcile.authorization",
            )
            operation = session.execute(
                select(EvaluationArtifactReconciliationOperation)
                .where(
                    EvaluationArtifactReconciliationOperation.department_id == department_id,
                    EvaluationArtifactReconciliationOperation.status == "registered",
                )
                .order_by(
                    EvaluationArtifactReconciliationOperation.created_at,
                    EvaluationArtifactReconciliationOperation.id,
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if operation is None:
                candidates = _reconciliation_candidates(session, department_id, limit)
                if not candidates:
                    return ()
                operation = EvaluationArtifactReconciliationOperation(
                    department_id=department_id,
                    actor_user_id=authorization.identity.id,
                    status="registered",
                )
                session.add(operation)
                session.flush()
                for candidate in candidates:
                    session.add(
                        EvaluationArtifactReconciliationOperationItem(
                            operation_id=operation.id,
                            department_id=department_id,
                            resource_type=candidate.resource_type,
                            resource_id=candidate.resource_id,
                            suite_id=candidate.suite_id,
                            ownership_attempt_id=candidate.ownership_attempt_id,
                            stage_id=candidate.stage_id,
                            attempt_number=candidate.attempt_number,
                            code_revision=candidate.code_revision,
                            status="registered",
                        )
                    )
                session.flush()
            candidates = _operation_candidates(session, operation.id, department_id)
            operation_id = operation.id

        outcomes = _apply_reconciliation_operation(
            factory,
            store,
            scope,
            principal,
            request_scope,
            operation_id,
        )
        result: list[ArtifactReconcileItem] = []
        for candidate in candidates:
            outcome, reason = outcomes.get(
                (candidate.resource_type, candidate.resource_id), ("registered", None)
            )
            result.append(
                _reconciliation_item(
                    store,
                    scope,
                    candidate,
                    applied=outcome == "completed",
                    reconciliation_status=outcome,
                    blocked_reason_code=reason,
                )
            )
        return tuple(result)
    except (ServiceError, EvaluationContractError):
        raise
    except SQLAlchemyError as error:
        raise EvaluationContractError("database_unavailable") from error


def _reconciliation_candidates(
    session: Session, department_id: UUID, limit: int
) -> tuple[_ArtifactReconcileCandidate, ...]:
    cutoff = session.scalar(
        select(func.clock_timestamp() - timedelta(seconds=RECONCILIATION_MINIMUM_AGE_SECONDS))
    )
    blocked_run = (
        ~select(EvaluationArtifactReconciliationOperationItem.id)
        .where(
            EvaluationArtifactReconciliationOperationItem.department_id == department_id,
            EvaluationArtifactReconciliationOperationItem.resource_type == "evaluation_run",
            EvaluationArtifactReconciliationOperationItem.resource_id == EvaluationRun.id,
            EvaluationArtifactReconciliationOperationItem.status == "blocked",
        )
        .exists()
    )
    runs = tuple(
        session.execute(
            select(
                EvaluationRun.id,
                EvaluationRun.suite_id,
                EvaluationRun.publication_attempt_id,
                EvaluationRun.attempt_number,
                EvaluationRun.code_revision,
                EvaluationRun.status,
                EvaluationRun.created_at,
            )
            .where(
                EvaluationRun.department_id == department_id,
                EvaluationRun.status.in_(("failed", "cancelled")),
                EvaluationRun.publication_attempt_id.is_not(None),
                EvaluationRun.updated_at <= cutoff,
                blocked_run,
            )
            .order_by(EvaluationRun.created_at, EvaluationRun.id)
            .limit(limit)
        )
    )
    candidates = [
        _ArtifactReconcileCandidate(
            "evaluation_run",
            run_id,
            suite_id,
            publication_attempt_id,
            publication_attempt_id,
            attempt_number,
            code_revision,
            status,
            created_at,
        )
        for (
            run_id,
            suite_id,
            publication_attempt_id,
            attempt_number,
            code_revision,
            status,
            created_at,
        ) in runs
        if publication_attempt_id is not None
    ]
    remaining = limit - len(candidates)
    if remaining <= 0:
        return tuple(candidates)
    blocked_suite = (
        ~select(EvaluationArtifactReconciliationOperationItem.id)
        .where(
            EvaluationArtifactReconciliationOperationItem.department_id == department_id,
            EvaluationArtifactReconciliationOperationItem.resource_type
            == "evaluation_suite_import_attempt",
            EvaluationArtifactReconciliationOperationItem.resource_id
            == EvaluationSuiteImportAttempt.id,
            EvaluationArtifactReconciliationOperationItem.status == "blocked",
        )
        .exists()
    )
    attempts = tuple(
        session.execute(
            select(
                EvaluationSuiteImportAttempt.id,
                EvaluationSuiteImportAttempt.suite_id,
                EvaluationSuiteImportAttempt.stage_id,
                EvaluationSuiteImportAttempt.status,
                EvaluationSuiteImportAttempt.created_at,
            )
            .where(
                EvaluationSuiteImportAttempt.department_id == department_id,
                EvaluationSuiteImportAttempt.status.in_(("registered", "staged", "published")),
                EvaluationSuiteImportAttempt.updated_at <= cutoff,
                blocked_suite,
            )
            .order_by(EvaluationSuiteImportAttempt.created_at, EvaluationSuiteImportAttempt.id)
            .limit(remaining)
        )
    )
    candidates.extend(
        _ArtifactReconcileCandidate(
            "evaluation_suite_import_attempt",
            attempt_id,
            suite_id,
            attempt_id,
            stage_id,
            None,
            None,
            status,
            created_at,
        )
        for attempt_id, suite_id, stage_id, status, created_at in attempts
    )
    return tuple(candidates)


def _operation_candidates(
    session: Session, operation_id: UUID, department_id: UUID
) -> tuple[_ArtifactReconcileCandidate, ...]:
    rows = tuple(
        session.execute(
            select(EvaluationArtifactReconciliationOperationItem)
            .where(
                EvaluationArtifactReconciliationOperationItem.operation_id == operation_id,
                EvaluationArtifactReconciliationOperationItem.department_id == department_id,
            )
            .order_by(
                EvaluationArtifactReconciliationOperationItem.created_at,
                EvaluationArtifactReconciliationOperationItem.id,
            )
        ).scalars()
    )
    candidates: list[_ArtifactReconcileCandidate] = []
    for item in rows:
        if item.resource_type == "evaluation_run":
            row = session.execute(
                select(EvaluationRun.status, EvaluationRun.created_at).where(
                    EvaluationRun.id == item.resource_id,
                    EvaluationRun.department_id == department_id,
                )
            ).one_or_none()
        else:
            row = session.execute(
                select(
                    EvaluationSuiteImportAttempt.status,
                    EvaluationSuiteImportAttempt.created_at,
                ).where(
                    EvaluationSuiteImportAttempt.id == item.resource_id,
                    EvaluationSuiteImportAttempt.department_id == department_id,
                )
            ).one_or_none()
        if row is None:
            row = ("unavailable", item.created_at)
        candidates.append(
            _ArtifactReconcileCandidate(
                item.resource_type,
                item.resource_id,
                item.suite_id,
                item.ownership_attempt_id,
                item.stage_id,
                item.attempt_number,
                item.code_revision,
                row.status,
                row.created_at,
            )
        )
    return tuple(candidates)


def _apply_reconciliation_operation(
    factory: sessionmaker[Session],
    store: EvaluationArtifactStore,
    scope: DepartmentScope,
    principal: AuthenticatedPrincipal,
    request_scope: DepartmentRequestScope,
    operation_id: UUID,
) -> dict[tuple[str, UUID], tuple[str, str | None]]:
    """Delete under locked metadata, then atomically terminalize and audit the batch."""

    outcomes: dict[tuple[str, UUID], tuple[str, str | None]] = {}
    applied: set[tuple[str, UUID]] = set()
    with factory.begin() as session:
        authorization = authorize_transaction(
            session,
            principal,
            request_scope,
            RECONCILER_ROLES,
            lock=True,
            audit_action="evaluation.artifact.reconcile.authorization",
        )
        operation = session.execute(
            select(EvaluationArtifactReconciliationOperation)
            .where(
                EvaluationArtifactReconciliationOperation.id == operation_id,
                EvaluationArtifactReconciliationOperation.department_id == scope.value,
                EvaluationArtifactReconciliationOperation.status == "registered",
            )
            .with_for_update()
        ).scalar_one_or_none()
        if operation is None:
            return outcomes
        items = tuple(
            session.scalars(
                select(EvaluationArtifactReconciliationOperationItem)
                .where(
                    EvaluationArtifactReconciliationOperationItem.operation_id == operation_id,
                    EvaluationArtifactReconciliationOperationItem.status == "registered",
                )
                .order_by(
                    EvaluationArtifactReconciliationOperationItem.created_at,
                    EvaluationArtifactReconciliationOperationItem.id,
                )
                .with_for_update()
            )
        )
        now = session.scalar(select(_clock_timestamp()))
        for item in items:
            current = _ArtifactReconcileCandidate(
                item.resource_type,
                item.resource_id,
                item.suite_id,
                item.ownership_attempt_id,
                item.stage_id,
                item.attempt_number,
                item.code_revision,
                "registered",
                item.created_at,
            )
            try:
                if item.resource_type == "evaluation_run":
                    row = session.execute(
                        select(EvaluationRun)
                        .where(
                            EvaluationRun.id == item.resource_id,
                            EvaluationRun.department_id == scope.value,
                            EvaluationRun.suite_id == item.suite_id,
                            EvaluationRun.status.in_(("failed", "cancelled")),
                            EvaluationRun.publication_attempt_id == item.ownership_attempt_id,
                            EvaluationRun.attempt_number == item.attempt_number,
                            EvaluationRun.code_revision == item.code_revision,
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    if row is None:
                        raise EvaluationContractError("result_publication_failed")
                    _delete_owned_run_artifacts(store, scope, current)
                    row.publication_attempt_id = None
                    row.version += 1
                else:
                    row = session.execute(
                        select(EvaluationSuiteImportAttempt)
                        .where(
                            EvaluationSuiteImportAttempt.id == item.resource_id,
                            EvaluationSuiteImportAttempt.department_id == scope.value,
                            EvaluationSuiteImportAttempt.suite_id == item.suite_id,
                            EvaluationSuiteImportAttempt.stage_id == item.stage_id,
                            EvaluationSuiteImportAttempt.status.in_(
                                ("registered", "staged", "published")
                            ),
                        )
                        .with_for_update()
                    ).scalar_one_or_none()
                    if row is None:
                        raise EvaluationContractError("result_publication_failed")
                    _delete_owned_suite_artifacts(store, scope, current)
                    row.status = "abandoned"
                    row.abandoned_at = now
                    row.cleanup_confirmed_at = now
                    row.version += 1
            except EvaluationContractError as error:
                if error.code != "result_publication_failed":
                    raise
                reason = _blocked_reconciliation_reason(store, scope, current, error)
                item.status = "blocked"
                item.blocked_at = now
                item.blocked_reason_code = reason
                outcomes[(item.resource_type, item.resource_id)] = ("blocked", reason)
                continue
            item.status = "completed"
            item.completed_at = now
            applied.add((item.resource_type, item.resource_id))
            outcomes[(item.resource_type, item.resource_id)] = ("completed", None)
        has_blocks = (
            session.scalar(
                select(EvaluationArtifactReconciliationOperationItem.id)
                .where(
                    EvaluationArtifactReconciliationOperationItem.operation_id == operation.id,
                    EvaluationArtifactReconciliationOperationItem.status == "blocked",
                )
                .limit(1)
            )
            is not None
        )
        operation.status = "completed_with_blocks" if has_blocks else "completed"
        operation.completed_at = now
        operation.version += 1
        if applied:
            append_mutation_audit(
                session,
                actor=authorization.identity,
                actor_subject=principal.subject,
                request_scope=request_scope,
                action="evaluation.artifact.reconcile",
                resource_type="evaluation_artifact_reconciliation_operation",
                resource_id=operation.id,
            )
    return outcomes


def _reconciliation_item(
    store: EvaluationArtifactStore,
    scope: DepartmentScope,
    candidate: _ArtifactReconcileCandidate,
    *,
    applied: bool,
    reconciliation_status: str,
    blocked_reason_code: str | None,
) -> ArtifactReconcileItem:
    try:
        if candidate.resource_type == "evaluation_run":
            if candidate.attempt_number is None or candidate.code_revision is None:
                raise EvaluationContractError("artifact_reconciliation_failed")
            staging_present = store.run_stage_present(
                scope, candidate.resource_id, candidate.stage_id
            )
            final_present = store.run_final_present(scope, candidate.resource_id)
            staging_owned = store.run_stage_owned_by(
                scope,
                candidate.resource_id,
                candidate.suite_id,
                candidate.ownership_attempt_id,
                candidate.attempt_number,
                candidate.code_revision,
            )
            final_owned = store.run_final_owned_by(
                scope,
                candidate.resource_id,
                candidate.suite_id,
                candidate.ownership_attempt_id,
                candidate.attempt_number,
                candidate.code_revision,
            )
        else:
            staging_present = store.suite_stage_present(
                scope, candidate.suite_id, candidate.stage_id
            )
            final_present = store.suite_final_present(scope, candidate.suite_id)
            staging_owned = store.suite_stage_owned_by(
                scope,
                candidate.suite_id,
                candidate.stage_id,
                candidate.ownership_attempt_id,
            )
            final_owned = store.suite_final_owned_by(
                scope, candidate.suite_id, candidate.ownership_attempt_id
            )
    except EvaluationContractError:
        staging_present = staging_present if "staging_present" in locals() else False
        final_present = final_present if "final_present" in locals() else False
        staging_owned = False
        final_owned = False
    return ArtifactReconcileItem(
        candidate.resource_type,
        candidate.resource_id,
        candidate.status,
        candidate.created_at,
        staging_present,
        staging_owned,
        final_present,
        final_owned,
        applied,
        reconciliation_status,
        blocked_reason_code,
    )


def _delete_owned_run_artifacts(
    store: EvaluationArtifactStore, scope: DepartmentScope, candidate: _ArtifactReconcileCandidate
) -> None:
    if candidate.attempt_number is None or candidate.code_revision is None:
        raise EvaluationContractError("artifact_reconciliation_failed")
    if store.run_stage_present(scope, candidate.resource_id, candidate.stage_id):
        store.remove_owned_run_stage(
            scope,
            candidate.resource_id,
            candidate.suite_id,
            candidate.ownership_attempt_id,
            candidate.attempt_number,
            candidate.code_revision,
        )
    if store.run_final_present(scope, candidate.resource_id):
        if not store.run_final_owned_by(
            scope,
            candidate.resource_id,
            candidate.suite_id,
            candidate.ownership_attempt_id,
            candidate.attempt_number,
            candidate.code_revision,
        ):
            raise EvaluationContractError("artifact_reconciliation_failed")
        store.remove_owned_run_final(
            scope,
            candidate.resource_id,
            candidate.suite_id,
            candidate.ownership_attempt_id,
            candidate.attempt_number,
            candidate.code_revision,
        )


def _delete_owned_suite_artifacts(
    store: EvaluationArtifactStore, scope: DepartmentScope, candidate: _ArtifactReconcileCandidate
) -> None:
    if store.suite_stage_present(scope, candidate.suite_id, candidate.stage_id):
        store.remove_owned_suite_stage(
            scope,
            candidate.suite_id,
            candidate.stage_id,
            candidate.ownership_attempt_id,
        )
    if store.suite_final_present(scope, candidate.suite_id):
        if not store.suite_final_owned_by(
            scope, candidate.suite_id, candidate.ownership_attempt_id
        ):
            raise EvaluationContractError("artifact_reconciliation_failed")
        store.remove_owned_suite_final(scope, candidate.suite_id, candidate.ownership_attempt_id)


def _blocked_reconciliation_reason(
    store: EvaluationArtifactStore,
    scope: DepartmentScope,
    candidate: _ArtifactReconcileCandidate,
    _error: EvaluationContractError,
) -> str:
    """Classify only reviewed, content-free terminal outcomes for manual follow-up."""

    if candidate.resource_type == "evaluation_run":
        if store.run_stage_present(scope, candidate.resource_id, candidate.stage_id):
            return "staging_path_unsafe"
        if store.run_final_present(scope, candidate.resource_id):
            return "artifact_manifest_invalid"
    else:
        if store.suite_stage_present(scope, candidate.suite_id, candidate.stage_id):
            return "staging_path_unsafe"
        if store.suite_final_present(scope, candidate.suite_id):
            return "artifact_manifest_invalid"
    return "artifact_ownership_mismatch"


def capture_canonical_suite_authority(
    factory: sessionmaker[Session],
    data_dir: Path,
    scope: DepartmentScope,
    cases: tuple[dict[str, object], ...],
) -> GroundTruthAuthoritySnapshot:
    parsed = tuple(_canonical_case_to_parsed(case) for case in cases)
    snapshot = capture_ground_truth_authority(factory, data_dir, scope, parsed)
    _compare_canonical_snapshots(cases, snapshot.sources)
    return snapshot


def capture_ground_truth_authority(
    factory: sessionmaker[Session],
    data_dir: Path,
    scope: DepartmentScope,
    cases: tuple[ParsedEvaluationCase, ...],
) -> GroundTruthAuthoritySnapshot:
    with factory() as session:
        current, validations = _ground_truth_snapshot_metadata(session, scope, cases)
    artifacts = _verify_ground_truth_artifacts(data_dir, scope, validations)
    return GroundTruthAuthoritySnapshot(current, artifacts)


def revalidate_ground_truth_authority_in_transaction(
    session: Session,
    data_dir: Path,
    scope: DepartmentScope,
    cases: tuple[ParsedEvaluationCase, ...],
    verified: GroundTruthAuthoritySnapshot,
) -> None:
    current, _validations = _ground_truth_snapshot_metadata(
        session,
        scope,
        cases,
        lock=True,
    )
    if current != verified.sources:
        raise EvaluationContractError("suite_source_stale")
    recheck_ground_truth_artifact_identities(data_dir, scope, verified.artifacts)


def revalidate_canonical_suite_authority_in_transaction(
    session: Session,
    data_dir: Path,
    scope: DepartmentScope,
    cases: tuple[dict[str, object], ...],
    verified: GroundTruthAuthoritySnapshot,
) -> None:
    parsed = tuple(_canonical_case_to_parsed(case) for case in cases)
    revalidate_ground_truth_authority_in_transaction(
        session,
        data_dir,
        scope,
        parsed,
        verified,
    )
    _compare_canonical_snapshots(cases, verified.sources)


def recheck_ground_truth_artifact_identities(
    data_dir: Path,
    scope: DepartmentScope,
    artifacts: tuple[VerifiedGroundTruthArtifact, ...],
) -> None:
    try:
        for artifact in artifacts:
            verify_artifact_authority_identity(
                data_dir,
                scope,
                artifact.expectation,
                artifact.identity,
            )
    except ArtifactError as error:
        raise EvaluationContractError("suite_source_stale") from error


def _compare_canonical_snapshots(
    cases: tuple[dict[str, object], ...],
    current: dict[UUID, dict[str, object]],
) -> None:
    for case in cases:
        sources = case.get("relevant_sources")
        if not isinstance(sources, list):
            raise EvaluationContractError("suite_artifact_mismatch")
        expected = {
            UUID(item["chunk_id"]): item
            for item in sources
            if isinstance(item, dict) and isinstance(item.get("chunk_id"), str)
        }
        if len(expected) != len(sources):
            raise EvaluationContractError("suite_artifact_mismatch")
        for chunk_id, snapshot in expected.items():
            if current.get(chunk_id) != snapshot:
                raise EvaluationContractError("suite_source_stale")


def _suite_definition(value: dict[str, object]) -> QualityGates:
    expected = {
        "suite_contract_version",
        "metric_contract_version",
        "answer_normalization_version",
        "gate_policy_version",
        "gates",
    }
    if (
        set(value) != expected
        or value.get("suite_contract_version") != SUITE_CONTRACT_VERSION
        or value.get("metric_contract_version") != METRIC_CONTRACT_VERSION
        or value.get("answer_normalization_version") != ANSWER_NORMALIZATION_VERSION
        or value.get("gate_policy_version") != GATE_POLICY_VERSION
    ):
        raise EvaluationContractError()
    return parse_quality_gates(value.get("gates"))


def _parse_cases(source: Path):
    yield from _parse_case_values(iter_source_cases(source))


def _parse_case_values(values):
    case_ids: set[UUID] = set()
    count = 0
    for value in values:
        count += 1
        if count > MAX_SUITE_CASES or set(value) != {
            "case_id",
            "expected_status",
            "question",
            "relevant_chunk_ids",
            "accepted_answers",
        }:
            raise EvaluationContractError()
        case_id = _uuid(value.get("case_id"))
        if case_id in case_ids:
            raise EvaluationContractError()
        case_ids.add(case_id)
        expected_status = value.get("expected_status")
        if expected_status not in {"answered", "insufficient_information"}:
            raise EvaluationContractError()
        question = value.get("question")
        if not isinstance(question, str) or unicodedata.normalize("NFC", question) != question:
            raise EvaluationContractError()
        try:
            normalized_question = normalize_question(question)
        except ValueError as error:
            raise EvaluationContractError() from error
        if normalized_question != question or len(question) > MAX_QUESTION_CHARS:
            raise EvaluationContractError()
        chunks = value.get("relevant_chunk_ids")
        answers = value.get("accepted_answers")
        if not isinstance(chunks, list) or not isinstance(answers, list):
            raise EvaluationContractError()
        chunk_ids = tuple(_uuid(item) for item in chunks)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise EvaluationContractError()
        accepted = tuple(_accepted_answer(item) for item in answers)
        if len({normalize_answer(item) for item in accepted}) != len(accepted):
            raise EvaluationContractError()
        if expected_status == "answered":
            if not 1 <= len(chunk_ids) <= 8 or not 1 <= len(accepted) <= 8:
                raise EvaluationContractError()
        elif chunk_ids or accepted:
            raise EvaluationContractError()
        yield ParsedEvaluationCase(case_id, expected_status, question, chunk_ids, accepted)
    if count == 0:
        raise EvaluationContractError()


def _canonical_case_to_parsed(value: dict[str, object]) -> ParsedEvaluationCase:
    if set(value) != {
        "case_id",
        "expected_status",
        "question",
        "relevant_sources",
        "accepted_answers",
    }:
        raise EvaluationContractError("suite_artifact_mismatch")
    sources = value.get("relevant_sources")
    if not isinstance(sources, list):
        raise EvaluationContractError("suite_artifact_mismatch")
    source_ids = tuple(
        _uuid(item.get("chunk_id")) if isinstance(item, dict) else _uuid(None) for item in sources
    )
    question = value.get("question")
    answers = value.get("accepted_answers")
    if not isinstance(question, str) or not isinstance(answers, list):
        raise EvaluationContractError("suite_artifact_mismatch")
    return ParsedEvaluationCase(
        _uuid(value.get("case_id")),
        str(value.get("expected_status")),
        question,
        source_ids,
        tuple(_accepted_answer(item) for item in answers),
    )


def _accepted_answer(value: object) -> str:
    if (
        not isinstance(value, str)
        or unicodedata.normalize("NFC", value) != value
        or not 1 <= len(value) <= MAX_ACCEPTED_ANSWER_CHARS
        or not value.strip()
    ):
        raise EvaluationContractError()
    try:
        validate_safe_text(value, field="accepted answer", max_chars=MAX_ACCEPTED_ANSWER_CHARS)
    except ValueError as error:
        raise EvaluationContractError() from error
    return value


def _uuid(value: object) -> UUID:
    try:
        parsed = UUID(value) if isinstance(value, str) else value
    except ValueError as error:
        raise EvaluationContractError() from error
    if not isinstance(parsed, UUID) or parsed.int == 0:
        raise EvaluationContractError()
    return parsed


def _ground_truth_snapshot_metadata(
    session: Session,
    scope: DepartmentScope,
    cases: tuple[ParsedEvaluationCase, ...],
    *,
    lock: bool = False,
) -> tuple[
    dict[UUID, dict[str, object]],
    tuple[GroundTruthArtifactValidation, ...],
]:
    chunk_ids = sorted(
        {chunk_id for case in cases for chunk_id in case.relevant_chunk_ids}, key=str
    )
    if not chunk_ids:
        return {}, ()
    statement = (
        select(Document, DocumentExtraction, DocumentVectorIndexing, DocumentChunk)
        .join(
            DocumentExtraction,
            (DocumentExtraction.document_id == Document.id)
            & (DocumentExtraction.department_id == Document.department_id),
        )
        .join(
            DocumentVectorIndexing,
            (DocumentVectorIndexing.extraction_id == DocumentExtraction.id)
            & (DocumentVectorIndexing.document_id == Document.id)
            & (DocumentVectorIndexing.department_id == Document.department_id),
        )
        .join(
            DocumentChunk,
            (DocumentChunk.extraction_id == DocumentExtraction.id)
            & (DocumentChunk.document_id == Document.id)
            & (DocumentChunk.department_id == Document.department_id),
        )
        .where(
            Document.department_id == scope.value,
            Document.status == "stored",
            DocumentExtraction.status == "succeeded",
            DocumentExtraction.pipeline_version == PIPELINE_VERSION,
            DocumentExtraction.normalization_version == NORMALIZATION_VERSION,
            DocumentExtraction.chunking_version == CHUNKING_VERSION,
            DocumentVectorIndexing.status == "succeeded",
            DocumentVectorIndexing.point_count == DocumentVectorIndexing.expected_chunk_count,
            DocumentVectorIndexing.expected_chunk_count == DocumentExtraction.chunk_count,
            DocumentVectorIndexing.embedding_pipeline_version == EMBEDDING_PIPELINE_VERSION,
            DocumentVectorIndexing.embedding_model_id == EMBEDDING_MODEL_ID,
            DocumentVectorIndexing.embedding_model_revision == EMBEDDING_MODEL_REVISION,
            DocumentVectorIndexing.embedding_dimension == EMBEDDING_DIMENSION,
            DocumentVectorIndexing.distance == EMBEDDING_DISTANCE,
            DocumentVectorIndexing.vector_schema_version == VECTOR_SCHEMA_VERSION,
            DocumentVectorIndexing.qdrant_collection == QDRANT_COLLECTION,
            DocumentVectorIndexing.vector_attempt_id.is_not(None),
            DocumentChunk.id.in_(chunk_ids),
        )
        .order_by(
            Document.id,
            DocumentExtraction.id,
            DocumentVectorIndexing.id,
            DocumentChunk.id,
        )
    )
    if lock:
        statement = statement.with_for_update(
            of=(Document, DocumentExtraction, DocumentVectorIndexing, DocumentChunk)
        )
    rows = session.execute(statement).all()
    if len(rows) != len(chunk_ids):
        raise EvaluationContractError("suite_source_stale")
    snapshots: dict[UUID, dict[str, object]] = {}
    by_extraction: dict[UUID, list[tuple[DocumentExtraction, DocumentChunk]]] = {}
    for document, extraction, indexing, chunk in rows:
        snapshot = {
            "department_id": str(document.department_id),
            "document_status": document.status,
            "chunk_id": str(chunk.id),
            "document_id": str(document.id),
            "extraction_id": str(extraction.id),
            "indexing_id": str(indexing.id),
            "vector_attempt_id": str(indexing.vector_attempt_id),
            "extraction_status": extraction.status,
            "extraction_chunk_count": extraction.chunk_count,
            "normalized_sha256": extraction.normalized_sha256,
            "normalized_byte_size": extraction.normalized_byte_size,
            "extraction_output_byte_size": extraction.output_byte_size,
            "indexing_status": indexing.status,
            "indexing_point_count": indexing.point_count,
            "indexing_expected_chunk_count": indexing.expected_chunk_count,
            "ordinal": chunk.ordinal,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            "byte_size": chunk.byte_size,
            "content_sha256": chunk.content_sha256,
            "provenance_kind": chunk.provenance_kind,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "line_start": chunk.line_start,
            "line_end": chunk.line_end,
            "extraction_pipeline_version": extraction.pipeline_version,
            "normalization_version": extraction.normalization_version,
            "chunking_version": extraction.chunking_version,
            "embedding_pipeline_version": indexing.embedding_pipeline_version,
            "embedding_model_id": indexing.embedding_model_id,
            "embedding_model_revision": indexing.embedding_model_revision,
            "embedding_dimension": indexing.embedding_dimension,
            "distance": indexing.distance,
            "vector_schema_version": indexing.vector_schema_version,
            "qdrant_collection": indexing.qdrant_collection,
        }
        snapshots[chunk.id] = snapshot
        by_extraction.setdefault(extraction.id, []).append((extraction, chunk))
    if set(snapshots) != set(chunk_ids):
        raise EvaluationContractError("suite_source_stale")
    artifact_validations = []
    for grouped in by_extraction.values():
        extraction = grouped[0][0]
        expectation = ArtifactExpectation(
            department_id=extraction.department_id,
            document_id=extraction.document_id,
            extraction_id=extraction.id,
            expected_chunk_count=extraction.chunk_count,
            normalized_sha256=extraction.normalized_sha256,
            normalized_byte_size=extraction.normalized_byte_size,
            output_byte_size=extraction.output_byte_size,
        )
        targets = {
            chunk.ordinal: {
                "content_sha256": chunk.content_sha256,
                "byte_size": chunk.byte_size,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "provenance_kind": chunk.provenance_kind,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
            }
            for _extraction, chunk in grouped
        }
        artifact_validations.append(GroundTruthArtifactValidation(expectation, targets))
    return snapshots, tuple(artifact_validations)


def _verify_ground_truth_artifacts(
    data_dir: Path,
    scope: DepartmentScope,
    validations: tuple[GroundTruthArtifactValidation, ...],
) -> tuple[VerifiedGroundTruthArtifact, ...]:
    try:
        verified: list[VerifiedGroundTruthArtifact] = []
        for validation in validations:
            found: set[int] = set()
            with Phase5ArtifactReader(data_dir, scope, validation.expectation) as reader:
                for artifact_chunk in reader.iter_chunks():
                    expected = validation.targets.get(artifact_chunk.ordinal)
                    if expected is None:
                        continue
                    if (
                        artifact_chunk.content_sha256 != expected["content_sha256"]
                        or artifact_chunk.byte_size != expected["byte_size"]
                        or artifact_chunk.char_start != expected["char_start"]
                        or artifact_chunk.char_end != expected["char_end"]
                        or artifact_chunk.provenance_kind != expected["provenance_kind"]
                        or artifact_chunk.page_start != expected["page_start"]
                        or artifact_chunk.page_end != expected["page_end"]
                        or artifact_chunk.line_start != expected["line_start"]
                        or artifact_chunk.line_end != expected["line_end"]
                    ):
                        raise EvaluationContractError("suite_source_stale")
                    found.add(artifact_chunk.ordinal)
                reader.verify_unchanged()
                identity = reader.authority_identity()
            if found != set(validation.targets):
                raise EvaluationContractError("suite_source_stale")
            verified.append(VerifiedGroundTruthArtifact(validation.expectation, identity))
        return tuple(verified)
    except ArtifactError as error:
        raise EvaluationContractError("suite_source_stale") from error


def _canonical_case(
    case: ParsedEvaluationCase, snapshots: dict[UUID, dict[str, object]]
) -> dict[str, object]:
    return {
        "case_id": str(case.case_id),
        "expected_status": case.expected_status,
        "question": case.question,
        "relevant_sources": [snapshots[item] for item in case.relevant_chunk_ids],
        "accepted_answers": list(case.accepted_answers),
    }


def _suite_manifest(
    *,
    department_id: UUID,
    suite_id: UUID,
    import_attempt_id: UUID,
    stage_id: UUID,
    gates: QualityGates,
    case_count: int,
    answered: int,
    insufficient: int,
) -> dict[str, object]:
    return {
        "suite_id": str(suite_id),
        "department_id": str(department_id),
        "import_attempt_id": str(import_attempt_id),
        "stage_id": str(stage_id),
        "suite_contract_version": SUITE_CONTRACT_VERSION,
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "answer_normalization_version": ANSWER_NORMALIZATION_VERSION,
        "gate_policy_version": GATE_POLICY_VERSION,
        "case_count": case_count,
        "answered_case_count": answered,
        "insufficient_case_count": insufficient,
        "gates": gates.as_dict(),
    }


def _clock_timestamp():
    from sqlalchemy import func

    return func.clock_timestamp()


def _repository_root(start: Path) -> Path | None:
    for candidate in start.resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    return None
