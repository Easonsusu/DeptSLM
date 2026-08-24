from __future__ import annotations

import os
from pathlib import Path

from .ipc import TrainingRuntimeServer
from .runtime import TrainingRuntime


def main() -> int:
    runtime = TrainingRuntime()
    socket_path = Path(
        os.getenv("DEPTSLM_TRAINING_RUNTIME_SOCKET", "/run/deptslm/training-runtime.sock")
    )
    TrainingRuntimeServer(socket_path, runtime.token, runtime.handle).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
