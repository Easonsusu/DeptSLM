"""Command-line lifecycle for the Phase 6 Qdrant indexing worker."""

from __future__ import annotations

import argparse
import logging
import signal
from threading import Event
from uuid import uuid4

from app.database import create_database_engine, create_session_factory
from deptslm_worker.index_pipeline import process_index_job
from deptslm_worker.index_queue import IndexQueueError, claim_next
from deptslm_worker.index_settings import IndexConfigurationError, IndexSettings
from deptslm_worker.qdrant_adapter import DepartmentQdrant, QdrantBoundaryError

STOP = Event()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DeptSLM vector-indexing worker")
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
        settings = IndexSettings.from_environment()
        qdrant = DepartmentQdrant(
            settings.qdrant_url,
            settings.qdrant_api_key,
            settings.qdrant_timeout_seconds,
        )
        qdrant.verify_collection()
    except (IndexConfigurationError, QdrantBoundaryError) as error:
        logging.error("indexer configuration error: %s", error)
        return 2
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    worker_id = uuid4()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        while not STOP.is_set():
            try:
                job = claim_next(factory, worker_id, settings.lease_seconds)
            except IndexQueueError:
                logging.error("indexing queue unavailable")
                if args.once:
                    return 3
                STOP.wait(settings.poll_seconds)
                continue
            if job is not None:
                logging.info(
                    "vector_index_event action=claim result=allowed "
                    "reason=claim_acquired department_id=%s resource_id=%s",
                    job.department_id,
                    job.id,
                )
                process_index_job(factory, settings, qdrant, job, STOP.is_set)
            if args.once:
                return 0
            STOP.wait(settings.poll_seconds)
        return 0
    finally:
        qdrant.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
