from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.adapter_evaluation_artifacts import AdapterEvaluationArtifactStore
from app.authorization import DepartmentScope
from app.evaluation_domain import EvaluationContractError


def test_adapter_evaluation_artifacts_use_closed_allowlist_and_external_namespace(tmp_path: Path):
    data = tmp_path / "data"
    for name in ("eval_results",):
        (data / name).mkdir(parents=True)
    store = AdapterEvaluationArtifactStore(data)
    scope = DepartmentScope(uuid4())
    evaluation_id = uuid4()
    attempt_id = uuid4()
    case_id = uuid4()
    metrics = {
        name: Decimal("0.5000")
        for name in (
            "retrieval_recall_at_5",
            "retrieval_recall_at_10",
            "retrieval_recall_at_20",
            "retrieval_mrr_at_20",
            "answer_status_accuracy",
            "citation_precision",
            "citation_recall",
            "normalized_exact_match",
            "character_f1",
            "invalid_contract_rate",
        )
    }
    attempt_id = uuid4()
    manifest = {
        "department_id": str(scope.value),
        "evaluation_id": str(evaluation_id),
        "adapter_id": str(uuid4()),
        "adapter_version": 1,
        "suite_id": str(uuid4()),
        "publication_attempt_id": str(attempt_id),
        "attempt_number": 1,
        "base_seed": 7,
        "baseline_lane_id": str(uuid4()),
        "candidate_lane_id": str(uuid4()),
        "base_model_id": "Qwen/Qwen3-0.6B",
        "base_model_revision": "c1899de289a04d12100db370d81485cdf75e47ca",
        "registry_publication_attempt_id": str(uuid4()),
        "registry_attempt_number": 1,
        "registry_manifest_sha256": "a" * 64,
        "adapter_config_sha256": "b" * 64,
        "adapter_config_byte_size": 1,
        "adapter_model_sha256": "c" * 64,
        "adapter_model_byte_size": 1,
        "runner_contract_version": "phase12-adapter-evaluation-v1",
        "metric_contract_version": "phase9-deterministic-metrics-v1",
        "gate_policy_version": "phase9-quality-gates-v1",
        "seed_policy_version": "phase12-adapter-evaluation-seed-v1",
        "code_revision": "d" * 40,
    }
    staged = store.stage_run(
        scope,
        evaluation_id,
        attempt_id,
        manifest=manifest,
        summary={
            "baseline_metrics": metrics,
            "candidate_metrics": metrics,
            "metric_deltas": metrics,
            "baseline_gate_status": "passed",
            "candidate_gate_status": "failed",
            "baseline_failed_gate_count": 0,
            "candidate_failed_gate_count": 1,
        },
        case_rows=[
            {
                "target": target,
                "case_id": case_id,
                "expected_status": "answered",
                "actual_status": "answered",
                "relevant_chunk_count": 1,
                "retrieval_candidate_count": 1,
                "retrieved_relevant_at_5": 1,
                "retrieved_relevant_at_10": 1,
                "retrieved_relevant_at_20": 1,
                "reciprocal_rank_at_20": Decimal("1"),
                "status_correct": True,
                "cited_count": 1,
                "cited_relevant_count": 1,
                "citation_precision": Decimal("1"),
                "citation_recall": Decimal("1"),
                "normalized_exact_match": Decimal("1"),
                "character_f1": Decimal("1"),
                "answer_contract_valid": True,
                "case_gate_passed": True,
                "error_code": None,
            }
            for target in ("baseline", "candidate")
        ],
    )
    published = store.publish(staged)
    assert {name for name, _digest in published.files} == {
        "manifest.json",
        "summary.json",
        "case_results.jsonl",
    }
    assert published.path.is_relative_to(data / "eval_results" / "adapter_runs")


def test_adapter_evaluation_artifacts_reject_contentful_fields(tmp_path: Path):
    data = tmp_path / "data"
    (data / "eval_results").mkdir(parents=True)
    store = AdapterEvaluationArtifactStore(data)
    with pytest.raises(EvaluationContractError):
        store.stage_run(
            DepartmentScope(uuid4()),
            uuid4(),
            uuid4(),
            manifest={"question": "secret"},
            summary={},
            case_rows=[],
        )
