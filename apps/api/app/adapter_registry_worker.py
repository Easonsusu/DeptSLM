"""One-shot or polling Phase 12.1C registry worker entrypoint."""

from __future__ import annotations

import argparse
import os
import re
import signal
import threading
from pathlib import Path
from uuid import UUID

from app.adapter_registry_queue import (
    AdapterRegistryQueueError,
    claim_next_adapter,
    process_adapter_registry,
)
from app.database import create_database_engine, create_session_factory

_REVISION = re.compile(r"^[0-9a-f]{40}$")


def _positive(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 3600:
        raise ValueError("Adapter registry worker timing is invalid")
    return int(raw)


def _settings() -> tuple[str, Path, UUID, int, int, int, str]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
    raw_worker = os.getenv("DEPTSLM_ADAPTER_REGISTRY_WORKER_ID", "").strip()
    revision = os.getenv("DEPTSLM_ADAPTER_REGISTRY_CODE_REVISION", "").strip()
    if not database_url.startswith("postgresql+psycopg://") or not raw_data_dir:
        raise ValueError("Adapter registry worker configuration is invalid")
    data_dir = Path(raw_data_dir).expanduser()
    if (
        not data_dir.is_absolute()
        or not data_dir.is_dir()
        or not (data_dir / "adapters" / "imports").is_dir()
        or not (data_dir / "adapters" / "registry").is_dir()
        or not (data_dir / "adapters" / ".staging" / "registry").is_dir()
    ):
        raise ValueError("Adapter registry worker storage is unavailable")
    try:
        worker_id = UUID(raw_worker)
    except ValueError as error:
        raise ValueError("Adapter registry worker identifier is invalid") from error
    if worker_id.int == 0 or _REVISION.fullmatch(revision) is None:
        raise ValueError("Adapter registry worker configuration is invalid")
    return (
        database_url,
        data_dir,
        worker_id,
        _positive("DEPTSLM_ADAPTER_REGISTRY_LEASE_SECONDS", "300"),
        _positive("DEPTSLM_ADAPTER_REGISTRY_POLL_SECONDS", "5"),
        _positive("DEPTSLM_ADAPTER_REGISTRY_OPERATION_SECONDS", "600"),
        revision,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.adapter_registry_worker")
    parser.add_argument("--poll", action="store_true")
    args = parser.parse_args(argv)
    try:
        database_url, data_dir, worker_id, lease, poll, operation, revision = _settings()
    except ValueError as error:
        print(str(error), file=os.sys.stderr)
        return 1
    engine = create_database_engine(database_url)
    factory = create_session_factory(engine)
    shutdown = threading.Event()

    def request_shutdown(_signal: int, _frame: object) -> None:
        shutdown.set()

    previous_term = signal.signal(signal.SIGTERM, request_shutdown)
    previous_int = signal.signal(signal.SIGINT, request_shutdown)
    try:
        while not shutdown.is_set():
            try:
                claim = claim_next_adapter(factory, worker_id, lease, revision)
                if claim is not None:
                    process_adapter_registry(
                        factory,
                        data_dir=data_dir,
                        claim=claim,
                        lease_seconds=lease,
                        operation_seconds=operation,
                        should_stop=shutdown.is_set,
                    )
            except AdapterRegistryQueueError as error:
                print(f"adapter-registry-worker queue failure: {error.code}", file=os.sys.stderr)
            if not args.poll:
                return 0
            shutdown.wait(poll)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "_settings"]
