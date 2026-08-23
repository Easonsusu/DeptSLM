"""Fixed-command process-group supervision for the real training child."""

from __future__ import annotations

import os
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from .contract import (
    KILL_REAP_SECONDS,
    MAX_LOG_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_FILE_BYTES,
    MAX_OUTPUT_FILES,
    MAX_SCRATCH_BYTES,
    STARTUP_TIMEOUT_SECONDS,
    TERM_GRACE_SECONDS,
    TRAINING_WALL_SECONDS,
)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    classification: str
    error_code: str | None
    stdout_bytes: int
    stderr_bytes: int


class TrainingProcessSupervisor:
    """Run exactly one fixed CLI in a dedicated process group."""

    def __init__(
        self,
        *,
        executable: str = "/opt/llamafactory/bin/llamafactory-cli",
        test_executable: bool = False,
        startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
        wall_timeout_seconds: float = TRAINING_WALL_SECONDS,
        term_grace_seconds: float = TERM_GRACE_SECONDS,
        kill_reap_seconds: float = KILL_REAP_SECONDS,
    ) -> None:
        self._executable = executable
        self._test_executable = test_executable
        self._startup_timeout = startup_timeout_seconds
        self._wall_timeout = wall_timeout_seconds
        self._term_grace = term_grace_seconds
        self._kill_reap = kill_reap_seconds
        self._process: subprocess.Popen[bytes] | None = None

    @property
    def child_pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def run(
        self,
        *,
        config_fd: int,
        input_fd: int,
        scratch_fd: int,
        logs_fd: int,
        output_stage_fd: int,
        environment: dict[str, str],
        should_stop: Callable[[], bool],
    ) -> ProcessResult:
        fds = (config_fd, input_fd, scratch_fd, logs_fd, output_stage_fd)
        if any(type(fd) is not int or fd < 0 for fd in fds) or len(set(fds)) != len(fds):
            return ProcessResult("execution_failed", "child_start_failed", 0, 0)
        command = (self._executable, "train", f"/proc/self/fd/{config_fd}")
        if (
            self._executable != "/opt/llamafactory/bin/llamafactory-cli"
            and not self._test_executable
        ):
            return ProcessResult("execution_failed", "child_start_failed", 0, 0)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_allowlisted_environment(environment),
                cwd="/",
                close_fds=True,
                pass_fds=fds,
                start_new_session=True,
            )
        except (OSError, ValueError):
            return ProcessResult("execution_failed", "child_start_failed", 0, 0)
        self._process = process
        selector = selectors.DefaultSelector()
        counters = {"stdout": 0, "stderr": 0}
        log_descriptors: dict[str, int] = {}
        try:
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                if pipe is None:
                    continue
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
                log_descriptors[name] = _open_log(logs_fd, name)
            started = time.monotonic()
            while True:
                if should_stop():
                    self.terminate()
                    return ProcessResult(
                        "execution_cancelled",
                        "claim_lost",
                        counters["stdout"],
                        counters["stderr"],
                    )
                elapsed = time.monotonic() - started
                if process.poll() is None and elapsed > self._startup_timeout:
                    self.terminate()
                    return ProcessResult(
                        "execution_failed",
                        "child_start_failed",
                        counters["stdout"],
                        counters["stderr"],
                    )
                if elapsed > self._wall_timeout:
                    self.terminate()
                    return ProcessResult(
                        "execution_failed",
                        "child_timeout",
                        counters["stdout"],
                        counters["stderr"],
                    )
                try:
                    _check_tree_limits(
                        scratch_fd,
                        max_files=MAX_OUTPUT_FILES,
                        max_bytes=MAX_SCRATCH_BYTES,
                        max_file_bytes=MAX_OUTPUT_FILE_BYTES,
                    )
                    _check_tree_limits(
                        output_stage_fd,
                        max_files=MAX_OUTPUT_FILES,
                        max_bytes=MAX_OUTPUT_BYTES,
                        max_file_bytes=MAX_OUTPUT_FILE_BYTES,
                    )
                except ValueError as error:
                    self.terminate()
                    return ProcessResult(
                        "execution_failed",
                        str(error),
                        counters["stdout"],
                        counters["stderr"],
                    )
                for key, _ in selector.select(timeout=0.2):
                    try:
                        block = os.read(key.fileobj.fileno(), 64 * 1024)
                    except OSError:
                        block = b""
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    counters[name] += len(block)
                    if sum(counters.values()) > MAX_LOG_BYTES:
                        self.terminate()
                        return ProcessResult(
                            "execution_failed",
                            "output_limit_exceeded",
                            counters["stdout"],
                            counters["stderr"],
                        )
                    descriptor = log_descriptors.get(name)
                    if descriptor is not None:
                        _bounded_write(descriptor, block)
                if process.poll() is not None:
                    # Drain any final pipe bytes before returning.
                    if not selector.get_map():
                        break
                if process.poll() is not None and not selector.get_map():
                    break
            return ProcessResult(
                "execution_succeeded" if process.returncode == 0 else "execution_failed",
                None if process.returncode == 0 else "child_failed",
                counters["stdout"],
                counters["stderr"],
            )
        finally:
            selector.close()
            for descriptor in log_descriptors.values():
                try:
                    os.fsync(descriptor)
                except OSError:
                    pass
                os.close(descriptor)
            if process.poll() is None:
                self.terminate()
            self._process = None

    def terminate(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=self._term_grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=self._kill_reap)
                except subprocess.TimeoutExpired:
                    # The runtime fails closed; a child that cannot be reaped
                    # is never reported as a successful execution.
                    pass
        else:
            process.wait()


def _allowlisted_environment(values: dict[str, str]) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "TOKENIZERS_PARALLELISM",
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "NVIDIA_DRIVER_CAPABILITIES",
        "DEPTSLM_TRAINING_RUNTIME_PROFILE",
        "DEPTSLM_TRAINING_MODEL_PATH",
    }
    if set(values) - allowed:
        raise ValueError("runtime_environment_invalid")
    result = {key: value for key, value in values.items() if key in allowed}
    for forbidden in (
        "DATABASE_URL",
        "DEPTSLM_TRAINING_RUNTIME_TOKEN",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        result.pop(forbidden, None)
    result.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1"})
    return result


def _open_log(logs_fd: int, name: str) -> int:
    descriptor = os.open(
        f"{name}.log",
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
        dir_fd=logs_fd,
    )
    return descriptor


def _bounded_write(descriptor: int, block: bytes) -> None:
    view = memoryview(block)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("log write failed")
        view = view[written:]


def _check_tree_limits(
    descriptor: int, *, max_files: int, max_bytes: int, max_file_bytes: int
) -> None:
    files = 0
    total = 0

    def walk(parent: int, depth: int) -> None:
        nonlocal files, total
        if depth > 16:
            raise ValueError("output_limit_exceeded")
        try:
            names = os.listdir(parent)
        except OSError as error:
            raise ValueError("output_invalid") from error
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("output_invalid")
            try:
                metadata = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except OSError as error:
                raise ValueError("output_invalid") from error
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("output_invalid")
            if stat.S_ISDIR(metadata.st_mode):
                if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
                    raise ValueError("output_invalid")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    actual = os.fstat(child)
                    if actual.st_ino != metadata.st_ino or actual.st_dev != metadata.st_dev:
                        raise ValueError("output_invalid")
                    walk(child, depth + 1)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("output_invalid")
            if metadata.st_size > max_file_bytes:
                raise ValueError("output_limit_exceeded")
            files += 1
            total += metadata.st_size
            if files > max_files or total > max_bytes:
                raise ValueError("output_limit_exceeded")

    walk(descriptor, 0)
