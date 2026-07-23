"""Dedicated command-line worker for Phase 9 evaluation runs."""

from __future__ import annotations

import argparse
import logging
import signal
from threading import Event

from app.database import create_database_engine, create_session_factory
from app.evaluation_artifacts import EvaluationArtifactStore
from app.rag_runtime_client import RagRuntimeClient
from deptslm_worker.evaluation_pipeline import process_evaluation_run
from deptslm_worker.evaluation_queue import (
    EvaluationQueueError,
    claim_next,
)
from deptslm_worker.evaluation_settings import (
    EvaluationConfigurationError,
    EvaluationSettings,
)
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError

STOP = Event()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeptSLM Phase 9 evaluator worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="claim at most one run")
    mode.add_argument("--poll", action="store_true", help="poll continuously")
    return parser.parse_args()


def _request_stop(_signum, _frame) -> None:
    STOP.set()


def main() -> int:
    args = _arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = EvaluationSettings.from_environment()
        store = EvaluationArtifactStore(settings.data_dir)
        qdrant = DepartmentQdrant(
            settings.rag.qdrant_url,
            settings.rag.qdrant_api_key,
            settings.rag.qdrant_timeout_seconds,
        )
        qdrant.verify_collection()
        runtime = RagRuntimeClient(
            settings.rag.runtime_url,
            settings.rag.runtime_token,
            min(
                settings.rag.request_timeout_seconds,
                settings.operation_timeout_seconds,
            ),
        )
    except (
        EvaluationConfigurationError,
        QdrantBoundaryError,
    ) as error:
        logging.error("evaluator configuration error: %s", error)
        return 2
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        while not STOP.is_set():
            try:
                job = claim_next(
                    factory,
                    settings.worker_id,
                    settings.lease_seconds,
                    settings.code_revision,
                )
            except EvaluationQueueError:
                logging.error("evaluation queue unavailable")
                if args.once:
                    return 3
                STOP.wait(settings.poll_seconds)
                continue
            if job is not None:
                logging.info(
                    "evaluation_event action=claim result=allowed "
                    "reason=claim_acquired department_id=%s suite_id=%s run_id=%s",
                    job.department_id,
                    job.suite_id,
                    job.id,
                )
                process_evaluation_run(
                    factory,
                    settings,
                    store,
                    runtime,
                    qdrant,
                    job,
                    STOP.is_set,
                )
            if args.once:
                return 0
            STOP.wait(settings.poll_seconds)
        return 0
    finally:
        qdrant.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
