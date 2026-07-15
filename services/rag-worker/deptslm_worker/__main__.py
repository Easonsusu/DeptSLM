"""Command-line lifecycle for the DeptSLM extraction worker."""

from __future__ import annotations

import argparse
import logging
import signal
from threading import Event
from uuid import uuid4

from app.database import create_database_engine, create_session_factory
from deptslm_worker.pipeline import process_job
from deptslm_worker.queue import QueueError, claim_next
from deptslm_worker.settings import WorkerConfigurationError, WorkerSettings

STOP = Event()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeptSLM document extraction worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="claim at most one job")
    mode.add_argument("--poll", action="store_true", help="poll continuously")
    return parser.parse_args()


def _request_stop(_signum, _frame) -> None:
    STOP.set()


def main() -> int:
    args = _arguments()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        settings = WorkerSettings.from_environment()
    except WorkerConfigurationError as error:
        logging.error("worker configuration error: %s", error)
        return 2
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    worker_id = uuid4()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        while not STOP.is_set():
            try:
                job = claim_next(factory, worker_id, settings.extraction_lease_seconds)
            except QueueError:
                logging.error("worker queue unavailable")
                if args.once:
                    return 3
                _wait_or_stop(settings.worker_poll_seconds)
                continue
            if job is not None:
                process_job(factory, settings, job, STOP.is_set)
            if args.once:
                return 0
            if _wait_or_stop(settings.worker_poll_seconds):
                break
        return 0
    finally:
        engine.dispose()


def _wait_or_stop(seconds: int) -> int:
    STOP.wait(seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
