from decimal import Decimal
from uuid import uuid4

import pytest

from app.adapter_evaluation_domain import (
    ADAPTER_EVALUATION_GATE_POLICY_VERSION,
    ADAPTER_EVALUATION_SEED_POLICY_VERSION,
    AdapterEvaluationTarget,
    compute_metric_deltas,
    derive_adapter_evaluation_case_seed,
    evaluate_adapter_gates,
)
from app.evaluation_domain import AggregateMetrics, QualityGates


def _metrics(value: str) -> AggregateMetrics:
    number = Decimal(value)
    return AggregateMetrics(
        retrieval_recall_at_5=number,
        retrieval_recall_at_10=number,
        retrieval_recall_at_20=number,
        retrieval_mrr_at_20=number,
        answer_status_accuracy=number,
        citation_precision=number,
        citation_recall=number,
        normalized_exact_match=number,
        character_f1=number,
        invalid_contract_rate=number,
    )


def _gates(value: str = "0.5000") -> QualityGates:
    number = Decimal(value)
    return QualityGates(
        retrieval_recall_at_5_min=number,
        retrieval_mrr_at_20_min=number,
        answer_status_accuracy_min=number,
        citation_precision_min=number,
        citation_recall_min=number,
        normalized_exact_match_min=number,
        character_f1_min=number,
        invalid_contract_rate_max=number,
    )


def test_adapter_pair_uses_fixed_seed_policy_and_same_case_seed():
    department_id = uuid4()
    adapter_id = uuid4()
    suite_id = uuid4()
    case_id = uuid4()

    first = derive_adapter_evaluation_case_seed(department_id, adapter_id, 3, suite_id, case_id)
    second = derive_adapter_evaluation_case_seed(department_id, adapter_id, 3, suite_id, case_id)

    assert ADAPTER_EVALUATION_SEED_POLICY_VERSION == "phase12-adapter-evaluation-seed-v1"
    assert first == second
    assert 0 <= first <= (1 << 63) - 1


def test_metric_deltas_are_exact_decimals_without_float_conversion():
    baseline = _metrics("0.1000")
    candidate = _metrics("0.3000")

    deltas = compute_metric_deltas(baseline, candidate)

    assert deltas["retrieval_recall_at_5"] == Decimal("0.2000")
    assert all(isinstance(value, Decimal) for value in deltas.values())


def test_gate_results_are_independent_and_gate_failure_is_evidence_only():
    baseline_metrics = _metrics("0.6000")
    candidate_metrics = _metrics("0.4000")
    baseline_metrics = AggregateMetrics(
        **{**baseline_metrics.as_dict(), "invalid_contract_rate": Decimal("0.1000")}
    )
    candidate_metrics = AggregateMetrics(
        **{**candidate_metrics.as_dict(), "invalid_contract_rate": Decimal("0.6000")}
    )
    baseline = evaluate_adapter_gates(baseline_metrics, _gates())
    candidate = evaluate_adapter_gates(candidate_metrics, _gates())

    assert baseline.passed is True
    assert candidate.passed is False
    assert candidate.failed_count == 8
    assert ADAPTER_EVALUATION_GATE_POLICY_VERSION == "phase9-quality-gates-v1"


def test_target_enum_is_closed_to_baseline_and_candidate():
    assert {item.value for item in AdapterEvaluationTarget} == {"baseline", "candidate"}
    with pytest.raises(ValueError):
        AdapterEvaluationTarget("fallback")
