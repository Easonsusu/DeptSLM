from app.adapter_evaluation_domain import (
    ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION,
    ADAPTER_EVALUATION_GATE_POLICY_VERSION,
    ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION,
    ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION,
    ADAPTER_EVALUATION_SEED_POLICY_VERSION,
)
from app.models import (
    AdapterEvaluationAttempt,
    AdapterEvaluationCaseResult,
    AdapterEvaluationEvidence,
    AdapterEvaluationRun,
)


def test_phase12_2_models_are_separate_content_free_entities():
    assert AdapterEvaluationRun.__tablename__ == "adapter_evaluation_runs"
    assert AdapterEvaluationAttempt.__tablename__ == "adapter_evaluation_attempts"
    assert AdapterEvaluationEvidence.__tablename__ == "adapter_evaluation_evidence"
    assert AdapterEvaluationCaseResult.__tablename__ == "adapter_evaluation_case_results"


def test_phase12_2_contract_versions_are_fixed():
    assert ADAPTER_EVALUATION_RUNNER_CONTRACT_VERSION.endswith("-v1")
    assert ADAPTER_EVALUATION_ARTIFACT_CONTRACT_VERSION.endswith("-v1")
    assert ADAPTER_EVALUATION_METRIC_CONTRACT_VERSION == "phase9-deterministic-metrics-v1"
    assert ADAPTER_EVALUATION_GATE_POLICY_VERSION == "phase9-quality-gates-v1"
    assert ADAPTER_EVALUATION_SEED_POLICY_VERSION.endswith("-v1")


def test_evidence_columns_do_not_include_content_fields():
    forbidden = {
        "question",
        "accepted_answer",
        "generated_answer",
        "prompt",
        "evidence_text",
        "vector",
        "adapter_bytes",
        "adapter_config",
        "path",
    }
    for model in (
        AdapterEvaluationRun,
        AdapterEvaluationAttempt,
        AdapterEvaluationEvidence,
        AdapterEvaluationCaseResult,
    ):
        assert not forbidden.intersection(model.__table__.columns.keys())


def test_metric_evidence_uses_decimal_columns():
    decimal_columns = {
        "retrieval_recall_at_5",
        "delta_retrieval_recall_at_5",
    }
    assert decimal_columns.issubset(AdapterEvaluationEvidence.__table__.columns.keys())
    assert all(
        "NUMERIC" in str(AdapterEvaluationEvidence.__table__.c[name].type).upper()
        for name in decimal_columns
    )
