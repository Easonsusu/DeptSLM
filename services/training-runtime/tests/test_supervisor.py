"""Process-group supervision tests using a test-only executable injection."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
from deptslm_training_runtime.supervisor import TrainingProcessSupervisor


def _child(path: Path, mode: str, marker: Path | None = None) -> Path:
    marker_literal = repr(str(marker)) if marker is not None else "None"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, subprocess, sys, time\n"
        f"mode = {mode!r}\n"
        f"marker = {marker_literal}\n"
        "if mode == 'success':\n"
        "    print('synthetic-child-success', flush=True)\n"
        "    raise SystemExit(0)\n"
        "if mode == 'nonzero':\n"
        "    print('synthetic-child-failure', flush=True)\n"
        "    raise SystemExit(7)\n"
        "if mode == 'flood':\n"
        "    sys.stdout.write('synthetic-log-' * 3000000)\n"
        "    sys.stdout.flush()\n"
        "    raise SystemExit(0)\n"
        "if mode == 'grandchild':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "    open(marker, 'w', encoding='ascii').write(str(child.pid))\n"
        "    time.sleep(60)\n"
        "if mode == 'ignore-term':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(60)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    path.chmod(stat.S_IRWXU)
    return path


def _descriptors(root: Path) -> list[int]:
    descriptors: list[int] = []
    root.mkdir(mode=0o700)
    for name in ("config", "input", "scratch", "logs", "output"):
        path = root / name
        path.mkdir(mode=0o700)
        descriptors.append(os.open(path, os.O_RDONLY | os.O_DIRECTORY))
    return descriptors


def _run(
    child: Path,
    root: Path,
    *,
    stop=lambda: False,
    startup: float = 2.0,
    wall: float = 5.0,
    term_grace: float = 0.2,
    kill_reap: float = 2.0,
):
    descriptors = _descriptors(root)
    try:
        return TrainingProcessSupervisor(
            executable=str(child),
            test_executable=True,
            startup_timeout_seconds=startup,
            wall_timeout_seconds=wall,
            term_grace_seconds=term_grace,
            kill_reap_seconds=kill_reap,
        ).run(
            config_fd=descriptors[0],
            input_fd=descriptors[1],
            scratch_fd=descriptors[2],
            logs_fd=descriptors[3],
            output_stage_fd=descriptors[4],
            environment={"PATH": "/usr/bin:/bin"},
            should_stop=stop,
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def test_process_group_success_and_nonzero_exit(tmp_path: Path) -> None:
    success = _child(tmp_path / "success", "success")
    result = _run(success, tmp_path / "success-root")
    assert result.classification == "execution_succeeded"
    assert result.error_code is None

    failure = _child(tmp_path / "nonzero", "nonzero")
    result = _run(failure, tmp_path / "failure-root")
    assert result.classification == "execution_failed"
    assert result.error_code == "child_failed"


def test_cancellation_and_wall_timeout_reap_the_child(tmp_path: Path) -> None:
    child = _child(tmp_path / "hang", "hang")
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    result = _run(child, tmp_path / "cancel-root", stop=stop)
    assert result.classification == "execution_cancelled"
    assert result.error_code == "claim_lost"

    result = _run(child, tmp_path / "timeout-root", wall=0.25)
    assert result.classification == "execution_failed"
    assert result.error_code == "child_timeout"


def test_sigterm_ignored_uses_bounded_sigkill_and_reaps_grandchild(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.pid"
    child = _child(tmp_path / "grandchild", "grandchild", marker)
    result = _run(child, tmp_path / "grandchild-root", wall=10.0)
    assert result.error_code == "child_timeout"
    for _ in range(40):
        if marker.exists():
            break
        time.sleep(0.025)
    assert marker.exists()
    grandchild_pid = int(marker.read_text(encoding="ascii"))
    for _ in range(40):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.025)
    pytest.fail("grandchild process survived process-group termination")


def test_slow_start_is_not_rejected_by_the_legacy_startup_interval(tmp_path: Path) -> None:
    child = _child(tmp_path / "slow-start", "hang")
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > 4

    result = _run(
        child,
        tmp_path / "slow-start-root",
        startup=0.01,
        wall=5.0,
        stop=stop,
    )
    assert result.classification == "execution_cancelled"
    assert result.error_code == "claim_lost"


def test_log_flood_is_bounded_and_test_executable_is_not_production_selectable(
    tmp_path: Path,
) -> None:
    child = _child(tmp_path / "flood", "flood")
    result = _run(child, tmp_path / "flood-root")
    assert result.error_code == "output_limit_exceeded"
    rejected = TrainingProcessSupervisor(executable=str(child)).run(
        config_fd=0,
        input_fd=1,
        scratch_fd=2,
        logs_fd=3,
        output_stage_fd=4,
        environment={"PATH": "/usr/bin:/bin"},
        should_stop=lambda: False,
    )
    assert rejected.error_code == "child_start_failed"
