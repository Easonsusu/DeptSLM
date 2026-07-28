"""Killable, heartbeat-supervised Phase 10 child operations.

The parent alone owns PostgreSQL claims.  Children receive no database URL,
tokens, or runtime configuration and are always reaped as process groups.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from collections.abc import Callable


def run_claimed_operation(
    *,
    timeout_seconds: int,
    heartbeat_seconds: int,
    should_stop: Callable[[], bool],
    heartbeat: Callable[[], None],
    error: Callable[[str], Exception],
    operation: Callable[[], object],
) -> object:
    """Run one blocking operation while continuously retaining its exact claim."""

    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_child_main, args=(sender, operation), daemon=False)
    child.start()
    sender.close()
    deadline = time.monotonic() + timeout_seconds
    heartbeat_interval = max(1.0, min(15.0, heartbeat_seconds / 3))
    heartbeat_at = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if should_stop():
                raise error("worker_shutdown")
            if now >= deadline:
                raise error("worker_timeout")
            if now >= heartbeat_at:
                heartbeat()
                heartbeat_at = now + heartbeat_interval
            if receiver.poll(min(0.2, max(0.01, deadline - now))):
                kind, value = receiver.recv()
                child.join(timeout=1)
                if kind == "ok":
                    return value
                raise error(value if isinstance(value, str) else "dataset_publication_failed")
            if not child.is_alive():
                child.join(timeout=1)
                raise error("dataset_publication_failed")
    except (EOFError, OSError) as caught:
        raise error("dataset_publication_failed") from caught
    finally:
        receiver.close()
        _terminate_group(child)


def _child_main(sender, operation: Callable[[], object]) -> None:
    try:
        os.setsid()
        os.environ.clear()
        os.environ["PATH"] = "/usr/bin:/bin"
        sender.send(("ok", operation()))
    except BaseException as caught:
        try:
            code = getattr(caught, "code", "dataset_publication_failed")
            sender.send(("error", code if isinstance(code, str) else "dataset_publication_failed"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


def _terminate_group(child: multiprocessing.Process) -> None:
    if child.pid is None:
        return
    if child.is_alive():
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        child.join(timeout=2)
    if child.is_alive():
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.join(timeout=5)
