from app.models import (
    ADAPTER_GOVERNANCE_ERROR_CODES,
    Adapter,
    AdapterDeploymentEvent,
    AdapterDeploymentOperation,
    AdapterReview,
    AdapterRollbackRetention,
    DepartmentAdapterDeployment,
)


def test_governance_authorities_are_separate_from_adapter_artifact_lifecycle():
    assert Adapter.__tablename__ == "adapters"
    status_sql = " ".join(
        str(constraint.sqltext)
        for constraint in Adapter.__table__.constraints
        if getattr(constraint, "name", "") == "ck_adapter_status"
    )
    assert status_sql
    assert not {"approved", "promoted", "superseded", "rejected"}.intersection(status_sql)
    assert not {"review_status", "deployment_status", "rollback_status"}.intersection(
        Adapter.__table__.columns.keys()
    )


def test_phase12_3_authorities_and_content_free_columns_exist():
    assert AdapterReview.__tablename__ == "adapter_reviews"
    assert DepartmentAdapterDeployment.__tablename__ == "department_adapter_deployments"
    assert AdapterDeploymentOperation.__tablename__ == "adapter_deployment_operations"
    assert AdapterDeploymentEvent.__tablename__ == "adapter_deployment_events"
    assert AdapterRollbackRetention.__tablename__ == "adapter_rollback_retentions"
    status_sql = " ".join(
        str(constraint.sqltext)
        for constraint in AdapterReview.__table__.constraints
        if getattr(constraint, "name", "") == "ck_adapter_review_status"
    )
    assert all(value in status_sql for value in ("pending", "approved", "rejected", "archived"))
    forbidden = {
        "question",
        "answer",
        "prompt",
        "evidence",
        "vector",
        "adapter_bytes",
        "path",
        "secret",
        "token",
    }
    for model in (
        AdapterReview,
        DepartmentAdapterDeployment,
        AdapterDeploymentOperation,
        AdapterDeploymentEvent,
        AdapterRollbackRetention,
    ):
        assert not forbidden.intersection(model.__table__.columns.keys())


def test_phase12_3_deployment_authority_keeps_exact_evaluation_scope():
    assert "suite_id" in DepartmentAdapterDeployment.__table__.columns
    assert "suite_id" in AdapterDeploymentEvent.__table__.columns
    assert "fk_adapter_deployment_evaluation_scope" in {
        constraint.name
        for constraint in DepartmentAdapterDeployment.__table__.foreign_key_constraints
    }
    assert "fk_adapter_deployment_operation_target_retention_exact" in {
        constraint.name
        for constraint in AdapterDeploymentOperation.__table__.foreign_key_constraints
    }
    assert "uq_adapter_rollback_retention_adapter_scope" in {
        constraint.name
        for constraint in AdapterRollbackRetention.__table__.constraints
        if constraint.name
    }


def test_governance_error_codes_are_closed_and_safe():
    assert "database_unavailable" in ADAPTER_GOVERNANCE_ERROR_CODES
    assert all(" " not in code and "\n" not in code for code in ADAPTER_GOVERNANCE_ERROR_CODES)
