"""Killable, single-target supervisor for candidate model execution."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import struct
import sys
from typing import Any

from app.rag_domain import MAX_CHILD_FRAME_BYTES
from deptslm_adapter_runtime.settings import AdapterRuntimeSettings

_HEADER = struct.Struct(">I")
_MAX_RESPONSE_BYTES = 256 * 1024


class AdapterRuntimeSupervisorError(RuntimeError):
    def __init__(self, code: str = "candidate_runtime_unavailable") -> None:
        self.code = code
        super().__init__(code)


class AdapterRuntimeSupervisor:
    def __init__(
        self,
        settings: AdapterRuntimeSettings,
        *,
        operation_timeout_seconds: float = 120,
        startup_timeout_seconds: float = 300,
    ) -> None:
        self._settings = settings
        self._operation_timeout = operation_timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._capacity = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._closed = False

    @property
    def ready(self) -> bool:
        return (
            not self._closed
            and self._process is not None
            and self._process.returncode is None
        )

    async def start(self) -> None:
        async with self._lifecycle:
            await self._start_unlocked()

    async def close(self) -> None:
        self._closed = True
        async with self._lifecycle:
            await self._terminate_unlocked()

    async def request(self, payload: dict[str, Any]) -> Any:
        if self._closed or self._capacity.locked():
            raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable")
        await self._capacity.acquire()
        try:
            async with self._lifecycle:
                if not self.ready:
                    await self._start_unlocked()
                process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise AdapterRuntimeSupervisorError()
            operation = payload.get("operation")
            if operation not in {"generate", "verify"}:
                raise AdapterRuntimeSupervisorError("invalid_request")
            frame = _encode_frame({"operation": operation, "payload": payload})
            try:
                async with asyncio.timeout(self._operation_timeout):
                    process.stdin.write(frame)
                    await process.stdin.drain()
                    response = await _read_frame(process.stdout, _MAX_RESPONSE_BYTES)
                return _validate_response(response)
            except asyncio.CancelledError:
                await self._retire()
                raise
            except TimeoutError as error:
                await self._retire()
                raise AdapterRuntimeSupervisorError(
                    "candidate_runtime_timeout"
                ) from error
            except Exception as error:
                await self._retire()
                if isinstance(error, AdapterRuntimeSupervisorError):
                    raise
                raise AdapterRuntimeSupervisorError() from error
        finally:
            self._capacity.release()

    async def _start_unlocked(self) -> None:
        if self._closed:
            raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable")
        await self._terminate_unlocked()
        try:
            self._process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "deptslm_adapter_runtime.candidate_child",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._settings.child_environment(),
                close_fds=True,
                start_new_session=True,
            )
            if self._process.stdout is None:
                raise AdapterRuntimeSupervisorError()
            async with asyncio.timeout(self._startup_timeout):
                ready = await _read_frame(self._process.stdout, 4096)
            if ready != {"ready": True}:
                raise AdapterRuntimeSupervisorError("candidate_adapter_load_failed")
        except asyncio.CancelledError:
            await self._terminate_unlocked()
            raise
        except TimeoutError as error:
            await self._terminate_unlocked()
            raise AdapterRuntimeSupervisorError("candidate_runtime_timeout") from error
        except AdapterRuntimeSupervisorError:
            await self._terminate_unlocked()
            raise
        except Exception as error:
            await self._terminate_unlocked()
            raise AdapterRuntimeSupervisorError() from error

    async def _retire(self) -> None:
        async with self._lifecycle:
            await self._terminate_unlocked()

    async def _terminate_unlocked(self) -> None:
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


def _encode_frame(value: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, UnicodeEncodeError) as error:
        raise AdapterRuntimeSupervisorError("invalid_request") from error
    if not payload or len(payload) > MAX_CHILD_FRAME_BYTES:
        raise AdapterRuntimeSupervisorError("invalid_request")
    return _HEADER.pack(len(payload)) + payload


async def _read_frame(reader: asyncio.StreamReader, maximum: int) -> Any:
    try:
        header = await reader.readexactly(_HEADER.size)
        size = _HEADER.unpack(header)[0]
        if not 1 <= size <= maximum:
            raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable")
        payload = await reader.readexactly(size)
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, struct.error) as error:
        raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable") from error
    except asyncio.IncompleteReadError as error:
        raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable") from error


def _validate_response(value: Any) -> Any:
    if not isinstance(value, dict) or set(value) not in (
        {"ok", "result"},
        {"ok", "code"},
    ):
        raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable")
    if value.get("ok") is True:
        return value["result"]
    code = value.get("code")
    if not isinstance(code, str):
        raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable")
    if code == "invalid_request":
        raise AdapterRuntimeSupervisorError("invalid_request")
    if code in {
        "candidate_adapter_load_failed",
        "candidate_runtime_unavailable",
        "candidate_runtime_timeout",
        "invalid_generation_response",
    }:
        raise AdapterRuntimeSupervisorError(code)
    raise AdapterRuntimeSupervisorError("candidate_runtime_unavailable")
