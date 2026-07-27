"""Minimal supervised Phase 10 dataset-build worker entrypoint."""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from uuid import UUID

from app.database import create_database_engine, create_session_factory
from app.sft_queue import SftQueueError, claim_next, process_build

_REVISION = re.compile(r"^[0-9a-f]{40}$")


def _settings() -> tuple[str, Path, UUID, int, int, str]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
    raw_worker = os.getenv("DEPTSLM_SFT_WORKER_ID", "").strip()
    revision = os.getenv("DEPTSLM_SFT_CODE_REVISION", "").strip()
    if not database_url.startswith("postgresql+psycopg://") or not raw_data_dir:
        raise ValueError("SFT worker configuration is invalid")
    data_dir = Path(raw_data_dir)
    if not data_dir.is_absolute() or not (data_dir / "training_datasets").is_dir():
        raise ValueError("SFT worker storage is unavailable")
    try:
        worker_id = UUID(raw_worker)
    except ValueError as error:
        raise ValueError("SFT worker identifier is invalid") from error
    if worker_id.int == 0 or _REVISION.fullmatch(revision) is None:
        raise ValueError("SFT worker configuration is invalid")
    lease = _positive(os.getenv("DEPTSLM_SFT_LEASE_SECONDS", "300"))
    poll = _positive(os.getenv("DEPTSLM_SFT_POLL_SECONDS", "5"))
    return database_url, data_dir, worker_id, lease, poll, revision


def _positive(raw: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 3600:
        raise ValueError("SFT worker configuration is invalid")
    return int(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.sft_worker")
    parser.add_argument("--poll", action="store_true")
    args = parser.parse_args(argv)
    try:
        database_url, data_dir, worker_id, lease, poll, revision = _settings()
    except ValueError as error:
        print(str(error), file=os.sys.stderr)
        return 1
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    try:
        while True:
            try:
                job = claim_next(factory, worker_id, lease, revision)
                if job is not None:
                    process_build(factory, data_dir, job, lease_seconds=lease)
            except SftQueueError:
                pass
            if not args.poll:
                return 0
            time.sleep(poll)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
