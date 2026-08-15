import inspect
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.adapter_governance_services import start_review, transition_review
from app.routes import router
from app.schemas import (
    AdapterDeploymentCancelRequest,
    AdapterPromotionRequest,
    AdapterReviewRequest,
    AdapterRollbackRequest,
)


def test_phase12_3_routes_are_metadata_only_and_department_scoped():
    paths = {route.path for route in router.routes if "adapter-governance" in route.tags}
    assert any(path.endswith("/review") for path in paths)
    assert any(path.endswith("/reviews") for path in paths)
    assert any(path.endswith("/promote") for path in paths)
    assert any(path.endswith("/rollback") for path in paths)
    assert any(path.endswith("/adapter-deployment") for path in paths)
    assert any(path.endswith("/adapter-deployment/events") for path in paths)
    assert not any("search" in path or "rag" in path for path in paths)


def test_phase12_3_request_schemas_are_closed():
    review = AdapterReviewRequest(
        action="start",
        evaluation_id=uuid4(),
        expected_adapter_version=1,
        expected_evaluation_version=1,
    )
    assert review.action == "start"
    with pytest.raises(ValidationError):
        AdapterReviewRequest(
            action="approve",
            review_id=uuid4(),
            expected_adapter_version=1,
            expected_review_version=1,
            question="must not persist",
        )
    with pytest.raises(ValidationError):
        AdapterPromotionRequest(
            review_id=uuid4(),
            expected_adapter_version=1,
            expected_review_version=1,
            expected_deployment_version=0,
            adapter_path="forbidden",
        )
    with pytest.raises(ValidationError):
        AdapterRollbackRequest(
            target="base",
            expected_deployment_version=0,
            runtime_url="forbidden",
        )
    assert AdapterDeploymentCancelRequest(expected_version=1).expected_version == 1


def test_review_mutations_require_the_path_adapter_selector():
    assert "adapter_id" in inspect.signature(start_review).parameters
    assert "adapter_id" in inspect.signature(transition_review).parameters
    source = inspect.getsource(start_review) + inspect.getsource(transition_review)
    assert "AdapterEvaluationRun.adapter_id == adapter_id" in source
    assert "AdapterReview.adapter_id == adapter_id" in source
