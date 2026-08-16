"""CI-only smoke for the real Phase 12.4 production runtime container.

The fixture is assembled from the already-approved synthetic registry final
created by the Phase 12.3 container smoke.  It contains no model weights and
uses the production HTTP supervisor with the explicit test-only fake provider.
All request/response files live below ``RUNNER_TEMP`` rather than the checkout.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session

from alembic import command
from app.adapter_deployment_authority import load_runtime_target
from app.adapter_runtime_contract import ADAPTER_RUNTIME_CONTRACT_VERSION
from app.database import create_database_engine
from app.models import DepartmentAdapterDeployment
from app.rag_domain import ANSWER_CONTRACT_VERSION, PROMPT_VERSION


def _database_url() -> str:
    value = os.getenv("DATABASE_TEST_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_TEST_URL is required")
    return value


def _paths() -> tuple[Path, Path, Path, Path]:
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    return (
        runner_temp / "deptslm-adapter-runtime-request.json",
        runner_temp / "deptslm-adapter-runtime-request-b.json",
        runner_temp / "deptslm-adapter-runtime-response.json",
        runner_temp / "deptslm-adapter-runtime-response-b.json",
    )


def _payload(target) -> dict[str, object]:
    value = {
        "operation": "generate",
        "target": "adapter",
        **target.adapter_request_fields(),
        "question": "Synthetic production adapter runtime smoke?",
        "evidence": [{"source_id": "S1", "text": "Synthetic approved evidence."}],
        "prompt_version": PROMPT_VERSION,
        "answer_contract_version": ANSWER_CONTRACT_VERSION,
    }
    value["runtime_contract_version"] = ADAPTER_RUNTIME_CONTRACT_VERSION
    return value


def prepare() -> None:
    root = Path(os.environ["DEPTSLM_DATA_DIR"]).resolve()
    if not root.is_dir():
        raise RuntimeError("runtime data directory is unavailable")
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_database_engine(_database_url())
    try:
        with Session(engine) as session:
            deployment = session.scalar(
                select(DepartmentAdapterDeployment)
                .where(DepartmentAdapterDeployment.target_kind == "adapter")
                .order_by(DepartmentAdapterDeployment.updated_at.desc())
            )
            if deployment is None:
                raise RuntimeError("synthetic adapter deployment is unavailable")
            target = load_runtime_target(session, deployment.department_id, lock=False)
            if target.target_kind != "adapter":
                raise RuntimeError("synthetic deployment did not resolve to adapter")
    finally:
        engine.dispose()
    request_path, request_b_path, _response_path, _response_b_path = _paths()
    request_path.write_text(json.dumps(_payload(target), sort_keys=True), encoding="utf-8")

    # A changed deployment authority must force a new child/session.  The
    # synthetic request remains otherwise identical and still points at the
    # exact verified registry final, so the fake provider can serve it safely.
    switched = replace(
        target,
        deployment_version=target.deployment_version + 1,
        deployment_row_version=(target.deployment_row_version or 0) + 1,
    )
    request_b_path.write_text(json.dumps(_payload(switched), sort_keys=True), encoding="utf-8")


def verify() -> None:
    _request_path, _request_b_path, response_path, response_b_path = _paths()
    for path in (response_path, response_b_path):
        value = json.loads(path.read_text(encoding="utf-8"))
        if set(value) != {"status", "answer", "citations", "served_target_fingerprint"}:
            raise RuntimeError("adapter runtime response contract failed")
        if value["status"] != "answered" or value["citations"] != ["S1"]:
            raise RuntimeError("adapter runtime fake generation failed")
        if not isinstance(value["answer"], str) or "[S1]" not in value["answer"]:
            raise RuntimeError("adapter runtime citation response failed")
    first = json.loads(response_path.read_text(encoding="utf-8"))
    second = json.loads(response_b_path.read_text(encoding="utf-8"))
    if first["served_target_fingerprint"] == second["served_target_fingerprint"]:
        raise RuntimeError("target change did not retire the prior runtime session")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "verify"}:
        raise SystemExit("usage: phase12_4_container_smoke.py prepare|verify")
    (prepare if sys.argv[1] == "prepare" else verify)()
