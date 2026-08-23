"""One-shot/polling supervisor for the Phase 14.2 execution queue.

The control-plane worker has no model cache or LlamaFactory dependency.  It
uses the private Unix runtime only when the reviewed socket, token, lock, and
environment fingerprints are configured; otherwise it fails closed with a
safe ``runtime_unavailable`` result.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from app.database import create_database_engine, create_session_factory
from app.training_execution_queue import claim_next_training_execution, process_training_execution
from app.training_execution_runtime import UnixTrainingRuntimeClient


def main() -> int:
    parser = argparse.ArgumentParser(description="DeptSLM Phase 14.2 execution worker")
    parser.add_argument("--poll", action="store_true")
    args = parser.parse_args()
    raw_data_dir = os.getenv("DEPTSLM_DATA_DIR", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not raw_data_dir or not Path(raw_data_dir).is_absolute() or not database_url:
        raise SystemExit(
            "training-execution-worker requires external DEPTSLM_DATA_DIR and DATABASE_URL"
        )
    factory = create_session_factory(create_database_engine(database_url))
    worker_id = uuid4()
    revision = os.getenv("DEPTSLM_TRAINING_EXECUTION_CODE_REVISION", "")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise SystemExit("training-execution-worker requires a reviewed 40-character code revision")
    runtime = None
    socket_path = os.getenv("DEPTSLM_TRAINING_RUNTIME_SOCKET", "").strip()
    runtime_token = os.getenv("DEPTSLM_TRAINING_RUNTIME_TOKEN", "")
    if socket_path or runtime_token:
        if not socket_path or not runtime_token:
            raise SystemExit(
                "training-execution-worker requires the private training runtime IPC boundary"
            )
        runtime = UnixTrainingRuntimeClient(socket_path, runtime_token)
    while True:
        claim = claim_next_training_execution(factory, worker_id, 300, revision)
        if claim is not None:
            process_training_execution(factory, Path(raw_data_dir), claim, runtime=runtime)
        if not args.poll:
            return 0
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
