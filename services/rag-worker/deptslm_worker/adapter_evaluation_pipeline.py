"""Phase 12.2 paired evaluation worker over shared Phase 7/9 policy."""

from __future__ import annotations

import time
from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adapter_evaluation_artifacts import AdapterEvaluationArtifactStore
from app.adapter_evaluation_domain import compute_metric_deltas
from app.adapter_evaluation_policy import (
    AdapterEvaluationLaneError,
    execute_paired_rag_case,
)
from app.adapter_evaluation_queue import (
    AdapterEvaluationQueueError,
    ClaimedAdapterEvaluation,
    fail_owned,
    finalize_success,
    renew_lease,
    require_live_claim,
)
from app.authorization import DepartmentScope
from app.evaluation_artifacts import _score_value
from app.evaluation_domain import (
    EvaluationContractError,
    aggregate_metrics,
    evaluate_gates,
    score_case,
)
from app.evaluation_suites import capture_canonical_suite_authority
from app.models import AdapterEvaluationRun, EvaluationSuite
from app.rag_domain import RagContractError
from app.rag_runtime_client import RagRuntimeClient
from deptslm_worker.adapter_evaluation_runtime import AdapterEvaluationRuntimeClient
from deptslm_worker.adapter_evaluation_settings import AdapterEvaluationSettings
from deptslm_worker.qdrant_adapter import DepartmentQdrant


def process_adapter_evaluation_run(
    factory: sessionmaker[Session],
    settings: AdapterEvaluationSettings,
    store: AdapterEvaluationArtifactStore,
    job: ClaimedAdapterEvaluation,
    should_stop: Callable[[], bool],
) -> bool:
    """Process one exact pair; all answers/evidence remain transient."""

    base = settings.evaluation
    qdrant = None
    try:
        require_live_claim(factory, job)
        _cleanup_stale_attempt(store, job)
        renew_lease(factory, job, settings.evaluation.lease_seconds)
        require_live_claim(factory, job)
        with factory() as session:
            run = session.execute(
                select(AdapterEvaluationRun).where(
                    AdapterEvaluationRun.id == job.id,
                    AdapterEvaluationRun.department_id == job.department_id,
                )
            ).scalar_one_or_none()
            suite = session.execute(
                select(EvaluationSuite).where(
                    EvaluationSuite.id == job.suite_id,
                    EvaluationSuite.department_id == job.department_id,
                )
            ).scalar_one_or_none()
            if run is None or suite is None:
                raise AdapterEvaluationQueueError("database_unavailable")
            scope = DepartmentScope(job.department_id)
            suite_cases = _load_suite_cases(
                base.data_dir,
                scope,
                suite.id,
                suite.artifact_manifest_sha256,
                suite.canonical_cases_sha256,
                suite.canonical_cases_byte_size,
                factory=factory,
                job=job,
                heartbeat_seconds=settings.evaluation.heartbeat_seconds,
                lease_seconds=settings.evaluation.lease_seconds,
                should_stop=should_stop,
            )
            adapter_fields = {
                "registry_publication_attempt_id": run.registry_publication_attempt_id,
                "registry_attempt_number": run.registry_attempt_number,
                "registry_manifest_sha256": run.registry_manifest_sha256,
                "adapter_config_sha256": run.registry_adapter_config_sha256,
                "adapter_config_byte_size": run.registry_adapter_config_byte_size,
                "adapter_model_sha256": run.registry_adapter_model_sha256,
                "adapter_model_byte_size": run.registry_adapter_model_byte_size,
            }
        suite_authority = capture_canonical_suite_authority(
            factory,
            base.data_dir,
            scope,
            suite_cases,
        )
        if should_stop():
            raise AdapterEvaluationQueueError("cancelled")
        renew_lease(factory, job, settings.evaluation.lease_seconds)
        if len(suite_cases) != job_case_count(factory, job):
            raise AdapterEvaluationQueueError("suite_authority_changed")
        baseline_runtime = RagRuntimeClient(
            base.rag.runtime_url,
            base.rag.runtime_token,
            base.rag.request_timeout_seconds,
        )
        candidate_runtime = AdapterEvaluationRuntimeClient(
            settings.candidate_runtime_url,
            settings.candidate_runtime_token,
            base.rag.request_timeout_seconds,
            department_id=job.department_id,
            adapter_id=job.adapter_id,
            adapter_version=run.adapter_version,
            **adapter_fields,
        )
        qdrant = DepartmentQdrant(
            base.rag.qdrant_url,
            base.rag.qdrant_api_key,
            base.rag.qdrant_timeout_seconds,
        )
        baseline_scores = []
        candidate_scores = []
        case_rows = []
        last_heartbeat = time.monotonic()

        def stage_checkpoint(_stage: str) -> None:
            nonlocal last_heartbeat
            if should_stop():
                raise AdapterEvaluationQueueError("cancelled")
            now = time.monotonic()
            if now - last_heartbeat >= settings.evaluation.heartbeat_seconds:
                renew_lease(factory, job, settings.evaluation.lease_seconds)
                last_heartbeat = now

        for case in suite_cases:
            if should_stop():
                raise AdapterEvaluationQueueError("cancelled")
            renew_lease(factory, job, settings.evaluation.lease_seconds)
            expected, relevant, accepted = _case_contract(case)
            pair = execute_paired_rag_case(
                factory,
                base.rag,
                base.data_dir,
                scope,
                case["question"],
                department_id=job.department_id,
                adapter_id=job.adapter_id,
                adapter_version=run.adapter_version,
                suite_id=job.suite_id,
                case_id=UUID(case["case_id"]),
                baseline_runtime=baseline_runtime,
                candidate_runtime=candidate_runtime,
                qdrant=qdrant,
                stage_callback=stage_checkpoint,
            )
            for target, outcome, scores in (
                ("baseline", pair.baseline, baseline_scores),
                ("candidate", pair.candidate, candidate_scores),
            ):
                score = score_case(
                    case_id=UUID(case["case_id"]),
                    expected_status=expected,
                    relevant_chunk_ids=relevant,
                    accepted_answers=accepted,
                    actual_status=outcome.status,
                    generated_answer=outcome.answer,
                    authorized_candidate_ids=outcome.retrieval_trace.authorized_candidate_ids,
                    cited_chunk_ids=outcome.cited_chunk_ids,
                    answer_contract_valid=outcome.answer_contract_valid,
                    error_code=outcome.error_code,
                )
                scores.append(score)
                row = _score_value(score)
                row["target"] = target
                row["retrieval_candidate_count"] = (
                    outcome.retrieval_trace.candidate_count
                )
                case_rows.append(row)
            renew_lease(factory, job, settings.evaluation.lease_seconds)
        baseline_metrics = aggregate_metrics(baseline_scores)
        candidate_metrics = aggregate_metrics(candidate_scores)
        gates = _gates(run)
        baseline_gate = evaluate_gates(baseline_metrics, gates)
        candidate_gate = evaluate_gates(candidate_metrics, gates)
        deltas = compute_metric_deltas(baseline_metrics, candidate_metrics)
        manifest = {
            "artifact_contract_version": run.artifact_contract_version,
            "department_id": str(job.department_id),
            "evaluation_id": str(job.id),
            "adapter_id": str(job.adapter_id),
            "adapter_version": run.adapter_version,
            "suite_id": str(job.suite_id),
            "publication_attempt_id": str(job.publication_attempt_id),
            "attempt_number": job.attempt_number,
            "base_seed": run.base_seed,
            "baseline_lane_id": str(
                uuid5(NAMESPACE_URL, f"deptslm:adapter-eval:{job.id}:baseline")
            ),
            "candidate_lane_id": str(
                uuid5(NAMESPACE_URL, f"deptslm:adapter-eval:{job.id}:candidate")
            ),
            "registry_publication_attempt_id": str(run.registry_publication_attempt_id),
            "registry_attempt_number": run.registry_attempt_number,
            "registry_manifest_sha256": run.registry_manifest_sha256,
            "adapter_config_sha256": run.registry_adapter_config_sha256,
            "adapter_config_byte_size": run.registry_adapter_config_byte_size,
            "adapter_model_sha256": run.registry_adapter_model_sha256,
            "adapter_model_byte_size": run.registry_adapter_model_byte_size,
            "base_model_id": run.base_model_id,
            "base_model_revision": run.base_model_revision,
            "runner_contract_version": run.runner_contract_version,
            "metric_contract_version": run.metric_contract_version,
            "gate_policy_version": run.gate_policy_version,
            "seed_policy_version": run.seed_policy_version,
            "code_revision": run.code_revision,
        }
        summary = {
            "baseline_metrics": baseline_metrics.as_dict(),
            "candidate_metrics": candidate_metrics.as_dict(),
            "metric_deltas": deltas,
            "baseline_gate_status": "passed" if baseline_gate.passed else "failed",
            "candidate_gate_status": "passed" if candidate_gate.passed else "failed",
            "baseline_failed_gate_count": baseline_gate.failed_count,
            "candidate_failed_gate_count": candidate_gate.failed_count,
        }
        staged = store.stage_run(
            DepartmentScope(job.department_id),
            job.id,
            job.publication_attempt_id,
            manifest=manifest,
            summary=summary,
            case_rows=case_rows,
        )
        renew_lease(factory, job, settings.evaluation.lease_seconds)
        require_live_claim(factory, job)
        published = store.publish(staged)
        published = store.verify_published(
            DepartmentScope(job.department_id),
            job.id,
            job.publication_attempt_id,
            expected_manifest=staged.manifest,
            expected_files=dict(published.files),
        )
        files = dict(published.files)
        candidate_runtime.verify_target()
        renew_lease(factory, job, settings.evaluation.lease_seconds)
        require_live_claim(factory, job)
        finalize_success(
            factory,
            job,
            baseline_metrics=baseline_metrics,
            candidate_metrics=candidate_metrics,
            baseline_gate=baseline_gate,
            candidate_gate=candidate_gate,
            result_manifest_sha256=files["manifest.json"].sha256,
            result_summary_sha256=files["summary.json"].sha256,
            case_results_sha256=files["case_results.jsonl"].sha256,
            case_results_byte_size=files["case_results.jsonl"].byte_size,
            case_rows=tuple(case_rows),
            data_dir=base.data_dir,
            suite_cases=suite_cases,
            suite_authority=suite_authority,
            result_store=store,
            result_manifest=staged.manifest,
            result_files=dict(published.files),
        )
        return True
    except AdapterEvaluationQueueError as error:
        _cleanup_attempt_stage(store, job)
        try:
            fail_owned(factory, job, error.code)
        except AdapterEvaluationQueueError:
            pass
        return False
    except AdapterEvaluationLaneError as error:
        _cleanup_attempt_stage(store, job)
        try:
            fail_owned(factory, job, error.code)
        except AdapterEvaluationQueueError:
            pass
        return False
    except RagContractError as error:
        _cleanup_attempt_stage(store, job)
        safe = error.code
        if safe in {"runtime_unavailable", "generation_failed"}:
            safe = "candidate_runtime_unavailable"
        elif safe in {"generation_timeout"}:
            safe = "candidate_runtime_timeout"
        elif safe == "source_changed":
            safe = "source_artifact_mismatch"
        elif safe not in {
            "invalid_generation_response",
            "invalid_citation",
            "query_embedding_failed",
            "invalid_query_embedding",
            "qdrant_unavailable",
            "retrieval_authority_failed",
            "source_artifact_missing",
            "source_artifact_mismatch",
        }:
            safe = "database_unavailable"
        try:
            fail_owned(factory, job, safe)
        except AdapterEvaluationQueueError:
            pass
        return False
    except EvaluationContractError as error:
        _cleanup_attempt_stage(store, job)
        if error.code in {
            "suite_source_stale",
            "suite_artifact_mismatch",
            "suite_artifact_missing",
            "suite_contract_invalid",
            "artifact_reconciliation_failed",
        }:
            safe = "suite_authority_changed"
        elif error.code in {"result_artifact_mismatch", "result_publication_failed"}:
            safe = "result_publication_failed"
        elif error.code in AdapterEvaluationQueueError._codes:
            safe = error.code
        else:
            safe = "database_unavailable"
        try:
            fail_owned(factory, job, safe)
        except AdapterEvaluationQueueError:
            pass
        return False
    except Exception:
        _cleanup_attempt_stage(store, job)
        try:
            fail_owned(factory, job, "database_unavailable")
        except AdapterEvaluationQueueError:
            pass
        return False
    finally:
        if qdrant is not None:
            try:
                qdrant.close()
            except Exception:
                pass


def external_suite_cases(
    data_dir, scope, suite_id, manifest_sha256, cases_sha256, cases_byte_size
):
    from app.evaluation_artifacts import EvaluationArtifactStore

    store = EvaluationArtifactStore(data_dir)
    return store.iter_suite_cases(
        scope,
        suite_id,
        manifest_sha256=manifest_sha256,
        cases_sha256=cases_sha256,
        cases_byte_size=cases_byte_size,
    )


def _load_suite_cases(
    data_dir,
    scope,
    suite_id,
    manifest_sha256,
    cases_sha256,
    cases_byte_size,
    *,
    factory,
    job,
    heartbeat_seconds: int,
    lease_seconds: int,
    should_stop: Callable[[], bool],
):
    """Scan the immutable suite with cancellation and lease checkpoints."""

    cases = []
    last_heartbeat = time.monotonic()
    for case in external_suite_cases(
        data_dir,
        scope,
        suite_id,
        manifest_sha256,
        cases_sha256,
        cases_byte_size,
    ):
        if should_stop():
            raise AdapterEvaluationQueueError("cancelled")
        now = time.monotonic()
        if now - last_heartbeat >= heartbeat_seconds:
            renew_lease(factory, job, lease_seconds)
            last_heartbeat = now
        cases.append(case)
    renew_lease(factory, job, lease_seconds)
    return tuple(cases)


def job_case_count(factory, job):
    with factory() as session:
        return session.scalar(
            select(AdapterEvaluationRun.case_count).where(
                AdapterEvaluationRun.id == job.id,
                AdapterEvaluationRun.department_id == job.department_id,
            )
        )


def _case_contract(case):
    if set(case) != {
        "case_id",
        "expected_status",
        "question",
        "relevant_sources",
        "accepted_answers",
    }:
        raise AdapterEvaluationQueueError("suite_authority_changed")
    expected = case.get("expected_status")
    sources = case.get("relevant_sources")
    accepted_values = case.get("accepted_answers")
    if (
        expected not in {"answered", "insufficient_information"}
        or not isinstance(case.get("question"), str)
        or not isinstance(sources, list)
        or not isinstance(accepted_values, list)
    ):
        raise AdapterEvaluationQueueError("suite_authority_changed")
    try:
        parsed_case_id = UUID(case["case_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise AdapterEvaluationQueueError("suite_authority_changed") from error
    if parsed_case_id.int == 0:
        raise AdapterEvaluationQueueError("suite_authority_changed")
    try:
        relevant = tuple(UUID(value["chunk_id"]) for value in sources)
    except (KeyError, TypeError, ValueError) as error:
        raise AdapterEvaluationQueueError("suite_authority_changed") from error
    if any(item.int == 0 for item in relevant) or len(relevant) != len(set(relevant)):
        raise AdapterEvaluationQueueError("suite_authority_changed")
    if any(not isinstance(value, str) for value in accepted_values):
        raise AdapterEvaluationQueueError("suite_authority_changed")
    accepted = tuple(accepted_values)
    if expected == "answered" and not (
        1 <= len(relevant) <= 8 and 1 <= len(accepted) <= 8
    ):
        raise AdapterEvaluationQueueError("suite_authority_changed")
    if expected == "insufficient_information" and (relevant or accepted):
        raise AdapterEvaluationQueueError("suite_authority_changed")
    return expected, relevant, accepted


def _cleanup_attempt_stage(store, job: ClaimedAdapterEvaluation) -> None:
    try:
        store.cleanup_stage(
            DepartmentScope(job.department_id), job.id, job.publication_attempt_id
        )
    except Exception:
        pass
    try:
        store.cleanup_published(
            DepartmentScope(job.department_id), job.id, job.publication_attempt_id
        )
    except Exception:
        pass


def _cleanup_stale_attempt(store, job: ClaimedAdapterEvaluation) -> None:
    """Remove only the exact prior attempt left by an expired worker."""

    if job.stale_publication_attempt_id is None:
        return
    try:
        store.cleanup_stage(
            DepartmentScope(job.department_id), job.id, job.stale_publication_attempt_id
        )
        store.cleanup_published(
            DepartmentScope(job.department_id), job.id, job.stale_publication_attempt_id
        )
    except Exception as error:
        raise AdapterEvaluationQueueError("result_publication_failed") from error


def _gates(run):
    from app.evaluation_domain import QualityGates

    return QualityGates(
        retrieval_recall_at_5_min=run.retrieval_recall_at_5_min,
        retrieval_mrr_at_20_min=run.retrieval_mrr_at_20_min,
        answer_status_accuracy_min=run.answer_status_accuracy_min,
        citation_precision_min=run.citation_precision_min,
        citation_recall_min=run.citation_recall_min,
        normalized_exact_match_min=run.normalized_exact_match_min,
        character_f1_min=run.character_f1_min,
        invalid_contract_rate_max=run.invalid_contract_rate_max,
    )
