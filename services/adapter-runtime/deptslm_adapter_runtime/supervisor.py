"""Single-target, killable production adapter child supervisor."""

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
_SAFE_CHILD_ERRORS = frozenset(
    {
        "adapter_runtime_unavailable",
        "adapter_runtime_timeout",
        "adapter_load_failed",
        "adapter_runtime_target_mismatch",
    }
)


class AdapterRuntimeSupervisorError(RuntimeError):
    def __init__(self, code: str = "adapter_runtime_unavailable") -> None:
        self.code = code
        super().__init__(code)


class AdapterRuntimeSupervisor:
    def __init__(self, settings: AdapterRuntimeSettings, *, timeout_seconds: float = 120) -> None:
        self._settings = settings
        self._timeout = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._target_key: tuple[object, ...] | None = None
        self._capacity = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._closed = False

    @property
    def ready(self) -> bool:
        return not self._closed and self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        async with self._lifecycle:
            if not self.ready:
                await self._start(())

    async def close(self) -> None:
        self._closed = True
        async with self._lifecycle:
            await self._terminate()

    async def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._closed or self._capacity.locked():
            raise AdapterRuntimeSupervisorError()
        await self._capacity.acquire()
        try:
            key = _target_key(payload)
            async with self._lifecycle:
                if key != self._target_key or not self.ready:
                    await self._terminate()
                    await self._start(key)
                process = self._process
            if process is None or process.stdin is None or process.stdout is None:
                raise AdapterRuntimeSupervisorError()
            frame = _encode_frame(payload)
            try:
                async with asyncio.timeout(self._timeout):
                    process.stdin.write(frame)
                    await process.stdin.drain()
                    response = await _read_frame(process.stdout, _MAX_RESPONSE_BYTES)
                if isinstance(response, dict) and set(response) == {"error"}:
                    code = response.get("error")
                    if code in _SAFE_CHILD_ERRORS:
                        raise AdapterRuntimeSupervisorError(code)
                if not isinstance(response, dict) or set(response) != {
                    "status",
                    "answer",
                    "citations",
                }:
                    raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")
                return response
            except asyncio.CancelledError:
                async with self._lifecycle:
                    await self._terminate()
                raise
            except TimeoutError as error:
                async with self._lifecycle:
                    await self._terminate()
                raise AdapterRuntimeSupervisorError("adapter_runtime_timeout") from error
            except AdapterRuntimeSupervisorError:
                async with self._lifecycle:
                    await self._terminate()
                raise
            except Exception as error:
                async with self._lifecycle:
                    await self._terminate()
                raise AdapterRuntimeSupervisorError() from error
        finally:
            self._capacity.release()

    async def _start(self, key: tuple[object, ...]) -> None:
        if self._closed:
            raise AdapterRuntimeSupervisorError()
        try:
            self._process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "deptslm_adapter_runtime.child",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._settings.child_environment(),
                close_fds=True,
                start_new_session=True,
            )
            if self._process.stdout is None:
                raise AdapterRuntimeSupervisorError("adapter_load_failed")
            async with asyncio.timeout(self._timeout):
                ready = await _read_frame(self._process.stdout, 4096)
            if ready != {"ready": True}:
                raise AdapterRuntimeSupervisorError("adapter_load_failed")
            self._target_key = key
        except Exception:
            await self._terminate()
            raise

    async def _terminate(self) -> None:
        process, self._process = self._process, None
        self._target_key = None
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


def _target_key(payload: dict[str, Any]) -> tuple[object, ...]:
    authority = payload.get("target")
    if not isinstance(authority, dict):
        authority = payload
    fields = (
        "department_id",
        "adapter_id",
        "adapter_version",
        "base_model_revision",
        "registry_publication_attempt_id",
        "registry_attempt_number",
        "adapter_config_sha256",
        "adapter_config_byte_size",
        "adapter_model_sha256",
        "adapter_model_byte_size",
        "target_fingerprint",
    )
    return tuple(authority.get(field) for field in fields)


def _encode_frame(value: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as error:
        raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch") from error
    if not 1 <= len(raw) <= MAX_CHILD_FRAME_BYTES:
        raise AdapterRuntimeSupervisorError("adapter_runtime_target_mismatch")
    return _HEADER.pack(len(raw)) + raw


async def _read_frame(reader: asyncio.StreamReader, maximum: int) -> Any:
    try:
        header = await reader.readexactly(_HEADER.size)
        size = _HEADER.unpack(header)[0]
        if not 1 <= size <= maximum:
            raise AdapterRuntimeSupervisorError()
        return json.loads((await reader.readexactly(size)).decode("utf-8"))
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        struct.error,
        asyncio.IncompleteReadError,
    ) as error:
        raise AdapterRuntimeSupervisorError() from error
