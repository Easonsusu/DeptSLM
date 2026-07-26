"""One claimed Phase 9 run through production-policy execution and publication."""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.authorization import DepartmentScope
from app.database import create_database_engine, create_session_factory
from app.evaluation_artifacts import EvaluationArtifactStore, PublishedArtifact, StagedArtifact
from app.evaluation_domain import (
    ANSWER_NORMALIZATION_VERSION,
    ARTIFACT_CONTRACT_VERSION,
    GATE_POLICY_VERSION,
    METRIC_CONTRACT_VERSION,
    RUNNER_CONTRACT_VERSION,
    SEED_DERIVATION_VERSION,
    SUITE_CONTRACT_VERSION,
    EvaluationContractError,
    QualityGates,
    aggregate_metrics,
    derive_case_seed,
    evaluate_gates,
    production_contract,
    score_case,
)
from app.evaluation_suites import (
    GroundTruthAuthoritySnapshot,
    capture_canonical_suite_authority,
)
from app.rag_answer_services import (
    RagPolicyEvaluationError,
    SafeRetrievalTrace,
    execute_rag_policy,
    revalidate_ephemeral_sources,
)
from app.rag_domain import RagContractError
from app.rag_runtime_client import RagRuntimeClient
from deptslm_worker.evaluation_queue import (
    ClaimedEvaluationRun,
    EvaluationQueueError,
    fail_owned,
    finalize_success,
    reconcile_stale_publication,
    record_progress,
    renew_lease,
    require_live_claim,
    validate_claim_authority,
)
from deptslm_worker.evaluation_settings import EvaluationSettings
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError
from deptslm_worker.vector_retrieval import RetrievalBoundaryError

LOGGER = logging.getLogger("deptslm.evaluator")


class _WorkerStopped(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _CaseExecution:
    actual_status: str
    generated_answer: str
    trace: SafeRetrievalTrace
    cited_chunk_ids: tuple[UUID, ...]
    answer_contract_valid: bool
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _SuiteExecution:
    cases: tuple[dict[str, object], ...]
    authority: GroundTruthAuthoritySnapshot


def process_evaluation_run(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    store: EvaluationArtifactStore,
    job: ClaimedEvaluationRun,
    should_stop: Callable[[], bool],
) -> bool:
    scope = DepartmentScope(job.department_id)
    staged = None
    published = None
    try:
        require_live_claim(factory, job)
        if job.stale_publication_attempt_id is not None:
            store.cleanup_stage(scope, job.id, job.stale_publication_attempt_id, suite=False)
            require_live_claim(factory, job)
        reconcile_stale_publication(factory, store, job)
        suite = validate_claim_authority(factory, job)
        suite_execution = _supervise_suite_load(
            factory,
            settings,
            scope,
            suite.id,
            suite.artifact_manifest_sha256,
            suite.canonical_cases_sha256,
            suite.canonical_cases_byte_size,
            job,
            should_stop,
        )
        cases = suite_execution.cases
        if len(cases) != job.case_count:
            raise EvaluationQueueError("suite_artifact_mismatch")
        gates = _gates(suite)
        scores = []
        answered = 0
        insufficient = 0
        for case in cases:
            if should_stop():
                raise _WorkerStopped()
            expected_status, relevant_ids, accepted_answers = _case_contract(case)
            execution = _supervise_case(
                factory,
                settings,
                scope,
                case["question"],
                derive_case_seed(job.base_seed, UUID(case["case_id"])),
                job,
                should_stop,
            )
            score = score_case(
                case_id=UUID(case["case_id"]),
                expected_status=expected_status,
                relevant_chunk_ids=relevant_ids,
                accepted_answers=accepted_answers,
                actual_status=execution.actual_status,
                generated_answer=execution.generated_answer,
                authorized_candidate_ids=execution.trace.authorized_candidate_ids,
                cited_chunk_ids=execution.cited_chunk_ids,
                answer_contract_valid=execution.answer_contract_valid,
                error_code=execution.error_code,
            )
            scores.append(score)
            answered += expected_status == "answered"
            insufficient += expected_status == "insufficient_information"
            record_progress(
                factory,
                job,
                completed=len(scores),
                answered=answered,
                insufficient=insufficient,
            )
        metrics = aggregate_metrics(scores)
        gate = evaluate_gates(metrics, gates)
        final_authority = _supervise_authority(
            factory,
            settings,
            scope,
            cases,
            job,
            should_stop,
        )
        require_live_claim(factory, job)
        if should_stop():
            raise _WorkerStopped()
        manifest, summary = _result_values(job, metrics, gates, gate)
        staged = _supervise_result_stage(
            factory,
            settings,
            scope,
            job,
            manifest,
            summary,
            tuple(scores),
            should_stop,
        )
        require_live_claim(factory, job)
        if should_stop():
            raise _WorkerStopped()
        published = _supervise_result_publish(factory, settings, job, staged, should_stop)
        require_live_claim(factory, job)
        if should_stop():
            raise _WorkerStopped()
        finalize_success(
            factory,
            store,
            job,
            published,
            tuple(scores),
            metrics,
            gate,
            cases,
            final_authority,
        )
        _event(job, "complete", "allowed", "evaluation_succeeded")
        return True
    except _WorkerStopped:
        if staged is not None:
            try:
                require_live_claim(factory, job)
                store.cleanup_stage(scope, job.id, job.publication_attempt_id, suite=False)
            except (EvaluationQueueError, EvaluationContractError):
                pass
        _event(job, "processing", "denied", "worker_shutdown")
        if published is not None:
            try:
                require_live_claim(factory, job, allow_cancellation=True)
                store.remove_owned_run_final(scope, job.id, job.publication_attempt_id)
            except (EvaluationQueueError, EvaluationContractError):
                pass
        return False
    except EvaluationQueueError as error:
        code = error.code
    except EvaluationContractError as error:
        code = error.code
    except RagContractError as error:
        code = _rag_error_code(error.code)
    except QdrantBoundaryError:
        code = "qdrant_unavailable"
    except RetrievalBoundaryError:
        code = "retrieval_authority_failed"
    except SQLAlchemyError:
        code = "database_unavailable"
    except Exception:
        code = "generation_failed"

    if staged is not None:
        try:
            require_live_claim(factory, job, allow_cancellation=True)
            store.cleanup_stage(scope, job.id, job.publication_attempt_id, suite=False)
        except (EvaluationQueueError, EvaluationContractError):
            pass
    if published is not None:
        try:
            require_live_claim(factory, job, allow_cancellation=True)
            store.remove_owned_run_final(scope, job.id, job.publication_attempt_id)
        except (EvaluationQueueError, EvaluationContractError):
            pass
    fail_owned(factory, job, code)
    _event(job, "complete", "denied", code)
    return False


def _result_stage_process_entry(
    connection,
    settings: EvaluationSettings,
    scope: DepartmentScope,
    suite_id: UUID,
    run_id: UUID,
    publication_attempt_id: UUID,
    manifest: dict[str, object],
    summary: dict[str, object],
    scores: tuple,
) -> None:
    try:
        os.setsid()
    except OSError:
        pass
    connection.send(("ready",))
    try:
        staged, _summary = EvaluationArtifactStore(settings.data_dir).stage_run(
            scope,
            suite_id,
            run_id,
            publication_attempt_id,
            manifest_value=manifest,
            summary_value=summary,
            scores=scores,
        )
        connection.send(("result", staged))
    except EvaluationContractError as error:
        connection.send(("failure", error.code))
    except BaseException:
        connection.send(("failure", "result_publication_failed"))
    finally:
        connection.close()


def _supervise_result_stage(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    scope: DepartmentScope,
    job: ClaimedEvaluationRun,
    manifest: dict[str, object],
    summary: dict[str, object],
    scores: tuple,
    should_stop: Callable[[], bool],
) -> StagedArtifact:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_result_stage_process_entry,
        args=(
            child,
            settings,
            scope,
            job.suite_id,
            job.id,
            job.publication_attempt_id,
            manifest,
            summary,
            scores,
        ),
        daemon=False,
    )
    process.start()
    child.close()
    result = _wait_for_supervised_process(
        factory, settings, job, should_stop, parent, process, StagedArtifact
    )
    if not isinstance(result, StagedArtifact):
        raise EvaluationQueueError("result_publication_failed")
    return result


def _result_publish_process_entry(
    connection, settings: EvaluationSettings, staged: StagedArtifact
) -> None:
    try:
        os.setsid()
    except OSError:
        pass
    connection.send(("ready",))
    try:
        published = EvaluationArtifactStore(settings.data_dir).publish(
            staged, frozenset({"manifest.json", "summary.json", "case_results.jsonl"})
        )
        connection.send(("result", published))
    except EvaluationContractError as error:
        connection.send(("failure", error.code))
    except BaseException:
        connection.send(("failure", "result_publication_failed"))
    finally:
        connection.close()


def _supervise_result_publish(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    job: ClaimedEvaluationRun,
    staged: StagedArtifact,
    should_stop: Callable[[], bool],
) -> PublishedArtifact:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_result_publish_process_entry,
        args=(child, settings, staged),
        daemon=False,
    )
    process.start()
    child.close()
    result = _wait_for_supervised_process(
        factory, settings, job, should_stop, parent, process, PublishedArtifact
    )
    if not isinstance(result, PublishedArtifact):
        raise EvaluationQueueError("result_publication_failed")
    return result


def _suite_process_entry(
    connection,
    settings: EvaluationSettings,
    scope: DepartmentScope,
    suite_id: UUID,
    manifest_sha256: str,
    cases_sha256: str,
    cases_byte_size: int,
) -> None:
    try:
        os.setsid()
    except OSError:
        pass
    connection.send(("ready",))
    engine = None
    try:
        store = EvaluationArtifactStore(settings.data_dir)
        cases = tuple(
            store.iter_suite_cases(
                scope,
                suite_id,
                manifest_sha256=manifest_sha256,
                cases_sha256=cases_sha256,
                cases_byte_size=cases_byte_size,
            )
        )
        engine = create_database_engine(settings.database_url)
        authority = capture_canonical_suite_authority(
            create_session_factory(engine),
            settings.data_dir,
            scope,
            cases,
        )
        connection.send(("result", _SuiteExecution(cases, authority)))
    except EvaluationContractError as error:
        connection.send(("failure", error.code))
    except SQLAlchemyError:
        connection.send(("failure", "database_unavailable"))
    except BaseException:
        connection.send(("failure", "suite_source_stale"))
    finally:
        if engine is not None:
            engine.dispose()
        connection.close()


def _supervise_suite_load(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    scope: DepartmentScope,
    suite_id: UUID,
    manifest_sha256: str,
    cases_sha256: str,
    cases_byte_size: int,
    job: ClaimedEvaluationRun,
    should_stop: Callable[[], bool],
) -> _SuiteExecution:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_suite_process_entry,
        args=(
            child,
            settings,
            scope,
            suite_id,
            manifest_sha256,
            cases_sha256,
            cases_byte_size,
        ),
        daemon=False,
    )
    process.start()
    child.close()
    result = _wait_for_supervised_process(
        factory,
        settings,
        job,
        should_stop,
        parent,
        process,
        _SuiteExecution,
    )
    if not isinstance(result, _SuiteExecution):
        raise EvaluationQueueError("suite_source_stale")
    return result


def _case_process_entry(
    connection,
    settings: EvaluationSettings,
    scope: DepartmentScope,
    question: str,
    seed: int,
) -> None:
    try:
        os.setsid()
    except OSError:
        pass
    connection.send(("ready",))
    engine = None
    qdrant = None
    try:
        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        runtime = RagRuntimeClient(
            settings.rag.runtime_url,
            settings.rag.runtime_token,
            min(
                settings.rag.request_timeout_seconds,
                settings.operation_timeout_seconds,
            ),
        )
        qdrant = DepartmentQdrant(
            settings.rag.qdrant_url,
            settings.rag.qdrant_api_key,
            settings.rag.qdrant_timeout_seconds,
        )

        def report_stage(stage: str) -> None:
            connection.send(("stage", stage))

        outcome = execute_rag_policy(
            factory,
            settings.rag,
            settings.data_dir,
            scope,
            question,
            runtime,
            qdrant,
            seed=seed,
            stage_callback=report_stage,
        )
        report_stage("final_source_verification")
        revalidate_ephemeral_sources(factory, scope, outcome.supplied)
        connection.send(
            (
                "result",
                _CaseExecution(
                    outcome.status,
                    outcome.answer,
                    outcome.retrieval_trace,
                    outcome.cited_chunk_ids,
                    True,
                    None,
                ),
            )
        )
    except RagPolicyEvaluationError as error:
        connection.send(
            (
                "result",
                _CaseExecution(
                    "failed",
                    "",
                    error.trace,
                    (),
                    False,
                    error.code,
                ),
            )
        )
    except RagContractError as error:
        connection.send(("failure", _rag_error_code(error.code)))
    except QdrantBoundaryError:
        connection.send(("failure", "qdrant_unavailable"))
    except RetrievalBoundaryError:
        connection.send(("failure", "retrieval_authority_failed"))
    except SQLAlchemyError:
        connection.send(("failure", "database_unavailable"))
    except BaseException:
        connection.send(("failure", "generation_failed"))
    finally:
        if qdrant is not None:
            try:
                qdrant.close()
            except Exception:
                pass
        if engine is not None:
            engine.dispose()
        connection.close()


def _supervise_case(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    scope: DepartmentScope,
    question: str,
    seed: int,
    job: ClaimedEvaluationRun,
    should_stop: Callable[[], bool],
) -> _CaseExecution:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_case_process_entry,
        args=(child, settings, scope, question, seed),
        daemon=False,
    )
    process.start()
    child.close()
    result = _wait_for_supervised_process(
        factory,
        settings,
        job,
        should_stop,
        parent,
        process,
        _CaseExecution,
    )
    if not isinstance(result, _CaseExecution):
        raise EvaluationQueueError("generation_failed")
    return result


def _authority_process_entry(
    connection,
    settings: EvaluationSettings,
    scope: DepartmentScope,
    cases: tuple[dict[str, object], ...],
) -> None:
    try:
        os.setsid()
    except OSError:
        pass
    connection.send(("ready",))
    engine = None
    try:
        engine = create_database_engine(settings.database_url)
        factory = create_session_factory(engine)
        authority = capture_canonical_suite_authority(
            factory,
            settings.data_dir,
            scope,
            cases,
        )
        connection.send(("result", authority))
    except EvaluationContractError as error:
        connection.send(("failure", error.code))
    except SQLAlchemyError:
        connection.send(("failure", "database_unavailable"))
    except BaseException:
        connection.send(("failure", "suite_source_stale"))
    finally:
        if engine is not None:
            engine.dispose()
        connection.close()


def _supervise_authority(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    scope: DepartmentScope,
    cases: tuple[dict[str, object], ...],
    job: ClaimedEvaluationRun,
    should_stop: Callable[[], bool],
) -> GroundTruthAuthoritySnapshot:
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_authority_process_entry,
        args=(child, settings, scope, cases),
        daemon=False,
    )
    process.start()
    child.close()
    result = _wait_for_supervised_process(
        factory,
        settings,
        job,
        should_stop,
        parent,
        process,
        GroundTruthAuthoritySnapshot,
    )
    if not isinstance(result, GroundTruthAuthoritySnapshot):
        raise EvaluationQueueError("suite_source_stale")
    return result


def _wait_for_supervised_process(
    factory: sessionmaker[Session],
    settings: EvaluationSettings,
    job: ClaimedEvaluationRun,
    should_stop: Callable[[], bool],
    parent,
    process: multiprocessing.Process,
    result_type: type,
):
    deadline = time.monotonic() + settings.operation_timeout_seconds
    next_heartbeat = time.monotonic()
    group_ready = False
    completed = False
    try:
        while True:
            now = time.monotonic()
            if should_stop():
                raise _WorkerStopped()
            if now >= deadline:
                raise EvaluationQueueError("runtime_timeout")
            if now >= next_heartbeat:
                renew_lease(factory, job, settings.lease_seconds)
                next_heartbeat = now + settings.heartbeat_seconds
            wait_for = min(
                deadline - now,
                max(0.0, next_heartbeat - now),
                0.1,
            )
            if parent.poll(wait_for):
                try:
                    message = parent.recv()
                except EOFError as error:
                    raise EvaluationQueueError("generation_failed") from error
                if (
                    not isinstance(message, tuple)
                    or not message
                    or message[0] not in {"ready", "stage", "result", "failure"}
                ):
                    raise EvaluationQueueError("generation_failed")
                if message[0] == "ready" and len(message) == 1:
                    group_ready = True
                    continue
                if message[0] == "stage" and len(message) == 2:
                    continue
                if message[0] == "failure" and len(message) == 2:
                    raise EvaluationQueueError(str(message[1]))
                if (
                    message[0] == "result"
                    and len(message) == 2
                    and isinstance(message[1], result_type)
                ):
                    completed = True
                    return message[1]
                raise EvaluationQueueError("generation_failed")
            if not process.is_alive():
                raise EvaluationQueueError("generation_failed")
    finally:
        parent.close()
        _terminate_and_reap(process, group_ready=group_ready, terminate=not completed)


def _terminate_and_reap(
    process: multiprocessing.Process,
    *,
    group_ready: bool,
    terminate: bool,
) -> None:
    if terminate and process.is_alive():
        try:
            if group_ready and process.pid is not None:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass
    process.join(timeout=1)
    if process.is_alive():
        try:
            if group_ready and process.pid is not None:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass
        process.join(timeout=1)
    if process.is_alive():
        raise EvaluationQueueError("generation_failed")


def _case_contract(
    value: dict[str, object],
) -> tuple[str, tuple[UUID, ...], tuple[str, ...]]:
    if set(value) != {
        "case_id",
        "expected_status",
        "question",
        "relevant_sources",
        "accepted_answers",
    }:
        raise EvaluationContractError("suite_artifact_mismatch")
    expected = value.get("expected_status")
    sources = value.get("relevant_sources")
    answers = value.get("accepted_answers")
    if (
        expected not in {"answered", "insufficient_information"}
        or not isinstance(value.get("question"), str)
        or not isinstance(sources, list)
        or not isinstance(answers, list)
    ):
        raise EvaluationContractError("suite_artifact_mismatch")
    try:
        relevant = tuple(UUID(item["chunk_id"]) for item in sources)
    except (KeyError, TypeError, ValueError) as error:
        raise EvaluationContractError("suite_artifact_mismatch") from error
    if any(not isinstance(item, str) for item in answers):
        raise EvaluationContractError("suite_artifact_mismatch")
    return expected, relevant, tuple(answers)


def _gates(suite) -> QualityGates:
    if (
        suite.suite_contract_version != SUITE_CONTRACT_VERSION
        or suite.artifact_contract_version != ARTIFACT_CONTRACT_VERSION
        or suite.metric_contract_version != METRIC_CONTRACT_VERSION
        or suite.answer_normalization_version != ANSWER_NORMALIZATION_VERSION
        or suite.gate_policy_version != GATE_POLICY_VERSION
    ):
        raise EvaluationQueueError("suite_contract_invalid")
    return QualityGates(
        **{name: getattr(suite, name) for name in QualityGates.__dataclass_fields__}
    )


def _result_values(job, metrics, gates, gate):
    contract = dict(production_contract())
    manifest = {
        "run_id": str(job.id),
        "suite_id": str(job.suite_id),
        "department_id": str(job.department_id),
        "publication_attempt_id": str(job.publication_attempt_id),
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "runner_contract_version": RUNNER_CONTRACT_VERSION,
        "answer_normalization_version": ANSWER_NORMALIZATION_VERSION,
        "gate_policy_version": GATE_POLICY_VERSION,
        "seed_derivation_version": SEED_DERIVATION_VERSION,
        "base_seed": job.base_seed,
        "code_revision": job.code_revision,
        "case_count": job.case_count,
        **contract,
    }
    summary = {
        "case_count": job.case_count,
        "metrics": metrics.as_dict(),
        "gates": gates.as_dict(),
        "gate_results": gate.results,
        "gate_status": "passed" if gate.passed else "failed",
        "failed_gate_count": gate.failed_count,
    }
    return manifest, summary


def _rag_error_code(code: str) -> str:
    mapping = {
        "query_embedding_failed": "invalid_query_embedding",
        "generation_timeout": "runtime_timeout",
        "source_changed": "source_artifact_mismatch",
        "department_unavailable": "department_unavailable",
        "database_unavailable": "database_unavailable",
    }
    return mapping.get(code, code)


def _event(job: ClaimedEvaluationRun, action: str, result: str, reason: str) -> None:
    LOGGER.info(
        "evaluation_event action=%s result=%s reason=%s department_id=%s "
        "suite_id=%s run_id=%s case_count=%s",
        action,
        result,
        reason,
        job.department_id,
        job.suite_id,
        job.id,
        job.case_count,
    )
