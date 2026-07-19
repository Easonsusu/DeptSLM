"""Killable, bounded supervisor for the persistent model execution child."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import struct
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from app.rag_domain import MAX_CHILD_FRAME_BYTES
from deptslm_runtime.settings import RuntimeSettings

MODEL_OPERATION_TIMEOUT_SECONDS = 120
MODEL_STARTUP_TIMEOUT_SECONDS = 300
MAX_CHILD_RESPONSE_BYTES = 256 * 1024
_HEADER = struct.Struct(">I")


class RuntimeSupervisorError(RuntimeError):
    def __init__(self, code: str = "model_operation_failed") -> None:
        self.code = code
        super().__init__(code)


class RuntimeBusyError(RuntimeSupervisorError):
    def __init__(self) -> None:
        super().__init__("runtime_busy")


class RecoverableModelRequestError(RuntimeSupervisorError):
    """A validated request-specific child error that leaves the child reusable."""


class ModelSupervisor:
    """Own one process group and replace it once after a fatal operation."""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        command: tuple[str, ...] | Callable[[], tuple[str, ...]] | None = None,
        operation_timeout_seconds: float = MODEL_OPERATION_TIMEOUT_SECONDS,
        startup_timeout_seconds: float = MODEL_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._command = command or (sys.executable, "-m", "deptslm_runtime.model_child")
        self._operation_timeout = operation_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._capacity = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._restart_failed = False
        self._closed = False

    @property
    def child_pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    @property
    def restarting(self) -> bool:
        restart = self._restart_task
        return restart is not None and not restart.done()

    @property
    def ready(self) -> bool:
        restart = self._restart_task
        return (
            not self._closed
            and (restart is None or restart.done())
            and self._process is not None
            and self._process.returncode is None
        )

    async def start(self) -> None:
        if self._closed:
            raise RuntimeSupervisorError("runtime_shutdown")
        await self._start_child()

    async def close(self) -> None:
        self._closed = True
        restart, self._restart_task = self._restart_task, None
        if restart is not None and not restart.done():
            restart.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await restart
        await self._terminate_child()

    async def request(self, operation: str, payload: dict[str, Any]) -> Any:
        if operation not in {"query_embedding", "generate"}:
            raise RuntimeSupervisorError("invalid_operation")
        if self._closed:
            raise RuntimeSupervisorError("runtime_shutdown")
        if self._capacity.locked():
            raise RuntimeBusyError()
        await self._capacity.acquire()
        try:
            frame = _encode_frame({"operation": operation, "payload": payload})
            process = self._ready_process()
            if process is None:
                if self.restarting:
                    raise RuntimeSupervisorError("runtime_restarting")
                if self._restart_failed:
                    raise RuntimeSupervisorError("runtime_unavailable")
                await self._retire_and_restart()
                raise RuntimeSupervisorError("runtime_restarting")
            try:
                async with asyncio.timeout(self._operation_timeout):
                    if process.stdin is None or process.stdout is None:
                        raise RuntimeSupervisorError()
                    process.stdin.write(frame)
                    await process.stdin.drain()
                    response = await _read_frame(process.stdout, MAX_CHILD_RESPONSE_BYTES)
                return _validated_response(response)
            except RecoverableModelRequestError:
                raise
            except asyncio.CancelledError:
                await asyncio.shield(self._retire_and_restart())
                raise
            except TimeoutError as error:
                await self._retire_and_restart()
                raise RuntimeSupervisorError("model_timeout") from error
            except RuntimeSupervisorError:
                await self._retire_and_restart()
                raise
            except (BrokenPipeError, ConnectionError, OSError) as error:
                await self._retire_and_restart()
                raise RuntimeSupervisorError() from error
        finally:
            self._capacity.release()

    def _ready_process(self) -> asyncio.subprocess.Process | None:
        restart = self._restart_task
        process = self._process
        if (
            self._closed
            or (restart is not None and not restart.done())
            or process is None
            or process.returncode is not None
        ):
            return None
        return process

    async def _start_child(self) -> asyncio.subprocess.Process:
        async with self._lifecycle:
            if self._closed:
                raise RuntimeSupervisorError("runtime_shutdown")
            if self._process is not None and self._process.returncode is None:
                return self._process
            await self._terminate_child_unlocked()
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._next_command(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=self._settings.child_environment(),
                    close_fds=True,
                    start_new_session=True,
                )
                if self._process.stdout is None:
                    raise RuntimeSupervisorError()
                async with asyncio.timeout(self._startup_timeout):
                    ready = await _read_frame(self._process.stdout, 4096)
                if ready != {"ready": True}:
                    raise RuntimeSupervisorError("model_context_mismatch")
                self._restart_failed = False
                return self._process
            except asyncio.CancelledError:
                await asyncio.shield(self._terminate_child_unlocked())
                raise
            except TimeoutError as error:
                await self._terminate_child_unlocked()
                raise RuntimeSupervisorError("model_startup_timeout") from error
            except RuntimeSupervisorError:
                await self._terminate_child_unlocked()
                raise
            except (OSError, asyncio.IncompleteReadError) as error:
                await self._terminate_child_unlocked()
                raise RuntimeSupervisorError() from error

    async def _retire_and_restart(self) -> None:
        await self._terminate_child()
        self._schedule_restart()

    def _schedule_restart(self) -> None:
        if self._closed:
            return
        current = self._restart_task
        if current is not None and not current.done():
            return
        self._restart_failed = False
        task = asyncio.create_task(self._restart_once())
        self._restart_task = task
        task.add_done_callback(self._restart_finished)

    async def _restart_once(self) -> None:
        try:
            await self._start_child()
        except RuntimeSupervisorError:
            # Readiness remains false. A bounded restart never loops or leaks details.
            self._restart_failed = True
            return

    def _restart_finished(self, task: asyncio.Task[None]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            task.exception()
        if self._restart_task is task:
            self._restart_task = None

    def _next_command(self) -> tuple[str, ...]:
        try:
            command = self._command() if callable(self._command) else self._command
        except Exception as error:
            raise RuntimeSupervisorError("invalid_child_command") from error
        if not command or not all(isinstance(item, str) and item for item in command):
            raise RuntimeSupervisorError("invalid_child_command")
        return command

    async def _terminate_child(self) -> None:
        async with self._lifecycle:
            await self._terminate_child_unlocked()

    async def _terminate_child_unlocked(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        else:
            await process.wait()


async def run_until_disconnect(
    operation: Awaitable[Any], disconnected: Callable[[], Awaitable[bool]]
) -> Any:
    """Cancel model work if the HTTP peer disappears before completion."""

    model_task = asyncio.create_task(operation)
    disconnect_task = asyncio.create_task(_wait_for_disconnect(disconnected))
    try:
        done, _pending = await asyncio.wait(
            {model_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if disconnect_task in done and disconnect_task.result():
            model_task.cancel()
            try:
                await model_task
            except asyncio.CancelledError:
                pass
            raise asyncio.CancelledError()
        disconnect_task.cancel()
        return await model_task
    except asyncio.CancelledError:
        model_task.cancel()
        try:
            await model_task
        except asyncio.CancelledError:
            pass
        raise
    finally:
        disconnect_task.cancel()


async def _wait_for_disconnect(disconnected: Callable[[], Awaitable[bool]]) -> bool:
    while True:
        if await disconnected():
            return True
        await asyncio.sleep(0.05)


def _encode_frame(value: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as error:
        raise RuntimeSupervisorError("invalid_request") from error
    if not payload or len(payload) > MAX_CHILD_FRAME_BYTES:
        raise RuntimeSupervisorError("invalid_request")
    return _HEADER.pack(len(payload)) + payload


async def _read_frame(reader: asyncio.StreamReader, maximum: int) -> Any:
    try:
        header = await reader.readexactly(_HEADER.size)
        size = _HEADER.unpack(header)[0]
        if not 1 <= size <= maximum:
            raise RuntimeSupervisorError("invalid_child_response")
        payload = await reader.readexactly(size)
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, struct.error) as error:
        raise RuntimeSupervisorError("invalid_child_response") from error
    except asyncio.IncompleteReadError as error:
        raise RuntimeSupervisorError("child_exited") from error


def _validated_response(value: Any) -> Any:
    if not isinstance(value, dict) or set(value) not in (
        {"ok", "result"},
        {"ok", "code"},
    ):
        raise RuntimeSupervisorError("invalid_child_response")
    if value.get("ok") is True:
        return value["result"]
    code = value.get("code")
    if value.get("ok") is False and code == "model_input_too_large":
        raise RecoverableModelRequestError(code)
    if (
        value.get("ok") is False
        and isinstance(code, str)
        and code
        in {
            "model_context_mismatch",
            "model_operation_failed",
            "invalid_request",
        }
    ):
        raise RuntimeSupervisorError(code)
    raise RuntimeSupervisorError("invalid_child_response")
