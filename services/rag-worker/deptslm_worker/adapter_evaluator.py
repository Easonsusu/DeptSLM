"""Dedicated Phase 12.2 adapter-evaluation worker command."""

from __future__ import annotations

import argparse
import logging
import signal
from threading import Event
from uuid import uuid4

from app.adapter_evaluation_artifacts import AdapterEvaluationArtifactStore
from app.adapter_evaluation_queue import AdapterEvaluationQueueError, claim_next
from app.database import create_database_engine, create_session_factory
from deptslm_worker.adapter_evaluation_pipeline import process_adapter_evaluation_run
from deptslm_worker.adapter_evaluation_settings import (
    AdapterEvaluationSettings,
    EvaluationConfigurationError,
)

STOP = Event()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeptSLM Phase 12.2 adapter evaluator")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--poll", action="store_true")
    return parser.parse_args()


def _request_stop(_signum, _frame) -> None:
    STOP.set()


def main() -> int:
    args = _arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = AdapterEvaluationSettings.from_environment()
    except EvaluationConfigurationError as error:
        logging.error("adapter evaluator configuration error: %s", error)
        return 2
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    engine = create_database_engine(settings.evaluation.database_url)
    factory = create_session_factory(engine)
    store = AdapterEvaluationArtifactStore(settings.evaluation.data_dir)
    try:
        while not STOP.is_set():
            try:
                job = claim_next(
                    factory,
                    uuid4(),
                    settings.evaluation.lease_seconds,
                    settings.evaluation.code_revision,
                )
            except AdapterEvaluationQueueError:
                logging.error("adapter evaluation queue unavailable")
                if args.once:
                    return 3
                STOP.wait(settings.evaluation.poll_seconds)
                continue
            if job is not None:
                process_adapter_evaluation_run(factory, settings, store, job, STOP.is_set)
            if args.once:
                return 0
            STOP.wait(settings.evaluation.poll_seconds)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
